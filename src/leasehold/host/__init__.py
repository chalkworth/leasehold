"""What leasehold asks of the operating system, behind one interface: `current()` is the `Host`
for this platform. Nothing else in leasehold starts, measures, pauses or stops a tree of
processes, or reads the machine's memory pressure, by itself.

A **tree** is a command and everything it starts, kept together so that it can be measured,
paused and stopped as one: its own process group always, and a kernel group where the platform
has one (a systemd scope on Linux). The operations:

* `start(argv, spec)` starts a command as the root of a new tree (`Started`), and `adopt(pids,
  spec)` makes a tree of processes that are already running (`Adoption`). `join(tree, pids)`
  adds more running processes to a tree that exists. `resize` sizes a tree to a lease of a new
  weight, and `release` takes a lease's limits off a tree that outlives its lease.
* `members(tree)` are its live processes and `footprint(tree)` what it costs the machine in
  memory; `pressure()` is one reading of the machine's memory; `session_processes(session)` is
  everything an agent session started, and `named(names)` the user's processes whose program is
  one of `names` (how agent sessions nobody registered are found). `command_line(proc)` is the
  command a process runs, as one line.
* `pause(tree)` stops every process in it from running, and `resume(tree)` lets them run again.
  A tree's `keeper` (the process that started it and waits for it) is never paused. `kill(tree,
  grace_s)` sends SIGTERM, and SIGCONT so that a paused member sees it, then SIGKILL to what is
  left after `grace_s`; with no grace, SIGKILL alone, at once (a stopped process does not act
  on SIGTERM until it runs again).
* `stay_resident(dry=False)` keeps the calling process's memory in RAM and raises its priority
  where this user may, so that a watchdog still runs when the machine is short of memory; with
  `dry`, it only says whether it could.
* `mark()` takes the counters before a command starts, and `ended(status, since)` gives the
  memory verdict on how it ended (`Ending`). For a command it started, the host owns the wait:
  `Started.wait()` and `Started.poll()` reap it, so that a platform that must read how a
  process ended before it is reaped can, and `Started.ending()` is the verdict.
* `capabilities()` says, for each operation, whether it works here and how (`Support`), and why
  not when it does not; `support(operation)` says it for one, without asking about the others.
  `memory_max(pid)` is the hard memory limit a process runs under, where the platform has one.

Every answer says how it was measured or done (`how`): `exact` when the kernel counted or did
it for the whole tree, `polled` when leasehold read or signalled each process in turn, and
`none` when this platform cannot, and then why (`reason`). No message claims more than the
platform gave.

Processes are fenced: a `Proc` and a `Tree` carry a `fence`, an opaque token the platform gives
each process (on Linux its start time), so that a pid the kernel has given to a new process is
never measured, paused or killed in place of the one that ended.

`linux` uses systemd user scopes, cgroup v2 and procfs where they are there (`systemd` holds the
scope mechanics). `posix` is the fallback for every other system, and the base of `linux`: it
starts commands in a process group of their own, signals that group, and says of everything
else that it is not supported here, and why.

This package imports only leasehold's leaf modules, and never the daemon. Its platform modules
are imported only from inside it (`.importlinter`).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Collection
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Protocol

#: The seconds over which `Pressure.stall_full_recent` is measured, at least.
STALL_WINDOW_S = 2.0

#: The operations `capabilities` reports on, in the order `doctor` lists them. `memory_hard` is
#: a hard cap on a tree's memory, given to `start` or `adopt` in `TreeSpec.cap_bytes`.
OPERATIONS = (
    "start",
    "adopt",
    "members",
    "footprint",
    "pressure",
    "pause",
    "resume",
    "kill",
    "ended",
    "memory_hard",
    "session_processes",
)


@dataclass(frozen=True)
class Support:
    """Whether an operation works here, and how. `how` is `exact` when the kernel does it for
    the whole tree, `polled` when leasehold does it process by process, and `none` when it
    cannot be done here, and then `reason` says why. `via` names the mechanism."""

    how: str
    reason: str | None = None
    via: str | None = None

    def describe(self) -> str:
        """The answer in a few words: `exact (systemd user scope)`, or `none: <reason>`."""
        if self.how == "none":
            return f"none: {self.reason}"
        return f"{self.how} ({self.via})" if self.via else self.how


@dataclass(frozen=True)
class Proc:
    """One live process. `fence` is the platform's token for it (None: the platform has none),
    `name` its program, `bytes` its resident and swapped memory (None: not read), `anon` the
    anonymous memory it has resident and `swap` what it has in swap (None: not read), `parent`
    its parent's pid and `parent_fence` that parent's token as read now, and `started` when it
    started, in wall-clock seconds (None: not known). In `session_processes`,
    `role` is `agent`, `tool`, `lease` or `orphan`, and `basis` how it was found to belong to
    the session: `slice`, `lineage` or `env`."""

    pid: int
    fence: str | None = None
    name: str = ""
    bytes: int | None = None
    parent: int | None = None
    parent_fence: str | None = None
    role: str | None = None
    basis: str | None = None
    anon: int | None = None
    swap: int | None = None
    started: float | None = None
    #: In `session_processes`, the kernel group it runs in (a cgroup path) and the lease its
    #: environment names (`LEASEHOLD_LEASE_ID`); None: none, or not read.
    group: str | None = None
    lease: str | None = None


@dataclass(frozen=True)
class Tree:
    """A command and what it starts. `root` is the pid of its first process (0 for a tree known
    by its kernel group alone) and `fence` that process's token; `pgid` the process group
    signalled as one (None: none of its own); `keeper` the process that waits for it, which
    `pause` never stops. Where it has a kernel group, `unit` is that group's name to the
    service manager (a systemd unit) and `group` where the kernel keeps it (a cgroup path,
    such as `/user.slice/.../lease-x.scope`).
    `cap_bytes` is its own hard memory cap, and `limit_bytes` the lowest hard memory limit on
    it or above it as read when it was made (None: none)."""

    root: int
    fence: str | None = None
    pgid: int | None = None
    keeper: int | None = None
    unit: str | None = None
    group: str | None = None
    cap_bytes: int | None = None
    limit_bytes: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Tree:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class TreeSpec:
    """The tree `start` or `adopt` makes: the kernel group `unit` (a systemd scope) in `slice_`,
    sized to a lease of `weight` slots, with `memory` (a size such as `4g`) as its soft limit,
    `cap_bytes` as its hard cap when given, and `hard_cpu` for a CPU quota of `weight` CPUs."""

    unit: str
    slice_: str
    weight: int = 1
    memory: str | None = None
    cap_bytes: int | None = None
    hard_cpu: bool = False


@dataclass(frozen=True)
class Adoption:
    """What `adopt` did. `how` is `exact` when the processes are now in a kernel group of
    their own (`unit`, and `tree`), else `none`, and then why (`reason`). `limit_bytes` is the
    lowest hard memory limit above that group (None: none), and `capped` whether `cap_bytes`
    holds it."""

    how: str
    unit: str | None = None
    reason: str | None = None
    limit_bytes: int | None = None
    capped: bool = False
    tree: Tree | None = None


@dataclass(frozen=True)
class Footprint:
    """What a tree costs the machine in memory now. `current` is what it holds (without file
    cache the kernel can reclaim) plus what it has in swap. The most it held is a range:
    `peak_low` is the most seen by reading (a lower bound), and `peak_high` the most the
    platform says it held (`memory.peak` on Linux, equal to `peak_low`; None: not known).
    `anon` is its anonymous memory, `swap` the most swap it used (for a `polled` tree, the swap
    its processes hold now), and `largest` its largest process. `oom_kills` and `high_events`
    are what its kernel group counted (OOM kills, and passes over its soft limit) and
    `cpu_usec` the CPU time it used. `how` is `exact` when the kernel counted it for the whole
    tree, `polled` for a sum of its processes' readings, and `none` with a `reason` when
    nothing could be read. Each value is None when not read."""

    how: str = "none"
    reason: str | None = None
    current: int | None = None
    peak_low: int | None = None
    peak_high: int | None = None
    anon: int | None = None
    swap: int | None = None
    largest: Proc | None = None
    oom_kills: int | None = None
    high_events: int | None = None
    cpu_usec: int | None = None


@dataclass(frozen=True)
class Pressure:
    """One reading of the machine's memory. `available` is what the machine can still give
    without paging hard, of `total`; `swap_used` of `swap_total`; `level` the OS's own scale
    (`ok`, `warn` or `critical`; None where it has none, as on Linux); `stall` and `stall_full`
    the share of the last ten seconds tasks stalled on memory, measured as `stall_how` says
    (`psi` on Linux); `stall_full_recent` the share of the last `STALL_WINDOW_S` seconds or so
    in which every task stalled (from the kernel's running total, so it does not lag as the
    ten-second average does; None on this host's first reading); `swapins_per_s` the pages
    read back from swap a second since the last reading by this host (None on the first);
    `compressed` the memory held compressed (zswap). `swap_kind` is what the swap is: `disk`
    (a file or partition, whose free space is free memory), `zram` (compressed RAM, which an
    incompressible page fills at about one to one) or `mixed`; None with no swap.
    `zram_ratio` is how much zram's pages shrank (their size over the RAM zram uses for
    them; None without zram, or before it holds anything). Shares are percentages. `how` is `exact`
    when the kernel gave it, `none` with a `reason` when nothing could be read. Each value is
    None when not known."""

    how: str = "none"
    reason: str | None = None
    available: int | None = None
    total: int | None = None
    swap_used: int | None = None
    swap_total: int | None = None
    level: str | None = None
    stall: float | None = None
    stall_full: float | None = None
    stall_full_recent: float | None = None
    stall_how: str | None = None
    swapins_per_s: float | None = None
    compressed: int | None = None
    swap_kind: str | None = None
    zram_ratio: float | None = None


@dataclass(frozen=True)
class Done:
    """What `pause`, `resume`, `kill`, `join`, `resize` or `release` did. `how` names the
    mechanism (`freeze`, `sigstop`, `sigcont`, `systemd`, `signal`), or is `none` when nothing
    was done, and then why (`reason`). `empty` is True when the tree had no process left to act
    on."""

    how: str
    reason: str | None = None
    empty: bool = False


@dataclass(frozen=True)
class SessionRef:
    """An agent session, for `session_processes`: its `id` (what `LEASEHOLD_SESSION` holds in
    its processes' environment), the agent's pid and that process's fence, and the kernel group
    the session runs in (`slice_`, a systemd slice), when it has one."""

    id: str
    agent_pid: int | None = None
    agent_fence: str | None = None
    slice_: str | None = None


@dataclass(frozen=True)
class Mark:
    """Counters taken before a command starts. `oom_kills` is how many processes the OOM killer
    had ended on this machine (None: not known here)."""

    oom_kills: int | None = None

    def encode(self) -> str:
        """The mark as one word, for a note kept between two processes (`decode`)."""
        return "-" if self.oom_kills is None else str(self.oom_kills)

    @classmethod
    def decode(cls, word: str | None) -> Mark:
        """The mark `encode` wrote; an empty mark for anything else."""
        return cls(int(word)) if word and word.isdigit() else cls()


@dataclass(frozen=True)
class Ending:
    """How a command ended, as far as memory goes. `memory` is `kernel` when the OOM killer
    ended it (or most likely did), `cap` when it ended it at the command's own hard cap, else
    None. `confidence` is `certain` or `likely` (None with no memory verdict). `kills` is how
    many processes the OOM killer ended on the machine while it ran (None: not known).
    `limit_bytes` is the limit it was killed at, when that is certain. `cause_detail` is the
    platform's own word for why (None where it gives none), and `sender` the process that sent
    the signal that ended it, as (pid, name), where the platform says. `how` and `reason` say
    how this was measured, and why not when `how` is `none`."""

    memory: str | None = None
    confidence: str | None = None
    kills: int | None = None
    how: str = "none"
    reason: str | None = None
    limit_bytes: int | None = None
    cause_detail: str | None = None
    sender: tuple[int, str] | None = None


class Cancelled(Exception):
    """`start` stopped waiting for a tree's kernel group because its `cancel_fd` turned
    readable: the command has not run, and what was started for it has been killed."""


class Host(Protocol):
    def support(self, operation: str) -> Support: ...

    def capabilities(self) -> dict[str, Support]: ...

    def start(
        self, argv, spec: TreeSpec | None, *, cancel_fd: int | None = None, **popen
    ) -> Started: ...

    def adopt(self, pids: list[int], spec: TreeSpec) -> Adoption: ...

    def join(self, tree: Tree, pids: list[int]) -> Done: ...

    def resize(self, tree: Tree, spec: TreeSpec) -> Done: ...

    def release(self, tree: Tree) -> Done: ...

    def members(self, tree: Tree) -> list[Proc]: ...

    def footprint(self, tree: Tree) -> Footprint: ...

    def pressure(self) -> Pressure: ...

    def pause(self, tree: Tree) -> Done: ...

    def resume(self, tree: Tree) -> Done: ...

    def kill(self, tree: Tree, grace_s: float) -> Done: ...

    def stay_resident(self, *, dry: bool = False) -> Done: ...

    def mark(self) -> Mark: ...

    def ended(
        self, status: int, since: Mark, *, tree: Tree | None = None, counted: int | None = None
    ) -> Ending: ...

    def memory_max(self, pid: int) -> int | None: ...

    def session_processes(self, session: SessionRef) -> list[Proc]: ...

    def named(self, names: Collection[str]) -> list[Proc]: ...

    def command_line(self, proc: Proc) -> str | None: ...

    def wakeup_fd(self) -> int | None: ...


class Started:
    """A command `start` started: `process` (its `subprocess.Popen`, for its pipes) and `pid`,
    the `tree` it leads, the `mark` taken just before it started, and `uncontained`, why it has
    no kernel group although one was asked for (None: it has one, or none was asked for).

    Reap it with `wait` or `poll`, never through `process`: the host may need to read how it
    ended before it is reaped. Once it has ended, `status` is its exit status (negative: the
    signal that ended it) and `ending` the memory verdict on it."""

    def __init__(
        self,
        host: Host,
        process: subprocess.Popen,
        tree: Tree,
        mark: Mark,
        uncontained: str | None = None,
    ) -> None:
        self.host = host
        self.process = process
        self.pid = process.pid
        self.tree = tree
        self.mark = mark
        self.uncontained = uncontained
        self.status: int | None = None

    def poll(self) -> int | None:
        """Its exit status once it has ended, reaped; None while it runs."""
        code = self.process.poll()
        if code is not None:
            self.status = code
        return code

    def wait(self, timeout: float | None = None) -> int:
        """Wait for it to end, and reap it: its exit status. Raises
        `subprocess.TimeoutExpired` when `timeout` passes first."""
        self.status = self.process.wait(timeout=timeout)
        return self.status

    def alive(self) -> bool:
        """Whether anything of its process group is left (its reaped leader included)."""
        return self.tree.pgid is not None and group_alive(self.tree.pgid)

    def kill(self, grace_s: float) -> int:
        """SIGTERM its process group, and SIGCONT so that a paused member sees it; wait up to
        `grace_s` for the group to go, then SIGKILL whatever is left. Its exit status."""
        pgid = self.tree.pgid if self.tree.pgid is not None else self.pid
        signal_group(pgid, signal.SIGTERM)
        signal_group(pgid, signal.SIGCONT)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if self.poll() is not None and not group_alive(pgid):
                break
            time.sleep(0.1)
        signal_group(pgid, signal.SIGKILL)
        return self.wait()

    def ending(self, counted: int | None = None) -> Ending:
        """Once it has ended: the memory verdict on it (`Host.ended`). `counted` is how many OOM
        kills its kernel group counted while it ran (`Footprint.oom_kills`, as last read: the
        group can be gone by now)."""
        if self.status is None:
            raise RuntimeError("the command has not been reaped")
        return self.host.ended(self.status, self.mark, tree=self.tree, counted=counted)


def group_alive(pgid: int) -> bool:
    """Whether any process is left in process group `pgid` (one of another user's counts)."""
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


def killed_by(status: int) -> int | None:
    """The signal that ended a command with `status`: negative as `subprocess` reports it, or
    128 and the signal as a shell reports it. None for a plain exit."""
    if status < 0:
        return -status
    if 128 < status < 128 + 65:
        return status - 128
    return None


def sigkilled(status: int) -> bool:
    return killed_by(status) == signal.SIGKILL


def current(
    *,
    env: dict | None = None,
    proc: Path | None = None,
    cgroup_root: Path | None = None,
) -> Host:
    """The host for this platform. `env` is the environment the caller's commands start with
    (default ours); `proc` and `cgroup_root` the roots a Linux host reads (default `/proc` and
    `/sys/fs/cgroup`), which tests point at fixture trees."""
    if sys.platform.startswith("linux"):
        from leasehold.host import linux

        roots = {"proc": proc, "cgroup_root": cgroup_root}
        return linux.Linux(env=env, **{k: v for k, v in roots.items() if v is not None})
    from leasehold.host import posix

    return posix.Posix(env=env)
