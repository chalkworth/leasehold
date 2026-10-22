"""What a contained command used, read from its scope's cgroup while it runs, and the repository it
ran in: the outcome a client sends with its release (`POST /v1/release`, `leasehold.outcomes`).

A transient scope is removed once its last process exits, so its files cannot be read after the
command ends. A `Meter` reads them from a thread of its own every `PERIOD_S` while the command
runs, and once more as it stops, and keeps the last value of each:

* `memory.peak`, the most memory the scope has used. It only grows, so the last read misses at
  most what the final `PERIOD_S` added. None where the kernel has no such file (before 5.19).
* `anon` from `memory.stat`, the most seen: anonymous memory, a lower bound on what the command
  needed, since `memory.peak` also counts page cache.
* `oom_kill` and `high` from `memory.events`: how many processes the kernel's OOM killer ended
  in the scope, and how often the scope went past its `MemoryHigh`.
* `usage_usec` from `cpu.stat`, the CPU time it used.

A value never read is None. The meter finds the scope from the command's own cgroup
(`/proc/<pid>/cgroup`), and reads nothing unless that is the scope it was told of.
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


def read(group: Path, last: Usage) -> Usage:
    """`last` updated with what can be read in the cgroup directory `group` now: each value
    read replaces the last one (for `anon_bytes`, only when it is larger), and a value that
    cannot be read keeps the last one."""
    events = _fields(_read(group / "memory.events"))
    anon = _fields(_read(group / "memory.stat")).get("anon")
    found = {
        "peak_bytes": _number(_read(group / "memory.peak")),
        "anon_bytes": anon if last.anon_bytes is None or (anon or 0) > last.anon_bytes else None,
        "oom_kills": events.get("oom_kill"),
        "high_events": events.get("high"),
        "cpu_usec": _fields(_read(group / "cpu.stat")).get("usage_usec"),
    }
    return replace(last, **{k: v for k, v in found.items() if v is not None})


class Meter:
    """Reads the cgroup of process `pid` every `PERIOD_S`, from a thread of its own, while that
    cgroup is the scope `unit`, until `stop`, or until `running` (when given) says the command
    has ended. `usage` is what it has read so far. `started` is when, on the `time.monotonic`
    clock, the command was started (default now)."""

    def __init__(
        self,
        pid: int,
        unit: str,
        *,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc: Path = Path("/proc"),
        started: float | None = None,
        running: Callable[[], bool] | None = None,
    ) -> None:
        self.usage = Usage()
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
            if self._stop.wait(PERIOD_S):
                return

    def _sample(self) -> None:
        if self._group is not None:
            self.usage = read(self._group, self.usage)

    def stop(self) -> float:
        """Stop reading, after one last read: the seconds from the command's start to its end,
        as `running` showed it, or else to now. A command waited for late is counted to within
        `PERIOD_S` of its end."""
        self._stop.set()
        self._thread.join()
        self._sample()
        return (self._ended or time.monotonic()) - self._started


def repository(cwd: str | os.PathLike | None = None) -> str | None:
    """The top of the git work tree `cwd` (default ours) is in: the nearest directory, from
    `cwd` up, that holds a `.git` (a directory, or the file a linked work tree has). None when
    there is none, or `cwd` cannot be read."""
    try:
        here = Path(os.getcwd() if cwd is None else cwd).resolve()
    except OSError:
        return None
    for directory in (here, *here.parents):
        if (directory / ".git").exists():
            return str(directory)
    return None
