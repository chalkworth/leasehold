"""A command run under a lease (`leasing.Lease.popen`), from its start to the end of its process
group.

* The command leads a process group of its own: in a session of its own, or with `tty` in this
  terminal's session, for a caller that hands it the terminal (`hold`).
* It gets the lease's environment (`leasing.child_env`): its `LEASEHOLD_*` variables and, with
  `job_env`, job and thread counts for the weight granted. Unleased, it gets the caller's
  environment without `LEASEHOLD_WANT`, and nothing else below applies.
* Where the daemon allows it and the caller runs in the user's own session, it runs in a systemd
  user scope sized to the lease (`leasehold.contain`); `contain=False` runs it as is.
* With `jobserver`, it gets a make jobserver of the lease's job count, and a range is elastic
  unless `elastic` is false: once its jobs take tokens, the lease's jobserver and scope follow
  the lease's target (`leasehold.follow`), stepped from the lease's renewal thread.
* A revoked `lend` lease, or one lost to its TTL, stops the group: SIGTERM, then SIGKILL after
  the grace. A `work` or `external` lease lost for good leaves the command running, unleased.
* Once the command ends, whatever it left in its group is stopped the same way.

No signal handler is installed: a caller that wants signals passed on sends them to `pid`'s
group.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from leasehold import contain, follow, jobs
from leasehold.kinds import PREEMPTIBLE
from leasehold.leasing import Lease, own_env
from leasehold.units import memory_to_bytes

__all__ = ["Command", "NotStarted", "group_alive", "jobserver_env", "scope_prefix"]

#: How often `Command.wait` looks at the lease while the command runs.
_POLL_S = 0.1


class NotStarted(Exception):
    """The command could not be started; nothing of it runs."""


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def stop_group(child: subprocess.Popen, grace_s: float) -> int:
    """SIGTERM the group `child` leads, wait up to `grace_s`, then SIGKILL whatever is left. The
    leader's exit status."""
    pgid = child.pid
    signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if child.poll() is not None and not group_alive(pgid):
            break
        time.sleep(0.1)
    signal_group(pgid, signal.SIGKILL)
    return child.wait()


def scope_prefix(lease_id: str, weight: int, settings: dict, memory: str | None) -> list | None:
    """The `systemd-run` prefix for a lease's command, or None where it runs as is: the daemon
    turned containment off (`[contain] mode`, from the grant's `contain` `settings`), or
    `contain.probe` finds no user scope this caller may use."""
    if settings.get("mode", "auto") == "off" or not contain.probe().enforced:
        return None
    return contain.lease_argv(
        lease_id, weight=weight, memory=memory, hard_cpu=bool(settings.get("hard_cpu"))
    )


def jobserver_env(env: Mapping[str, str], count: int, fifo: Path) -> dict[str, str]:
    """`MAKEFLAGS` naming the jobserver, and the same in `CARGO_MAKEFLAGS` and `MFLAGS` where
    they are set (cargo reads those first, so an outer build's would win)."""
    flags = jobs.makeflags(env.get("MAKEFLAGS"), count, fifo)
    out = {"MAKEFLAGS": flags}
    for name in ("CARGO_MAKEFLAGS", "MFLAGS"):
        if env.get(name):
            out[name] = jobs.makeflags(env[name], count, fifo)
    return out


class Command:
    """`argv` run under `lease`, started at once. `process` is its `subprocess.Popen` and `pid`
    its process id, which leads its group;
    `uncontained` says why it runs without the scope it was to have (None: it has it, or none
    was asked for); `stopped_for` is why the lease stopped it (None: it was not). `memory` (a
    size such as `4g`), or `memory_per_slot` for each slot granted, holds its scope's memory.
    `say` hears what the follower and the stop have to tell. Raises `NotStarted`, or
    `contain.Cancelled` when `cancel_fd` turns readable while its scope is made."""

    def __init__(
        self,
        lease: Lease,
        argv: Sequence[str],
        *,
        jobserver: bool = False,
        elastic: bool | None = None,
        contain: bool = True,
        grace_s: float = 30.0,
        job_env: bool = True,
        threads: jobs.Threads = "jobs",
        memory: str | None = None,
        memory_per_slot: str | None = None,
        env: Mapping[str, str] | None = None,
        tty: bool = False,
        cancel_fd: int | None = None,
        say: Callable[[str], object] = lambda _: None,
        **popen,
    ) -> None:
        if not argv:
            raise NotStarted("no command given")
        self.lease = lease
        self.grace_s = grace_s
        self.stopped_for: str | None = None
        self.uncontained: str | None = None
        self.returncode: int | None = None
        self._say = say
        self._stack = contextlib.ExitStack()
        try:
            self._start(
                list(argv), jobserver, elastic, contain, job_env, threads, memory,
                memory_per_slot, env, tty, cancel_fd, popen,
            )  # fmt: skip
        except BaseException:
            self._stack.close()
            raise

    def _start(
        self, argv, jobserver, elastic, contained, job_env, threads, memory, per_slot, env, tty,
        cancel_fd, popen,
    ) -> None:  # fmt: skip
        lease = self.lease
        server = prefix = None
        if not lease.held:
            env = own_env(env)
            elastic = False
        else:
            granted = dict(lease._granted)
            weight = int(granted["weight"])
            if memory is None and per_slot is not None:
                memory = f"{weight * memory_to_bytes(per_slot)}b"
            ranged = int(granted.get("max_weight") or 0) > int(granted.get("min_weight") or 0)
            elastic = (jobserver if elastic is None else elastic) and jobserver and ranged
            most = int(granted.get("max_jobs") or lease.jobs) if elastic else None
            env = lease.env(env, job_env=job_env, threads=threads, elastic_jobs=most)
            if jobserver:
                try:
                    server = self._stack.enter_context(jobs.Jobserver(lease.jobs))
                except OSError as exc:
                    raise NotStarted(f"cannot make a jobserver: {exc}") from exc
                env |= jobserver_env(env, lease.jobs, server.fifo)
            if contained:
                settings = lease._answer.get("contain") or {}
                prefix = scope_prefix(lease.lease_id, weight, settings, memory)
            if prefix:
                cwd = popen.get("cwd")
                if cwd is None:
                    try:
                        cwd = os.getcwd()
                    except OSError:
                        cwd = None
                path = env.get("PATH") or os.defpath
                # Resolve relative PATH entries and argv[0] relative to cwd
                resolved_dirs = []
                for d in path.split(":"):
                    if d == "":
                        d = cwd if cwd is not None else ""
                    elif cwd is not None and not os.path.isabs(d):
                        d = os.path.join(cwd, d)
                    resolved_dirs.append(d)
                resolved_path = ":".join(resolved_dirs)
                # Determine if argv[0] is a path (contains a separator)
                if "/" in argv[0] or (os.path.sep in argv[0]):
                    # Resolve relative to cwd; when cwd is unknown (the caller gave none and
                    # os.getcwd() failed, as for a deleted directory), skip the check and let
                    # systemd-run report a missing program.
                    if os.path.isabs(argv[0]):
                        abs_path = argv[0]
                    elif cwd is not None:
                        abs_path = os.path.join(cwd, argv[0])
                    else:
                        abs_path = None
                    if abs_path is not None and not (
                        os.path.isfile(abs_path) and os.access(abs_path, os.X_OK)
                    ):
                        raise NotStarted(f"cannot run {argv[0]}: not found or not executable")
                else:
                    # Search in resolved PATH
                    if shutil.which(argv[0], path=resolved_path) is None:
                        raise NotStarted(f"cannot run {argv[0]}: not found or not executable")
                # Said here: under systemd-run a missing program would be its error, not ours.
        group = {"process_group": 0} if tty else {"start_new_session": True}
        try:
            self.process, self.uncontained = contain.spawn(
                argv, prefix, env=env, cancel_fd=cancel_fd, **group, **popen
            )
        except OSError as exc:
            raise NotStarted(f"cannot run {argv[0]}: {exc}") from exc
        self.pid = self.process.pid
        if lease.held and lease.kind in PREEMPTIBLE:
            watcher = threading.Thread(
                target=self._stop_when_lost, name=f"leasehold-watch-{self.pid}", daemon=True
            )
            watcher.start()
        if elastic and server is not None:
            settings = lease._answer.get("contain") or {}
            scoped = prefix and not self.uncontained
            follower = follow.Follower(
                lease._quick, lease.lease_id, granted, server=server,
                scope=contain.lease_unit(lease.lease_id) if scoped else None,
                memory_per_slot=per_slot, hard_cpu=bool(settings.get("hard_cpu")), say=self._say,
            )  # fmt: skip
            settle_s = (lease._answer.get("elastic") or {}).get("settle_s")
            lease._follow(follower, settle_s)
            self._stack.callback(lease._follow, None, None)

    def _stop_when_lost(self) -> None:
        """Stop the group once the lease is lost, whether or not anyone waits for the command."""
        while self.process.poll() is None:
            if self.lease._lost_event.wait(_POLL_S):
                if self.process.poll() is None:
                    self.stopped_for = self.lease.lost
                    self._say(f"lease revoked ({self.stopped_for}); stopping the command")
                    stop_group(self.process, self.grace_s)
                return

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the command to end, or for the lease to stop it, then stop whatever it left
        in its group: its exit status (negative: the signal that ended it). Raises
        `subprocess.TimeoutExpired` when `timeout` passes first."""
        if self.returncode is not None:
            return self.returncode
        code = self.process.wait(timeout=timeout)
        self._end(code)
        return code

    def _end(self, code: int) -> None:
        if group_alive(self.pid):
            stop_group(self.process, self.grace_s)
        self.returncode = code
        self._stack.close()

    def close(self) -> None:
        """Stop the command's group, if it still runs, and what it holds (its jobserver)."""
        if self.returncode is None:
            self._end(stop_group(self.process, self.grace_s))

    def __enter__(self) -> Command:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
