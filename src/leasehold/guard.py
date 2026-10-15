"""When a machine holds back new work: the pure gate behind memory-pressure admission.

Running out of memory is what freezes or OOM-kills a machine that runs too much at once, and it
is measured, not declared: the guard watches memory PSI (`some avg10`, the share of the last
ten seconds in which some task stalled on memory) and `MemAvailable`. While it is **on**, what
is already running keeps running, and new work is held back, in one of two ways (the machine
agent chooses by the config):

* with a memory budget (`[memory]`), new work may count at most the memory measured free
  (`AdmissionPolicy.memory_free`): none while pressure is over the threshold or memory is under
  the floor (`pressing`), and while the guard settles, what `MemAvailable` has above the floor
  less what was granted in the last `settle_s` (work that has not grown to its size yet). A
  request heavier than its pool's reservation starts as soon as its memory fits;
* without one, every pool admits only up to its reservation (`AdmissionPolicy.capped`): reserved
  work still starts, and the rest waits in line with the reason given.

It goes on when pressure passes `memory_pressure_on` or available memory drops below
`min_available`, and off only once pressure has stayed at most `memory_pressure_off` with
memory above the floor for `settle_s`. The two thresholds and the settling time keep it from
flapping: work admitted when it goes off adds pressure of its own.

Pure: the caller supplies the clock and the readings, and acts on the `Guard.on` it reports.
`Reading.now` is on a clock that never steps (`time.monotonic`), so a wall-clock change neither
ends nor stretches the settling; `Reading.at`, the wall time, is only reported (`since`).
"""

from __future__ import annotations

from dataclasses import dataclass

from leasehold.machine import GuardSettings


@dataclass(frozen=True)
class Reading:
    #: When, on a clock that never steps: what the settling is measured on.
    now: float
    #: Memory PSI `some avg10` (a percentage), or None without PSI.
    memory_pressure: float | None
    #: `MemAvailable` in bytes, or None when unknown.
    available: int | None
    #: The wall time of the reading, for `since`; `now` when not given.
    at: float | None = None


class Guard:
    """Whether new work is held back now, and why (`reason`)."""

    def __init__(self, settings: GuardSettings, *, min_available: int) -> None:
        self.settings = settings
        self.min_available = min_available
        self.on = False
        self.reason: str | None = None
        self.since: float | None = None
        #: Whether the last reading was itself over a threshold (not only settling).
        self.pressing = False
        self._clear_since: float | None = None

    def _over(self, r: Reading) -> str | None:
        s = self.settings
        if r.memory_pressure is not None and r.memory_pressure > s.memory_pressure_on:
            return f"memory pressure {r.memory_pressure:g}% over {s.memory_pressure_on:g}%"
        if r.available is not None and r.available < self.min_available:
            return (
                f"{r.available / 2**30:.1f} GiB of memory available, "
                f"under {self.min_available / 2**30:.1f} GiB"
            )
        return None

    def _clear(self, r: Reading) -> bool:
        low = r.memory_pressure is None or r.memory_pressure <= self.settings.memory_pressure_off
        return low and (r.available is None or r.available >= self.min_available)

    def observe(self, r: Reading) -> bool:
        """Take one reading; whether new work is held back now."""
        why = self._over(r)
        self.pressing = why is not None
        if why is not None:
            if not self.on:
                self.since = r.now if r.at is None else r.at
            self.on, self.reason, self._clear_since = True, why, None
        elif self.on:
            if not self._clear(r):
                self._clear_since = None
            elif self._clear_since is None:
                self._clear_since = r.now
            if (
                self._clear_since is not None
                and r.now - self._clear_since >= self.settings.settle_s
            ):
                self.on, self.reason, self.since, self._clear_since = False, None, None, None
        return self.on

    def status(self) -> dict:
        s = self.settings
        return {
            "on": self.on,
            "pressing": self.pressing,
            "reason": self.reason,
            "since": self.since,
            "memory_pressure_on": s.memory_pressure_on,
            "memory_pressure_off": s.memory_pressure_off,
            "min_available_bytes": self.min_available,
            "settle_s": s.settle_s,
        }
