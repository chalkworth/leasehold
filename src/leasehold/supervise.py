"""A command run under a lease (`leasing.Lease.popen`), from its start to the end of its process
group.

* The command leads a process group of its own: in a session of its own, or with `tty` in this
  terminal's session, for a caller that hands it the terminal (`hold`).
* It gets the lease's environment (`leasing.child_env`): its `LEASEHOLD_*` variables and, with
  `job_env`, job and thread counts for the weight granted. Unleased, it gets the caller's
  environment without `LEASEHOLD_WANT`, and nothing else below applies.
* Where the daemon allows it and the host can (`leasehold.host`; on Linux, for a caller in the
  user's own session), it runs in a kernel group sized to the lease, a systemd user scope;
  `contain=False` runs it as is.
* With `jobserver`, it gets a make jobserver of the lease's job count, and a range is elastic
  unless `elastic` is false: once its jobs take tokens, the lease's jobserver and scope follow
  the lease's target (`leasehold.follow`), stepped from the lease's renewal thread.
* A revoked `lend` lease, or one lost to its TTL, stops the group: SIGTERM, then SIGKILL after
  the grace. A `work` or `external` lease lost for good leaves the command running, unleased.
* Once the command ends, whatever it left in its group is stopped the same way.
* A contained command is measured while it runs (`leasehold.measure`): once it ends, what it
  used, with the lease's rule and the git repository it ran in, is the outcome the lease sends
  when it is released (`leasing.Lease.release`). An uncontained one is not measured.
* Once a contained command ends, `oom_killed` says whether the kernel's OOM killer ended any
  of its processes: its scope counted an OOM kill, or it ended by SIGKILL while the machine
  counted one. `peak_bytes` is the most memory its scope used.
* With a hard cap (`memory_max`: a size such as `8g` or a share of the machine's memory such
  as `50%`; else the cap the grant names, `[commands] memory_max`; `off` for none), a contained
  command's scope is capped through `leasehold.host`: past the cap the kernel kills the whole
  command. A cap that cannot be enforced here (no scope, or a platform with no cap) is said
  in `uncapped`, never left silent.
* Once a contained command ends, a process it left running in its scope (a build server) loses
  the lease's limits (`Host.release`), and `say` hears so.
* Once any command ends, `memory` is the memory verdict on it (`leasehold.verdict`), or None
  when memory did not end it as far as leasehold can tell: `certain` when its scope counted
  the OOM kill, `likely` when it ended by SIGKILL while the machine's count rose
  (`leasehold.host`, which knows of no kills off Linux).

The host starts the command and reaps it (`leasehold.host.Started`). No signal handler is
installed: a caller that wants signals passed on sends them to `pid`'s group.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from leasehold import contain, follow, host, jobs, measure, verdict
from leasehold.host import group_alive, signal_group
from leasehold.kinds import PREEMPTIBLE
from leasehold.leasing import Lease, own_env
from leasehold.units import CAP_OFF, memory_to_bytes, size

__all__ = ["Command", "NotStarted", "group_alive", "jobserver_env", "signal_group"]

#: How often `Command.wait` looks at the lease while the command runs.
_POLL_S = 0.1
#: How often a capped command's scope is read (`measure.Meter`).
CAP_PERIOD_S = 0.25


class NotStarted(Exception):
    """The command could not be started; nothing of it runs."""


def cap_bytes(asked: str | None, contain_settings: Mapping) -> int | None:
    """The hard cap on a command, in bytes: `asked` (a size, a share of this machine's memory
    such as `50%`, or `off`), else the grant's `memory_max`; None for none."""
    value = asked if asked is not None else contain_settings.get("memory_max")
    if value is None or value == CAP_OFF:
        return None
    if value.endswith("%"):
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return max(1, int(total * float(value[:-1]) / 100))
    return memory_to_bytes(value)


def _no_scope(settings: Mapping, found: host.Support) -> str | None:
    """Why a lease's command gets no kernel group here: the daemon turned containment off
    (`[contain] mode`, from the grant's `contain` `settings`), or the host cannot make one for
    this caller (`found`, its support for `start`). None when it gets one."""
    if settings.get("mode", "auto") == "off":
        return 'the daemon has [contain] mode = "off"'
    return found.reason if found.how == "none" else None


def jobserver_env(env: Mapping[str, str], count: int, fifo: Path) -> dict[str, str]:
    """`MAKEFLAGS` naming the jobserver, and the same in `CARGO_MAKEFLAGS` and `MFLAGS` where
    they are set (cargo reads those first, so an outer build's would win)."""
    flags = jobs.makeflags(env.get("MAKEFLAGS"), count, fifo)
    out = {"MAKEFLAGS": flags}
    for name in ("CARGO_MAKEFLAGS", "MFLAGS"):
        if env.get(name):
            out[name] = jobs.makeflags(env[name], count, fifo)
    return out


def _findable(program: str, env: Mapping[str, str], cwd: str | None) -> bool:
    """Whether `program` names an executable from `cwd` (default ours) with `env`'s PATH, as
    the command would find it. True where `cwd` cannot be known: the check is left to the run."""
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            return True
    if "/" in program:
        found = os.path.join(cwd, program)
        return os.path.isfile(found) and os.access(found, os.X_OK)
    dirs = [os.path.join(cwd, d) for d in os.get_exec_path(env)]
    return shutil.which(program, path=os.pathsep.join(dirs)) is not None


class Command:
    """`argv` run under `lease`, started at once. `process` is its `subprocess.Popen` and `pid`
    its process id, which leads its group;
    `uncontained` says why it runs without the scope it was to have (None: it has it, or none
    was asked for); `stopped_for` is why the lease stopped it (None: it was not). `memory` (a
    size such as `4g`), or `memory_per_slot` for each slot granted, holds its scope's memory.
    `memory_max` is its hard cap (a size, a share of the machine's memory such as `50%`, or
    `off`; None: the cap the grant names, if any); `uncapped` says why a cap asked for does not
    hold (None: it holds, or none was asked for).
    `say` hears what the follower and the stop have to tell. Raises `NotStarted`, or
    `host.Cancelled` when `cancel_fd` turns readable while its scope is made.

    Once it has ended, `oom_killed` says whether the OOM killer ended any of its processes and
    `peak_bytes` is the most memory its scope used (None: unknown). Both are known only for a
    contained command: an uncontained one is never `oom_killed`. `memory` is the memory
    verdict on any command once it has ended (`leasehold.verdict`; None: memory did not end
    it, as far as leasehold can tell)."""

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
        memory_max: str | None = None,
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
        self.uncapped: str | None = None
        self.returncode: int | None = None
        self.oom_killed = False
        self.peak_bytes: int | None = None
        self.memory: verdict.Verdict | None = None
        self._program = Path(argv[0]).name if argv else ""
        self._host = host.current()
        self._cap: int | None = None
        self._say = say
        self._meter: measure.Meter | None = None
        self._repo: str | None = None
        self._stack = contextlib.ExitStack()
        try:
            self._start(
                list(argv), jobserver, elastic, contain, job_env, threads, memory,
                memory_per_slot, memory_max, env, tty, cancel_fd, popen,
            )  # fmt: skip
        except BaseException:
            self._stack.close()
            raise

    def _start(
        self, argv, jobserver, elastic, contained, job_env, threads, memory, per_slot, most_mem,
        env, tty, cancel_fd, popen,
    ) -> None:  # fmt: skip
        lease = self.lease
        server = spec = None
        cap = why = None
        if not lease.held:
            env = own_env(env)
            elastic = False
            cap = cap_bytes(most_mem, {})
            why = "it runs without a lease, so in no scope"
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
            settings = lease._answer.get("contain") or {}
            cap = cap_bytes(most_mem, settings)
            capped = cap
            if cap is not None:
                found = self._host.support("memory_hard")
                if found.how == "none":
                    why, capped = found.reason, None
            if contained:
                no_scope = _no_scope(settings, self._host.support("start"))
                if no_scope is None:
                    spec = host.TreeSpec(
                        contain.lease_unit(lease.lease_id), contain.lease_slice(),
                        weight=weight, memory=memory, cap_bytes=capped,
                        hard_cpu=bool(settings.get("hard_cpu")),
                    )  # fmt: skip
                elif cap is not None and why is None:
                    why = no_scope
            elif cap is not None:
                why = "it was started with contain=False"
            if spec and not _findable(argv[0], env, popen.get("cwd")):
                # Said here: under systemd-run a missing program would be its error, not ours.
                raise NotStarted(f"cannot run {argv[0]}: not found or not executable")
        group = {"process_group": 0} if tty else {"start_new_session": True}
        started = time.monotonic()
        try:
            self.started = self._host.start(
                argv, spec, env=env, cancel_fd=cancel_fd, **group, **popen
            )
        except OSError as exc:
            raise NotStarted(f"cannot run {argv[0]}: {exc}") from exc
        self.process, self.uncontained = self.started.process, self.started.uncontained
        self.pid = self.process.pid
        if cap is not None:
            why = why or self.uncontained
            if why:
                self.uncapped = f"no memory cap of {size(cap)} on {self._program}: {why}"
            else:
                self._cap = cap
        if spec and not self.uncontained:
            self._repo = measure.repository(popen.get("cwd"))
            running = lambda: self.started.poll() is None  # noqa: E731
            # A capped command is read more often, to name its largest process if it is killed.
            period = CAP_PERIOD_S if self._cap is not None else measure.PERIOD_S
            self._meter = measure.Meter(
                self._host, self.started.tree, started=started, running=running, period_s=period
            )
            self._stack.callback(self._meter.stop)
        if lease.held and lease.kind in PREEMPTIBLE:
            watcher = threading.Thread(
                target=self._stop_when_lost, name=f"leasehold-watch-{self.pid}", daemon=True
            )
            watcher.start()
        if elastic and server is not None:
            settings = lease._answer.get("contain") or {}
            scoped = spec and not self.uncontained
            follower = follow.Follower(
                lease._quick, lease.lease_id, granted, server=server,
                tree=self.started.tree if scoped else None, platform=self._host,
                memory_per_slot=per_slot, hard_cpu=bool(settings.get("hard_cpu")), say=self._say,
            )  # fmt: skip
            settle_s = (lease._answer.get("elastic") or {}).get("settle_s")
            lease._follow(follower, settle_s)
            self._stack.callback(lease._follow, None, None)

    def _stop_when_lost(self) -> None:
        """Stop the group once the lease is lost, whether or not anyone waits for the command."""
        while self.started.poll() is None:
            if self.lease._lost_event.wait(_POLL_S):
                if self.started.poll() is None:
                    self.stopped_for = self.lease.lost
                    self._say(f"lease revoked ({self.stopped_for}); stopping the command")
                    self.started.kill(self.grace_s)
                return

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the command to end, or for the lease to stop it, then stop whatever it left
        in its group: its exit status (negative: the signal that ended it). Raises
        `subprocess.TimeoutExpired` when `timeout` passes first."""
        if self.returncode is not None:
            return self.returncode
        code = self.started.wait(timeout=timeout)
        self._end(code)
        return code

    def _end(self, code: int) -> None:
        largest = None
        if self._meter is None:
            ending = self.started.ending()
        else:
            wall_s = self._meter.stop()
            usage = self._meter.usage
            self.lease._outcome = {
                "rule": self.lease._rule,
                "repo": self._repo,
                **dataclasses.asdict(usage),
                "wall_s": round(wall_s, 3),
                "exit_status": code,
            }
            self.peak_bytes = usage.peak_bytes
            # Certain when its own scope counted the kill, or systemd says its cap ended it.
            ending = self.started.ending(counted=usage.oom_kills)
            self.oom_killed = ending.confidence == "certain" or (
                code == -signal.SIGKILL and (ending.kills or 0) > 0
            )
            self.lease.oom_killed = self.oom_killed
            # Named only when it held most of what was capped: otherwise it was read too early.
            seen = self._meter.largest
            if self._cap is not None and seen is not None and seen[2] * 2 >= self._cap:
                largest = seen
        stop = None
        if code in (-signal.SIGKILL, 128 + signal.SIGKILL):
            stop = self.lease.stopped_by_watchdog()
            if stop is not None:
                ending = host.Ending("leasehold", "certain", how="exact")
                self.oom_killed = self.lease.oom_killed = False
        self.memory = verdict.describe(
            ending,
            self._program,
            status=code,
            peak_bytes=self.peak_bytes,
            limit_bytes=ending.limit_bytes,
            agent_limit=os.environ.get(verdict.AGENT_LIMIT_ENV) or None,
            largest=largest,
            watchdog=stop,
        )
        self.lease.memory = self.memory
        if group_alive(self.pid):
            self.started.kill(self.grace_s)
        if self._meter is not None and (left := self._meter.left()):
            pid, name, used = left[0]
            done = self._host.release(self.started.tree)
            self._say(
                f"{self._program} left {name} running (pid {pid}, {size(used)}) in its scope; "
                + (done.reason if done.how == "none" else "it no longer has the lease's limits")
            )
        self.returncode = code
        self._stack.close()

    def close(self) -> None:
        """Stop the command's group, if it still runs, and what it holds (its jobserver)."""
        if self.returncode is None:
            self._end(self.started.kill(self.grace_s))

    def __enter__(self) -> Command:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
