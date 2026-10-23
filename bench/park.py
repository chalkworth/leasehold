"""Parking, an experiment: when memory inside the benchmark's limit runs out, pause one memory
job and push its memory out to swap, instead of letting the OOM killer end it.

A condition with `park` or `overflow` (`bench/conditions.py`) runs a `Parker` beside its jobs.
It watches the cgroup that carries the benchmark's `MemoryMax` (the jobs' slice) and the lease
scope of every running `mem` job. Its modes:

* `freeze`. Each running `mem` job's scope gets `memory.swap.max` of `overflow` (1 GiB by
  default), a little swap to spill into while the parker acts. A park is due when a running
  job's swap grew by `SPILL_RATE` or more over the last `WINDOW_S`, and the slice spent
  `SOME_SHARE` or more of that time stalled on memory (PSI `some`, from its `total`, which
  answers within the window where `avg10` lags by seconds). `memory.swap.current` alone would
  not do: swapped pages stay counted until they are touched, so one spill would keep it high.
  A running job that fills its `overflow` before the parker acts takes the slice to its limit
  and the OOM killer; `swap_max_hits` counts the jobs whose swap reached their `overflow`
  less `FULL_SLACK` while running, before any park. A thawed job keeps the swap its park
  pushed out, so it swaps no more until it reads that back in.
* `threshold`. A park is due when the slice's `memory.current` is above `PARK_AT` of its
  limit. The slice also gets `MemoryHigh` at `HIGH_AT` of `MemoryMax` (`Setup.start`), so a
  growing job is throttled, which gives the parker time to act.
* `off` (`overflow` without `park`). The scopes get their `overflow` and nothing is parked:
  the arm that tells what the swap limit does from what parking does.

A park needs two or more `mem` jobs running unfrozen, and it parks the youngest: it freezes
the job's scope (`cgroup.freeze`), lifts its `memory.swap.max` to `max`, and writes the job's
`memory.current` to its `memory.reclaim`. A reclaim that stops short or fails (`EAGAIN`) is
fine: the job stays frozen either way. No park follows within `SETTLE_S` of a thaw. A park of a
job parked before in the run is a re-park.

The parker thaws one job a tick, oldest parked first, when the slice's usage plus what the job
held before it was parked is below `THAW_AT` of the limit, or when no other `mem` job is
running unfrozen. A job parked for `MAX_PARK_S` is thawed whatever the memory (a forced thaw),
and parked again if the trigger fires again while its peers still run. A thawed job's
`memory.swap.max` goes back to `overflow`.

A park is counted spurious when the job, at what it held before the park, would have fitted
within `SPURIOUS_AT` of the limit beside the most the rest of the slice used while it was
parked: nothing ran short after all.

The parker wakes on every change to the slice's `memory.events` and on a memory pressure
trigger on the slice (`PSI_TRIGGER`; an unprivileged trigger's window must be a multiple of 2
s), and every `TICK_S` besides.

Swap writes wear the disk, so a run reclaims at most `cap_bytes` (the condition's `parkcap`).
Once it has, the parker parks nothing more in that run and records that the cap was reached;
it still thaws. `thaw_all` thaws every job it parked and runs when the run ends, however it
ends; `thaw_leftovers` thaws anything still frozen under the benchmark's slices, for a harness
that was killed.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import os
import select
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PARK_AT = 0.85
THAW_AT = 0.80
HIGH_AT = 0.92
SPURIOUS_AT = 0.95
SPILL_RATE = 32 << 20
FULL_SLACK = 16 << 20
SOME_SHARE = 0.05
WINDOW_S = 0.25
MAX_PARK_S = 120.0
SETTLE_S = 2.0
TICK_S = 0.25
PSI_TRIGGER = b"some 50000 2000000\0"
#: What `parkcap` and `overflow` are when a condition does not set them.
DEFAULT_CAP_BYTES = 24 << 30
DEFAULT_OVERFLOW_BYTES = 1 << 30
#: The modes, by the name `park=` gives them; `off` is `overflow` without `park`.
MODES = ("freeze", "threshold", "off")


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().split()[0]
    except (OSError, IndexError):
        return None
    return None if text == "max" else int(text)


def _some(cgroup: Path) -> tuple[float | None, int | None]:
    """(`some` avg10, `some` total in microseconds) of `cgroup`'s memory pressure."""
    with contextlib.suppress(OSError, ValueError, KeyError, IndexError):
        line = (cgroup / "memory.pressure").read_text().splitlines()[0]
        fields = dict(f.split("=") for f in line.split()[1:])
        return float(fields["avg10"]), int(fields["total"])
    return None, None


def _swapped(cgroup: Path) -> int:
    return _read_int(cgroup / "memory.swap.current") or 0


class Window:
    """Readings of one counter over time; `rise` is how much it grew over at least `WINDOW_S`
    (None until there are readings that far apart)."""

    def __init__(self) -> None:
        self.points: collections.deque[tuple[float, int]] = collections.deque()

    def add(self, t: float, value: int) -> None:
        self.points.append((t, value))
        while len(self.points) > 2 and t - self.points[1][0] >= WINDOW_S:
            self.points.popleft()

    def rise(self) -> tuple[int, float] | None:
        """(growth, seconds) from the oldest reading kept to the newest."""
        if len(self.points) < 2:
            return None
        (t0, v0), (t1, v1) = self.points[0], self.points[-1]
        return (v1 - v0, t1 - t0) if t1 - t0 >= WINDOW_S else None

    def clear(self) -> None:
        self.points.clear()


@dataclass
class Job:
    cgroup: Path
    started: float
    parked_at: float | None = None
    pre_park: int = 0
    parks: int = 0
    low: int = 0  # the least swap held since the job last started running
    spill: int = 0  # the most swap taken past `low` while running
    peak_others: int = 0  # the most the rest of the slice used while this job was parked
    swap_peak: int = 0  # the most swap held while running
    swap: Window = field(default_factory=Window)


class Parker:
    """Parks and thaws the `mem` jobs of one run (see the module's docstring). The agents
    `track` a job once it has started and `untrack` it when it ends."""

    def __init__(
        self, cap_cgroup: Path, limit: int, cap_bytes: int, mode: str, overflow: int | None
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"park={mode!r}: one of {', '.join(MODES)}")
        self.cap_cgroup = cap_cgroup
        self.limit = _read_int(cap_cgroup / "memory.max") or limit
        self.cap_bytes = cap_bytes
        self.mode = mode
        self.overflow = overflow
        self.lock = threading.Lock()
        self.jobs: dict[int, Job] = {}
        self.events: list[dict] = []
        self.reclaimed = 0
        self.overflowed = 0
        self.swap_max_hits = 0
        self.spurious = 0
        self.cap_reached = False
        self.last_thaw = 0.0
        self.some = Window()
        self.wakeups = {"timer": 0, "events": 0, "psi": 0}
        self.psi_trigger = False

    def track(self, key: int, cgroup: Path, started: float) -> None:
        with self.lock:
            if self.overflow is not None:
                _write(cgroup / "memory.swap.max", str(self.overflow))
            self.jobs[key] = Job(cgroup, started, low=_swapped(cgroup))

    def untrack(self, key: int) -> None:
        with self.lock:
            job = self.jobs.pop(key, None)
            if job is None:
                return
            if job.parked_at is not None:
                self._unparked(job, time.time(), "ended-parked")
            self.overflowed += job.spill
            if self.overflow is not None:
                self.swap_max_hits += job.swap_peak >= self.overflow - FULL_SLACK

    def run(self, stop: threading.Event) -> None:
        watch = Watch(self.cap_cgroup)
        try:
            while not stop.is_set():
                woken = watch.wait(TICK_S)
                self.psi_trigger = self.psi_trigger or watch.psi
                self.wakeups[woken] += 1
                with self.lock:
                    self.tick(time.time())
        finally:
            watch.close()
            self.thaw_all()

    def tick(self, now: float) -> None:
        current = _read_int(self.cap_cgroup / "memory.current") or 0
        _, total = _some(self.cap_cgroup)
        if total is not None:
            self.some.add(now, total)
        stalled = self.some.rise()
        pressed = stalled is not None and stalled[0] / 1e6 >= SOME_SHARE * stalled[1]
        for job in self._parked():
            held = _read_int(job.cgroup / "memory.current") or 0
            job.peak_others = max(job.peak_others, current - held)
            if now - (job.parked_at or now) >= MAX_PARK_S:
                self._thaw(job, now, "forced-thaw")
        running = [j for j in self.jobs.values() if j.parked_at is None]
        spilling = False
        for job in running:
            swap = _swapped(job.cgroup)
            job.low = min(job.low, swap)
            job.spill = max(job.spill, swap - job.low)
            job.swap.add(now, swap)
            if not job.parks:  # a thawed job's swap is mostly what its park pushed out
                job.swap_peak = max(job.swap_peak, swap)
            rise = job.swap.rise()
            spilling = spilling or (rise is not None and rise[0] >= SPILL_RATE)
        if self.mode == "off":
            return
        parked = self._parked()
        if parked:
            oldest = parked[0]
            if not running or current + oldest.pre_park < THAW_AT * self.limit:
                self._thaw(oldest, now, "thaw")
                return
        due = spilling and pressed if self.mode == "freeze" else current > PARK_AT * self.limit
        if due and not self.cap_reached and len(running) >= 2 and now - self.last_thaw >= SETTLE_S:
            self._park(max(running, key=lambda j: j.started), now)

    def _parked(self) -> list[Job]:
        return sorted(
            (j for j in self.jobs.values() if j.parked_at is not None),
            key=lambda j: j.parked_at or 0,
        )

    def _park(self, job: Job, now: float) -> None:
        if not _write(job.cgroup / "cgroup.freeze", "1"):
            return  # the job's scope is gone: it has just ended
        job.parked_at = now
        job.parks += 1
        if self.overflow is not None:
            _write(job.cgroup / "memory.swap.max", "max")
        job.pre_park = _read_int(job.cgroup / "memory.current") or 0
        job.peak_others = 0
        swap0 = _swapped(job.cgroup)
        want = min(job.pre_park, self.cap_bytes - self.reclaimed)
        if want > 0:
            _write(job.cgroup / "memory.reclaim", str(want))
        after = _read_int(job.cgroup / "memory.current")
        reclaimed = max(0, job.pre_park - after) if after is not None else 0
        self.reclaimed += reclaimed
        self._event(
            "park" if job.parks == 1 else "re-park",
            job,
            job_bytes=job.pre_park,
            job_swap_before=swap0,
            reclaimed=reclaimed,
            swapped=_swapped(job.cgroup) - swap0,
            reclaim_s=round(time.time() - now, 3),
        )
        if self.reclaimed >= self.cap_bytes:
            self.cap_reached = True
            self._event("cap-reached", job)

    def _thaw(self, job: Job, now: float, why: str) -> None:
        if self.overflow is not None:
            _write(job.cgroup / "memory.swap.max", str(self.overflow))
        _write(job.cgroup / "cgroup.freeze", "0")
        self._unparked(job, now, why)
        job.low = _swapped(job.cgroup)
        job.swap.clear()
        self.last_thaw = now

    def _unparked(self, job: Job, now: float, why: str) -> None:
        spurious = job.pre_park + job.peak_others <= SPURIOUS_AT * self.limit
        self.spurious += spurious
        self._event(
            why,
            job,
            parked_s=now - (job.parked_at or now),
            peak_others=job.peak_others,
            spurious=spurious,
        )
        job.parked_at = None

    def thaw_all(self) -> None:
        """Thaw every job this parker has parked."""
        with self.lock:
            for job in self._parked():
                self._thaw(job, time.time(), "thaw-at-end")

    def _event(self, what: str, job: Job, **extra) -> None:
        self.events.append(
            {
                "t": time.time(),
                "event": what,
                "scope": job.cgroup.name,
                "slice_current": _read_int(self.cap_cgroup / "memory.current"),
                "psi_some_avg10": _some(self.cap_cgroup)[0],
                **extra,
            }
        )

    def summary(self) -> dict:
        """What run.json records: parks and re-parks, forced thaws, spurious parks, seconds
        parked in all, bytes reclaimed, whether the cap on them was reached, the swap running
        jobs spilled into, how many jobs filled their overflow, and what woke the parker."""
        ends = ("thaw", "forced-thaw", "thaw-at-end", "ended-parked")
        parked_s = sum(e.get("parked_s", 0) for e in self.events if e["event"] in ends)
        overflowed = self.overflowed + sum(j.spill for j in self.jobs.values())
        return {
            "mode": self.mode,
            "parks": sum(e["event"] in ("park", "re-park") for e in self.events),
            "reparks": sum(e["event"] == "re-park" for e in self.events),
            "forced_thaws": sum(e["event"] == "forced-thaw" for e in self.events),
            "spurious": self.spurious,
            "park_s": round(parked_s, 1),
            "reclaimed_bytes": self.reclaimed,
            "swapped_bytes": sum(e.get("swapped", 0) for e in self.events),
            "cap_bytes": self.cap_bytes,
            "cap_reached": self.cap_reached,
            "overflow_bytes": self.overflow,
            "overflowed_bytes": overflowed,
            "swap_max_hits": self.swap_max_hits if self.overflow is not None else None,
            "psi_trigger": self.psi_trigger,
            "wakeups": self.wakeups,
        }


class Watch:
    """What wakes the parker: a change to `cgroup`'s `memory.events`, or its memory pressure
    trigger. systemd removes an empty slice's cgroup and makes a new one when a scope starts
    in it again, and a new cgroup's memory files can appear a moment after it, so both are
    opened again whenever the cgroup is a new one or either is missing."""

    def __init__(self, cgroup: Path) -> None:
        self.cgroup = cgroup
        self.inode: int | None = None
        self.fds: dict[int, str] = {}
        self.poller = select.poll()
        self.psi = False

    def _reopen(self) -> None:
        try:
            inode = os.stat(self.cgroup).st_ino
        except OSError:
            inode = None
        if inode == self.inode and len(self.fds) == 2:
            return
        self.close()
        self.inode = inode
        if inode is None:
            return
        with contextlib.suppress(OSError):
            fd = os.open(self.cgroup / "memory.events", os.O_RDONLY)
            os.read(fd, 4096)  # a change is news from here on
            self.fds[fd] = "events"
        with contextlib.suppress(OSError):
            fd = os.open(self.cgroup / "memory.pressure", os.O_RDWR | os.O_NONBLOCK)
            try:
                os.write(fd, PSI_TRIGGER)
                self.fds[fd] = "psi"
                self.psi = True
            except OSError:
                os.close(fd)
        for fd in self.fds:
            self.poller.register(fd, select.POLLPRI)

    def wait(self, timeout: float) -> str:
        """Wait up to `timeout` seconds; what woke it: `events`, `psi` or `timer`."""
        self._reopen()
        ready = self.poller.poll(timeout * 1000)
        for fd, _ in ready:
            if self.fds[fd] == "events":
                os.lseek(fd, 0, os.SEEK_SET)
                os.read(fd, 4096)
        return self.fds[ready[0][0]] if ready else "timer"

    def close(self) -> None:
        for fd in self.fds:
            self.poller.unregister(fd)
            os.close(fd)
        self.fds = {}


def _write(path: Path, value: str) -> bool:
    """Write `value` to the cgroup file `path`; False if the cgroup is gone. A reclaim that
    could not reclaim all it was asked (`EAGAIN`) counts as written."""
    try:
        fd = os.open(path, os.O_WRONLY)
    except FileNotFoundError:
        return False
    try:
        os.write(fd, value.encode())
    except OSError as exc:
        if exc.errno == errno.EAGAIN:
            return True
        if exc.errno in (errno.ENOENT, errno.ENODEV):
            return False
        raise
    finally:
        os.close(fd)
    return True


def thaw_leftovers(root: Path) -> list[Path]:
    """Thaw every frozen cgroup under the benchmark's session slices in `root` (the user
    manager's `leasehold.slice`); the cgroups it thawed."""
    thawed = []
    for path in root.glob("leasehold-bench_*.slice/**/cgroup.freeze"):
        with contextlib.suppress(OSError):
            if path.read_text().strip() == "1":
                path.write_text("0")
                thawed.append(path.parent)
    return thawed
