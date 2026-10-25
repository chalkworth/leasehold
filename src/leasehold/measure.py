"""What a contained command used, read through the host while it runs, and the repository it ran
in: the outcome a client sends with its release (`POST /v1/release`, `leasehold.outcomes`).

A tree's kernel group can be removed once its last process exits, so it cannot be read after
the command ends. A `Meter` reads the tree's footprint (`leasehold.host`) from a thread of its
own every `PERIOD_S` while the command runs, and once more as it stops, and keeps the last value
of each:

* `peak_bytes`, the most memory the tree has used (`memory.peak` on Linux). It only grows, so
  the last read misses at most what the final `PERIOD_S` added. None where the platform does
  not say (before Linux 5.19).
* `anon_bytes`, the most anonymous memory seen: a lower bound on what the command needed, since
  `peak_bytes` also counts page cache.
* `swap_bytes`, the most swap the tree used (`memory.swap.peak`, or before Linux 6.5 the most
  `memory.swap.current` seen): memory of the command's that was out of RAM, which neither of
  the two above counts.
* `oom_kills` and `high_events`: how many processes the kernel's OOM killer ended in the tree,
  and how often it went past its soft memory limit.
* `cpu_usec`, the CPU time it used.
* the largest of its processes, by resident and swapped memory, the largest seen at any read
  (`largest`), so that a kill can name it.

A value never read is None.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from leasehold.host import Footprint, Host, Tree

#: How often a `Meter` reads its tree.
PERIOD_S = 1.0


@dataclass(frozen=True)
class Usage:
    """What a tree used, as last read. Each is None until read."""

    peak_bytes: int | None = None
    anon_bytes: int | None = None
    swap_bytes: int | None = None
    oom_kills: int | None = None
    high_events: int | None = None
    cpu_usec: int | None = None


def _most(new: int | None, last: int | None) -> int | None:
    return new if last is None or (new or 0) > last else None


def read(found: Footprint, last: Usage) -> Usage:
    """`last` updated with the footprint `found`: each value read replaces the last one (for
    `peak_bytes`, `anon_bytes` and `swap_bytes`, only when it is larger), and a value that
    could not be read keeps the last one."""
    peak = found.peak_high if found.peak_high is not None else found.peak_low
    values = {
        "peak_bytes": _most(peak, last.peak_bytes),
        "anon_bytes": _most(found.anon, last.anon_bytes),
        "swap_bytes": _most(found.swap, last.swap_bytes),
        "oom_kills": found.oom_kills,
        "high_events": found.high_events,
        "cpu_usec": found.cpu_usec,
    }
    return replace(last, **{k: v for k, v in values.items() if v is not None})


class Meter:
    """Reads `tree` through `host` every `period_s` (default `PERIOD_S`), from a thread of its
    own, until `stop`, or until `running` (when given) says the command has ended. A tree
    without a kernel group is not read. `usage` is what it has read so far. `started` is when,
    on the `time.monotonic` clock, the command was started (default now). `largest` is the
    largest of its processes seen at any read, as (pid, name, bytes) (None: none read)."""

    def __init__(
        self,
        host: Host,
        tree: Tree,
        *,
        started: float | None = None,
        running: Callable[[], bool] | None = None,
        period_s: float = PERIOD_S,
    ) -> None:
        self.usage = Usage()
        self.largest: tuple[int, str, int] | None = None
        self._host = host
        self._tree = tree
        self._period_s = period_s
        self._started = time.monotonic() if started is None else started
        self._running = running
        #: When the thread saw the command had ended; None: it has not.
        self._ended: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"leasehold-meter-{tree.root}", daemon=True
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
        if self._tree.group is None:
            return
        found = self._host.footprint(self._tree)
        self.usage = read(found, self.usage)
        big = found.largest
        if big is not None and (self.largest is None or (big.bytes or 0) > self.largest[2]):
            self.largest = (big.pid, big.name, big.bytes or 0)

    def left(self) -> list[tuple[int, str, int]]:
        """What is still running in the tree, largest first, as (pid, name, bytes): what the
        command left behind once it has ended."""
        if self._tree.group is None:
            return []
        return [(p.pid, p.name, p.bytes or 0) for p in self._host.members(self._tree)]

    def stop(self) -> float:
        """Stop reading, after one last read: the seconds from the command's start to its end,
        as `running` showed it, or else to now. A command waited for late is counted to within
        `PERIOD_S` of its end."""
        self._stop.set()
        self._thread.join()
        self._sample()
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
