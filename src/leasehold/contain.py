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
  the scope's memory. It is soft: it never kills, and it bounds nothing outright, so a command
  can still pass it, and the machine can still run out of memory;
* a hard cap only when one is asked for (`memory_max`): `MemoryMax` with no swap, past which the
  kernel kills the whole command.

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
operator's slice stays where it is, under that service's limits and clean-up. So does a command
whose caller runs under a `memory.max` that the scope would leave behind (`cgroup.escaped`):
Claude Code's `CLAUDE_CODE_TOOL_MEMORY_LIMIT` group, or the scope of a capped command that runs
`hold` itself. Anywhere it is not used, `probe` says why, and the lease works as before
("advisory").

A lease's scope can also be given a hard cap (`memory_max`; the properties come from
`leasehold.host`): then it is started without `--collect`, so that once it has ended systemd
still says whether the OOM killer ended it (`cap_result`), and `spawn` sets its
`memory.oom.group` from inside, so the kernel kills all of it at once.
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

from leasehold import cgroup
from leasehold.units import memory_to_bytes, size

#: Systemd's own default CPUWeight, the weight of one slot.
WEIGHT_PER_SLOT = 100
#: The slice all of leasehold's scopes live under.
ROOT_SLICE = "leasehold.slice"
#: `LEASEHOLD_CONTAIN=off` turns containment off for one command and what it starts.
ENV = "LEASEHOLD_CONTAIN"
#: The session slice `leasehold run` made, for the scopes of the commands under it.
ENV_SLICE = "LEASEHOLD_SLICE"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.]")
#: How `probe`'s reason starts when the caller runs under a memory limit a scope would escape.
UNDER_LIMIT = "caller under a memory limit"


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
    target = "/" + root_slice_cgroup(uid)
    if is_session_slice(env.get(ENV_SLICE) or ""):
        target += "/" + env[ENV_SLICE]
    kept = cgroup.escaped(cgroup_root, path, target)
    if kept is not None:
        return Containment(
            False,
            f"{UNDER_LIMIT} of {size(kept.limit_bytes)} ({kept.cgroup}): a scope would take "
            "the command out from under it, so commands stay there",
        )
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


def root_slice_cgroup(uid: int | None = None) -> str:
    """Where `ROOT_SLICE` is in the cgroup tree for user `uid` (default ours), relative to the
    cgroup root: under the user manager, where `probe` requires it. Every contained command's
    scope is in it, a `leasehold run` session's slice included."""
    uid = os.getuid() if uid is None else uid
    return f"user.slice/user-{uid}.slice/user@{uid}.service/{ROOT_SLICE}"


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
    cap: Sequence[str] = (),
) -> list[str]:
    """The `systemd-run` prefix that starts a command in scope `unit` of `slice_`, sized to a
    lease of `weight` slots (and `memory`, a size such as `4g`, when given), with the unit
    properties `cap` of a hard memory cap (`leasehold.host`) when given. A capped scope is
    kept once it fails, for `cap_result` to read."""
    argv = ["systemd-run", "--user", "--scope", "--quiet"]
    if not cap:
        argv.append("--collect")
    argv += [f"--unit={unit}", f"--slice={slice_}"]
    for prop in [*scope_properties(weight=weight, memory=memory, hard_cpu=hard_cpu), *cap]:
        argv += ["-p", prop]
    return [*argv, "--"]


def scope_properties(
    *, weight: int, memory: str | None = None, hard_cpu: bool = False
) -> list[str]:
    """The unit properties that size a scope to a lease of `weight` slots (and `memory`, a size
    such as `4g`, when given)."""
    props = [f"CPUWeight={min(10_000, max(1, weight) * WEIGHT_PER_SLOT)}"]
    if hard_cpu:
        props.append(f"CPUQuota={max(1, weight) * 100}%")
    if memory:
        props.append(f"MemoryHigh={memory_to_bytes(memory)}")
    return props


def lease_unit(lease_id: str) -> str:
    """The scope a lease's command runs in: `lease-<id>.scope`."""
    return f"lease-{unit_part(lease_id)}.scope"


def lease_slice(env: Mapping[str, str] | None = None) -> str:
    """The slice a lease's scope goes in: the one `leasehold run` named in `LEASEHOLD_SLICE`,
    else `leasehold.slice`."""
    env = os.environ if env is None else env
    slice_ = env.get(ENV_SLICE) or ""
    return slice_ if is_session_slice(slice_) else ROOT_SLICE


def lease_argv(lease_id: str, env: Mapping[str, str] | None = None, **sizes) -> list[str]:
    """`scope_argv` for a lease's command: `lease_unit`, in `lease_slice`."""
    return scope_argv(lease_unit(lease_id), lease_slice(env), **sizes)


def update_scope(
    unit: str, *, weight: int, memory: str | None = None, hard_cpu: bool = False
) -> str | None:
    """Size the running scope `unit` to a lease of `weight` slots (and `memory`, when given),
    as `scope_argv` sized it at the start, until it ends (`systemctl --user set-property
    --runtime`). Best effort: why it could not be done, or None."""
    props = scope_properties(weight=weight, memory=memory, hard_cpu=hard_cpu)
    argv = ["systemctl", "--user", "set-property", "--runtime", unit, *props]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot resize {unit}: {exc}"
    if done.returncode != 0:
        return f"cannot resize {unit}: {done.stderr.strip() or f'exit {done.returncode}'}"
    return None


#: How `adopt` sends each property `scope_properties` and a cap give over D-Bus: its name
#: there, its type, and its value there from the `systemctl` one.
_DBUS = {
    "CPUWeight": ("CPUWeight", "t", int),
    "CPUQuota": ("CPUQuotaPerSecUSec", "t", lambda v: int(v.rstrip("%")) * 10_000),
    "MemoryHigh": ("MemoryHigh", "t", int),
    "MemoryMax": ("MemoryMax", "t", int),
    "MemorySwapMax": ("MemorySwapMax", "t", int),
}
_MANAGER = ["org.freedesktop.systemd1", "/org/freedesktop/systemd1"]
_MANAGER += ["org.freedesktop.systemd1.Manager"]


def _busctl(*args: str) -> subprocess.CompletedProcess:
    argv = ["busctl", "--user", "call", *_MANAGER, *args]
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)


def adopt(
    pids: Sequence[int],
    unit: str,
    slice_: str,
    props: Sequence[str] = (),
    *,
    oom_continue: bool = True,
    wait_s: float = 2.0,
    proc: Path = Path("/proc"),
) -> str | None:
    """Move the running processes `pids` into a new scope `unit` of `slice_` with the unit
    properties `props` (as `scope_properties` and `leasehold.host.cap_properties` give them),
    or into `unit` as it is when it exists already. This is what `systemd-run --scope` asks of
    the user manager for its own pid (`StartTransientUnit` with `PIDs`), asked for processes
    that are already running; the user manager has the system manager move a process out of a
    login session's scope. With `oom_continue`, an OOM kill of one process in the scope leaves
    the others running (`OOMPolicy=continue`, where systemd knows it), so that the command
    ends as it would have outside it. Why the processes could not be moved, or None once each
    of them that still runs is seen in the scope, or `wait_s` has passed."""
    if shutil.which("busctl") is None:
        return "busctl is not installed"
    pids = [int(p) for p in pids]
    listed = ["au", str(len(pids)), *map(str, pids)]
    existing = _busctl("GetUnit", "s", unit).returncode == 0
    if existing:
        done = _busctl("AttachProcessesToUnit", "ssau", unit, "", *listed[1:])
    else:
        sent = [("PIDs", *listed), ("Slice", "s", slice_), ("Description", "s", unit)]
        for prop in props:
            name, _, value = prop.partition("=")
            dbus, kind, convert = _DBUS[name]
            sent.append((dbus, kind, str(convert(value))))
        oom = [("OOMPolicy", "s", "continue")] if oom_continue else []
        for extra in (oom, []) if oom else ([],):
            flat = [word for item in [*sent, *extra] for word in item]
            done = _busctl(
                "StartTransientUnit", "ssa(sv)a(sa(sv))", unit, "fail",
                str(len(sent) + len(extra)), *flat, "0",
            )  # fmt: skip
            if done.returncode == 0 or "OOMPolicy" not in done.stderr:
                break  # a systemd from before scopes had an OOMPolicy is asked again without
    if done.returncode != 0:
        return f"cannot move pids into {unit}: {done.stderr.strip() or done.returncode}"
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        homes = [cgroup.own_cgroup(proc, str(pid)) for pid in pids]
        if all(home is None or home.endswith("/" + unit) for home in homes):
            return None
        time.sleep(0.005)
    return f"{unit} was made, but its processes were not seen in it within {wait_s:g}s"


#: The properties that take a scope's lease limits off: the weight of one slot, and no CPU or
#: memory limit.
NEUTRAL = (
    f"CPUWeight={WEIGHT_PER_SLOT}",
    "CPUQuota=",
    "MemoryHigh=infinity",
    "MemoryMax=infinity",
    "MemorySwapMax=infinity",
)


def neutral_scope(unit: str) -> str | None:
    """Take the lease's limits off the scope `unit`, which outlives its lease (a server its
    command left running): `NEUTRAL`, until it ends. Best effort: why it could not be done,
    or None."""
    argv = ["systemctl", "--user", "set-property", "--runtime", unit, *NEUTRAL]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"cannot reset {unit}: {exc}"
    if done.returncode != 0:
        return f"cannot reset {unit}: {done.stderr.strip() or f'exit {done.returncode}'}"
    return None


def cap_result(
    unit: str, wait_s: float = 3.0, cgroup_root: Path = Path("/sys/fs/cgroup")
) -> str | None:
    """How systemd says the capped scope `unit` ended (`Result`: `oom-kill` when the OOM
    killer ended a process of it), once its command has ended; the scope is then forgotten
    (`reset-failed`). An emptied scope is waited for, up to `wait_s`, until systemd has seen
    it end; one that still holds processes (a server the command left) is not, and is
    `success` unless an OOM kill already failed it. None when systemd cannot be asked, or no
    longer knows the scope."""
    deadline = time.monotonic() + wait_s
    try:
        while True:
            shown = _systemctl("show", "-p", "LoadState", "-p", "ActiveState", "-p", "Result", unit)
            props = dict(line.partition("=")[::2] for line in shown.stdout.splitlines())
            if shown.returncode != 0 or props.get("LoadState") == "not-found":
                return None
            if props.get("ActiveState") in ("failed", "inactive") or time.monotonic() > deadline:
                break
            if _populated(unit, cgroup_root):
                break
            time.sleep(0.05)
        _systemctl("reset-failed", unit)
    except CleanupError:
        return None
    return props.get("Result") or None


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


_OOM_SCORE_ADJ = "/proc/self/oom_score_adj"


def oom_reset_code() -> str:
    """Python source, run in a contained command's process just before it becomes the command,
    that raises a negative `oom_score_adj` to 0 and never lowers one. A command started under a
    parent with `-1000` would otherwise be exempt from the OOM killer, so a memory limit on its
    scope could never end it. Raising needs no privilege. Linux only: elsewhere there is nothing
    to reset, and the source is empty."""
    if not sys.platform.startswith("linux"):
        return ""
    return (
        "try:\n"
        f" f=open({_OOM_SCORE_ADJ!r},'r+')\n"
        " if int(f.read())<0: f.seek(0); f.write('0'); f.flush()\n"
        "except (OSError,ValueError): pass\n"
    )


#: Run in the scope by `spawn`: with `whole` as its second argument, set the scope's
#: `memory.oom.group` where it can, so the OOM killer ends every process in it together; say
#: the scope exists on the pipe it is handed; reset the OOM score (`oom_reset_code`); then
#: become the command. Exit 127, as a shell would, when the command cannot be run.
_TRAMPOLINE = (
    "import os,sys\n"
    "try:\n"
    " if sys.argv[2]=='whole':\n"
    "  g=open('/proc/self/cgroup').read().split('0::',1)[1].split('\\n')[0]\n"
    "  open('/sys/fs/cgroup'+g+'/memory.oom.group','w').write('1')\n"
    "except (OSError,IndexError): pass\n"
    "fd=int(sys.argv[1]); os.write(fd,b'x'); os.close(fd)\n"
    + oom_reset_code()
    + "try: os.execvp(sys.argv[3],sys.argv[3:])\n"
    "except OSError as e: sys.stderr.write(f'{sys.argv[3]}: {e}\\n'); os._exit(127)\n"
)
#: How long `spawn` waits for a scope before it gives up on it.
SCOPE_TIMEOUT_S = 30.0


class Cancelled(Exception):
    """`spawn` stopped waiting for a scope because its `cancel_fd` turned readable: the command
    has not run, and what was started for it has been killed."""


def spawn(
    argv: Sequence[str],
    prefix: Sequence[str] | None,
    *,
    cancel_fd: int | None = None,
    whole: bool = False,
    **popen,
) -> tuple[subprocess.Popen, str | None]:
    """Start `argv` (with `subprocess.Popen`'s `popen` arguments) in the scope `prefix` makes,
    or as is when there is no prefix; with `whole`, a scope the OOM killer ends whole
    (`memory.oom.group`). When the scope cannot be made, the command has not run, and it is
    started as is instead. When `cancel_fd` turns readable before the scope is made,
    raises `Cancelled`. The process, and why it is not contained when a scope was asked for and
    could not be made (None otherwise)."""
    if not prefix:
        return subprocess.Popen(list(argv), **popen), None
    read_end, write_end = os.pipe()
    mode = "whole" if whole else "-"
    trampoline = [sys.executable, "-I", "-S", "-c", _TRAMPOLINE, str(write_end), mode, *argv]
    original_pass_fds = popen.pop("pass_fds", ())
    try:
        passed = (*original_pass_fds, write_end)
        child = subprocess.Popen([*prefix, *trampoline], pass_fds=passed, **popen)
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
    return subprocess.Popen(list(argv), pass_fds=original_pass_fds, **popen), why
