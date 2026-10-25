"""What a contained command used, read from its scope's cgroup while it runs, and the repository it
ran in: the outcome a client sends with its release (`POST /v1/release`, `leasehold.outcomes`).

A transient scope is removed once its last process exits, so its files cannot be read after the
command ends. A `Meter` reads them from a thread of its own every `PERIOD_S` while the command
runs, and once more as it stops, and keeps the last value of each:

* `memory.peak`, the most memory the scope has used. It only grows, so the last read misses at
  most what the final `PERIOD_S` added. None where the kernel has no such file (before 5.19).
* `anon` from `memory.stat`, the most seen: anonymous memory, a lower bound on what the command
  needed, since `memory.peak` also counts page cache.
* `memory.swap.peak`, the most swap the scope used, or where the kernel has no such file (before
  6.5) the most `memory.swap.current` seen: memory of the command's that was out of RAM, which
  neither of the two above counts.
* `oom_kill` and `high` from `memory.events`: how many processes the kernel's OOM killer ended
  in the scope, and how often the scope went past its `MemoryHigh`.
* `usage_usec` from `cpu.stat`, the CPU time it used.
* the largest of its processes, by resident and swapped memory (`VmRSS` and `VmSwap` in
  `/proc/<pid>/status`), the largest seen at any read (`largest`), so that a kill can name it.

A value never read is None. The meter finds the scope from the command's own cgroup
(`/proc/<pid>/cgroup`), and reads nothing unless that is the scope it was told of.

The scope is removed as soon as an OOM kill empties it, often before the next read, so the
meter also counts the OOM kills on the whole machine (`oom_kill` in `/proc/vmstat`) from the
command's start to its end (`machine_oom_kills`): with a command that ended by SIGKILL, one of
them is likely its own.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from leasehold import cgroup

#: How often a `Meter` reads its scope's files.
PERIOD_S = 1.0


@dataclass(frozen=True)
class Usage:
    """What a scope used, as last read. Each is None until read."""

    peak_bytes: int | None = None
    anon_bytes: int | None = None
    swap_bytes: int | None = None
    oom_kills: int | None = None
    high_events: int | None = None
    cpu_usec: int | None = None


def _number(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def _fields(text: str | None) -> dict[str, int]:
    """A cgroup file of `name value` lines as a mapping; lines that are not one are skipped."""
    out = {}
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdigit():
            out[name] = int(value)
    return out


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def members(group: Path, proc: Path = Path("/proc")) -> list[tuple[int, str, int]]:
    """Each process in the cgroup directory `group` now (`cgroup.procs`), as (pid, name,
    bytes), its resident and swapped memory, largest first. A process that cannot be read is
    left out."""
    found = []
    for word in (_read(group / "cgroup.procs") or "").split():
        if not word.isdigit():
            continue
        status = _read(proc / word / "status") or ""
        kib = name = None
        for line in status.splitlines():
            key, _, value = line.partition(":")
            if key == "Name":
                name = value.strip()
            elif key in ("VmRSS", "VmSwap") and value.split() and value.split()[0].isdigit():
                kib = (kib or 0) + int(value.split()[0])
        if name is not None and kib is not None:
            found.append((int(word), name, kib << 10))
    return sorted(found, key=lambda m: -m[2])


def machine_oom_kills(proc: Path = Path("/proc")) -> int | None:
    """How many processes the OOM killer has ended on this machine since boot (`oom_kill` in
    `vmstat`); None where it cannot be read."""
    return _fields(_read(proc / "vmstat")).get("oom_kill")


def read(group: Path, last: Usage) -> Usage:
    """`last` updated with what can be read in the cgroup directory `group` now: each value
    read replaces the last one (for `anon_bytes` and `swap_bytes`, only when it is larger), and a
    value that cannot be read keeps the last one."""
    events = _fields(_read(group / "memory.events"))
    anon = _fields(_read(group / "memory.stat")).get("anon")
    swap = _number(_read(group / "memory.swap.peak"))
    if swap is None:
        swap = _number(_read(group / "memory.swap.current"))
    found = {
        "peak_bytes": _number(_read(group / "memory.peak")),
        "anon_bytes": anon if last.anon_bytes is None or (anon or 0) > last.anon_bytes else None,
        "swap_bytes": swap if last.swap_bytes is None or (swap or 0) > last.swap_bytes else None,
        "oom_kills": events.get("oom_kill"),
        "high_events": events.get("high"),
        "cpu_usec": _fields(_read(group / "cpu.stat")).get("usage_usec"),
    }
    return replace(last, **{k: v for k, v in found.items() if v is not None})


class Meter:
    """Reads the cgroup of process `pid` every `period_s` (default `PERIOD_S`), from a thread of
    its own, while that cgroup is the scope `unit`, until `stop`, or until `running` (when
    given) says the command has ended. `usage` is what it has read so far. `started` is when,
    on the `time.monotonic` clock, the command was started (default now). `machine_oom_kills`
    is how many OOM kills the machine had from its start to `stop` (None: unknown). `largest`
    is the largest of its processes seen at any read, as (pid, name, bytes) (None: none
    read)."""

    def __init__(
        self,
        pid: int,
        unit: str,
        *,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc: Path = Path("/proc"),
        started: float | None = None,
        running: Callable[[], bool] | None = None,
        period_s: float = PERIOD_S,
    ) -> None:
        self.usage = Usage()
        self.machine_oom_kills: int | None = None
        self.largest: tuple[int, str, int] | None = None
        self._period_s = period_s
        self._proc = proc
        self._oom_before = machine_oom_kills(proc)
        self._started = time.monotonic() if started is None else started
        self._running = running
        #: When the thread saw the command had ended; None: it has not.
        self._ended: float | None = None
        self._group: Path | None = None
        path = cgroup.own_cgroup(proc, str(pid))
        if path is not None and PurePosixPath(path).name == unit:
            self._group = cgroup_root / path.lstrip("/")
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"leasehold-meter-{pid}", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            self._sample()
            if self._running is not None and not self._running():
                self._ended = time.monotonic()
                return
            if self._stop.wait(self._period_s):
                return

    def _sample(self) -> None:
        if self._group is not None:
            self.usage = read(self._group, self.usage)
            found = members(self._group, self._proc)
            if found and (self.largest is None or found[0][2] > self.largest[2]):
                self.largest = found[0]

    def left(self) -> list[tuple[int, str, int]]:
        """What is still running in the scope, largest first (`members`): what the command
        left behind once it has ended."""
        return [] if self._group is None else members(self._group, self._proc)

    def stop(self) -> float:
        """Stop reading, after one last read: the seconds from the command's start to its end,
        as `running` showed it, or else to now. A command waited for late is counted to within
        `PERIOD_S` of its end."""
        self._stop.set()
        self._thread.join()
        self._sample()
        after = machine_oom_kills(self._proc)
        if self._oom_before is not None and after is not None:
            self.machine_oom_kills = after - self._oom_before
        return (self._ended or time.monotonic()) - self._started


def _is_repository(entry: Path) -> bool:
    """Whether `entry`, a directory's `.git`, is one a repository uses: a directory that holds
    a `HEAD`, or the file a linked work tree or submodule has, which starts `gitdir:`."""
    try:
        if entry.is_dir():
            return (entry / "HEAD").is_file()
        with entry.open("rb") as handle:
            return handle.read(7) == b"gitdir:"
    except OSError:
        return False


def repository(cwd: str | os.PathLike | None = None) -> str | None:
    """The top of the work tree `cwd` (default ours) is in, found as the version-control tool
    finds it: the nearest directory, from `cwd` up, whose `.git` is a repository's
    (`_is_repository`). An empty or stray `.git` is passed over. None when there is none, or
    `cwd` cannot be read."""
    try:
        here = Path(os.getcwd() if cwd is None else cwd).resolve()
    except OSError:
        return None
    for directory in (here, *here.parents):
        if _is_repository(directory / ".git"):
            return str(directory)
    return None
