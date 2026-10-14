"""Containment: running a command leasehold starts in a systemd user scope sized to its lease.

A lease alone is accounting: a weight-1 command may still use the whole machine. When the user's
systemd manager can take it, `hold` (and so the PATH shims) and `leasehold run` start their
command in a transient scope of the user manager instead, with

* `CPUWeight = 100 x weight`. CPU weights share among siblings: a weight-4 scope gets four
  times the CPU of a weight-1 scope beside it when the CPUs are contended, and any idle CPU when
  they are not. `leasehold.slice` as a whole competes with the rest of the user's session at
  systemd's default weight, and each `leasehold run` session slice with the other sessions
  equally. A hard `CPUQuota` of `weight` CPUs is set only when asked for (`hard_cpu`);
* `MemoryHigh` when the lease names a memory size: past it the kernel throttles and reclaims
  the scope's memory. It is soft: it never kills, and it bounds nothing outright (`MemoryMax` is
  never set), so a command can still pass it, and the machine can still run out of memory.

Scopes sit in `leasehold.slice`. `leasehold run` gives each agent session a slice of its own
(`leasehold-<session>.slice`, named to its commands in `LEASEHOLD_SLICE`), so the agent's scope
(`agent-<session>.scope`) and the scopes of the commands it holds leases for (`lease-<id>.scope`)
share one slice that `run` stops as one when the agent ends.
`systemd-run --scope` executes the command itself: its PID, process group and signals are the
same as without a scope. `spawn` starts it behind a small trampoline that reports once the scope
exists, so a scope that cannot be made (the user manager refuses, or does not answer) is seen
before the command has run, and the command is then started uncontained instead.

Containment is used only where it cannot take a command from someone else's control: the caller
must itself run in the user's own part of the tree (`/user.slice/user-<uid>.slice/`, a login
session or the user manager). A command started from a system service, a CI runner's job or an
operator's slice stays where it is, under that service's limits and clean-up. Anywhere it is not
used, `probe` says why, and the lease works as before ("advisory").
"""

from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leasehold.units import memory_to_bytes

#: Systemd's own default CPUWeight, the weight of one slot.
WEIGHT_PER_SLOT = 100
#: The slice all of leasehold's scopes live under.
ROOT_SLICE = "leasehold.slice"
#: `LEASEHOLD_CONTAIN=off` turns containment off for one command and what it starts.
ENV = "LEASEHOLD_CONTAIN"
#: The session slice `leasehold run` made, for the scopes of the commands under it.
ENV_SLICE = "LEASEHOLD_SLICE"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.]")


@dataclass(frozen=True)
class Containment:
    """Whether commands are put in scopes here (`enforced`), and if not, why."""

    enforced: bool
    reason: str

    def describe(self) -> str:
        return "enforced (systemd)" if self.enforced else f"advisory: {self.reason}"


def probe(
    *,
    uid: int | None = None,
    env: Mapping[str, str] | None = None,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    self_cgroup: Path = Path("/proc/self/cgroup"),
    which=shutil.which,
    answers=None,
) -> Containment:
    """Whether a command this process starts can be put in a user scope: cheap reads, then one
    question to the user manager (every root and the question are injectable, so tests use
    fixture trees)."""
    answers = _answers if answers is None else answers
    uid = os.getuid() if uid is None else uid
    env = os.environ if env is None else env
    if env.get(ENV, "").lower() in ("off", "0", "no", "false"):
        return Containment(False, f"{ENV}={env[ENV]}")
    if not (cgroup_root / "cgroup.controllers").exists():
        return Containment(False, "no cgroup v2 at " + str(cgroup_root))
    try:
        mine = self_cgroup.read_text()
    except OSError:
        return Containment(False, f"cannot read {self_cgroup}")
    path = next((line[3:] for line in mine.splitlines() if line.startswith("0::")), "")
    home = f"/user.slice/user-{uid}.slice/"
    if not path.startswith(home):
        return Containment(False, f"caller in {path or 'an unknown cgroup'}, not a user session")
    manager = cgroup_root / home.strip("/") / f"user@{uid}.service"
    try:
        delegated = set((manager / "cgroup.subtree_control").read_text().split())
    except OSError:
        return Containment(False, f"no systemd user manager for uid {uid}")
    missing = sorted({"cpu", "memory"} - delegated)
    if missing:
        return Containment(False, f"user manager is not delegated {' and '.join(missing)}")
    runtime = env.get("XDG_RUNTIME_DIR")
    if not runtime or not (Path(runtime) / "systemd" / "private").exists():
        return Containment(False, "the user manager cannot be reached (XDG_RUNTIME_DIR)")
    if which("systemd-run") is None or which("systemctl") is None:
        return Containment(False, "systemd-run or systemctl is not installed")
    if not answers():
        return Containment(False, "the user manager does not answer (systemctl --user)")
    return Containment(True, "")


def _answers() -> bool:
    try:
        done = subprocess.run(
            ["systemctl", "--user", "show", "--property=Version"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def unit_part(name: str) -> str:
    """`name` made safe inside a unit name. A `-` in a slice name means nesting, so it goes."""
    return _UNSAFE.sub("_", name)[:80] or "_"


_SLICE = re.compile(r"^leasehold-[A-Za-z0-9_.]{1,100}\.slice$")


def session_slice(session: str, token: str) -> str:
    """The slice one `leasehold run` puts its session in: the session's name for people to
    read, and `token`, which is the run's own, so no two runs ever share a slice."""
    return f"leasehold-{unit_part(session)[:40]}_{unit_part(token)}.slice"


def is_session_slice(name: str) -> bool:
    return bool(_SLICE.match(name))


def scope_argv(
    unit: str,
    slice_: str,
    *,
    weight: int,
    memory: str | None = None,
    hard_cpu: bool = False,
) -> list[str]:
    """The `systemd-run` prefix that starts a command in scope `unit` of `slice_`, sized to a
    lease of `weight` slots (and `memory`, a size such as `4g`, when given)."""
    argv = [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--slice={slice_}",
        "-p",
        f"CPUWeight={min(10_000, max(1, weight) * WEIGHT_PER_SLOT)}",
    ]
    if hard_cpu:
        argv += ["-p", f"CPUQuota={max(1, weight) * 100}%"]
    if memory:
        argv += ["-p", f"MemoryHigh={memory_to_bytes(memory)}"]
    return [*argv, "--"]


def lease_argv(lease_id: str, env: Mapping[str, str] | None = None, **sizes) -> list[str]:
    """`scope_argv` for a lease's command: `lease-<id>.scope`, in the slice `leasehold run`
    named in `LEASEHOLD_SLICE`, else in `leasehold.slice`."""
    env = os.environ if env is None else env
    slice_ = env.get(ENV_SLICE) or ""
    if not is_session_slice(slice_):
        slice_ = ROOT_SLICE
    return scope_argv(f"lease-{unit_part(lease_id)}.scope", slice_, **sizes)


class CleanupError(RuntimeError):
    """What was left in a slice could not be stopped, or not be confirmed stopped."""


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"systemctl --user {' '.join(args)}: {exc}") from None


def _populated(slice_: str, cgroup_root: Path) -> bool:
    """Whether any process is left in `slice_`. A slice systemd no longer knows is empty."""
    shown = _systemctl("show", "-P", "ControlGroup", slice_)
    if shown.returncode != 0:
        raise CleanupError(f"cannot ask about {slice_}: {shown.stderr.strip()}")
    group = shown.stdout.strip()
    if not group:
        return False
    try:
        events = (cgroup_root / group.lstrip("/") / "cgroup.events").read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CleanupError(f"cannot read {slice_}'s cgroup: {exc}") from None
    return "populated 1" in events


def stop_slice(
    slice_: str, grace_s: float = 10.0, *, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> bool:
    """Stop everything left in `slice_` (one `leasehold run` session's): SIGTERM, up to
    `grace_s` to exit, then SIGKILL, then the slice unit itself, so no empty slice is left
    behind. Only that slice's units are touched. Whether anything had been left running.
    Raises `CleanupError` when something could not be stopped, or not be seen to stop."""
    if not is_session_slice(slice_):
        raise ValueError(f"not a session slice: {slice_}")
    left = _populated(slice_, cgroup_root)
    if left:
        _systemctl("kill", "--signal=TERM", slice_)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and _populated(slice_, cgroup_root):
            time.sleep(0.1)
        if _populated(slice_, cgroup_root):
            _systemctl("kill", "--signal=KILL", slice_)
            deadline = time.monotonic() + 5
            while _populated(slice_, cgroup_root):
                if time.monotonic() > deadline:
                    raise CleanupError(f"processes are still running in {slice_} after SIGKILL")
                time.sleep(0.1)
    stopped = _systemctl("stop", slice_)
    if stopped.returncode != 0:
        raise CleanupError(f"cannot stop {slice_}: {stopped.stderr.strip()}")
    return left


#: Run in the scope by `spawn`: say the scope exists on the pipe it is handed, then become the
#: command. Exit 127, as a shell would, when the command cannot be run.
_TRAMPOLINE = (
    "import os,sys\n"
    "fd=int(sys.argv[1]); os.write(fd,b'x'); os.close(fd)\n"
    "try: os.execvp(sys.argv[2],sys.argv[2:])\n"
    "except OSError as e: sys.stderr.write(f'{sys.argv[2]}: {e}\\n'); os._exit(127)\n"
)
#: How long `spawn` waits for a scope before it gives up on it.
SCOPE_TIMEOUT_S = 30.0


class Cancelled(Exception):
    """`spawn` stopped waiting for a scope because its `cancel_fd` turned readable: the command
    has not run, and what was started for it has been killed."""


def spawn(
    argv: Sequence[str], prefix: Sequence[str] | None, *, cancel_fd: int | None = None, **popen
) -> tuple[subprocess.Popen, str | None]:
    """Start `argv` (with `subprocess.Popen`'s `popen` arguments) in the scope `prefix` makes,
    or as is when there is no prefix. When the scope cannot be made, the command has not run,
    and it is started as is instead. When `cancel_fd` turns readable before the scope is made,
    raises `Cancelled`. The process, and why it is not contained when a scope was asked for and
    could not be made (None otherwise)."""
    if not prefix:
        return subprocess.Popen(list(argv), **popen), None
    read_end, write_end = os.pipe()
    trampoline = [sys.executable, "-I", "-S", "-c", _TRAMPOLINE, str(write_end), *argv]
    try:
        child = subprocess.Popen([*prefix, *trampoline], pass_fds=(write_end,), **popen)
    finally:
        os.close(write_end)
    waited = [read_end] if cancel_fd is None else [read_end, cancel_fd]
    try:
        ready, _, _ = select.select(waited, [], [], SCOPE_TIMEOUT_S)
        started = read_end in ready and os.read(read_end, 1) == b"x"
    finally:
        os.close(read_end)
    if started:
        return child, None
    if child.poll() is None:  # no answer in time: the command has not run, and now never will
        child.kill()
    code = child.wait()
    if cancel_fd is not None and cancel_fd in ready:
        raise Cancelled
    why = f"no systemd scope (systemd-run exited {code}); running uncontained"
    return subprocess.Popen(list(argv), **popen), why
