"""When a machine lends: the pure gate behind `lend` leases, the only kind ever preempted.

A lend lease is granted only while the machine is **idle**:

* no item of any other kind waits for capacity on the machine;
* measured pressure is low: CPU and memory PSI `some avg10` at most their thresholds (or,
  without PSI, the 1-minute load average per CPU at most `load_per_cpu_max`; unknown load is
  never idle);
* the local time is inside the optional window;

and has been so continuously for `quiet_s`. The moment any of these stops holding, the quiet
period starts again from zero and every lend lease is revoked. Revocation reaches a holder
through its next renewal (`revoked`); a holder that stops renewing loses the lease at its TTL.

Pure: the caller supplies an `Observation` (its clock included) and acts on the `Verdict`.
"""

from __future__ import annotations

from dataclasses import dataclass

from leasehold.machine import LendSettings


@dataclass(frozen=True)
class Observation:
    now: float
    #: Minute of the local day (0..1439), for the window.
    minute: int
    #: Items of any non-lend kind waiting for capacity.
    queued_other: int
    cpu_pressure: float | None
    memory_pressure: float | None
    load_per_cpu: float | None


@dataclass(frozen=True)
class Verdict:
    #: Whether a lend lease may be granted now.
    eligible: bool
    #: Why lending must stop now (revoke every lend lease), or None.
    revoke: str | None
    #: A one-line account of the decision, for `status`.
    reason: str
    quiet_since: float | None


def in_window(window: tuple[int, int] | None, minute: int) -> bool:
    if window is None:
        return True
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def busy(settings: LendSettings, obs: Observation) -> str | None:
    """Why the machine is not idle now, or None."""
    if obs.queued_other:
        return f"{obs.queued_other} non-lend item(s) waiting"
    if obs.cpu_pressure is not None:
        if obs.cpu_pressure > settings.cpu_pressure_max:
            return f"cpu pressure {obs.cpu_pressure:g}% over {settings.cpu_pressure_max:g}%"
    elif obs.load_per_cpu is None:
        return "load unknown (no PSI, no load average)"
    elif obs.load_per_cpu > settings.load_per_cpu_max:
        return f"load {obs.load_per_cpu:.2f} per cpu over {settings.load_per_cpu_max:g}"
    if obs.memory_pressure is not None and obs.memory_pressure > settings.memory_pressure_max:
        return f"memory pressure {obs.memory_pressure:g}% over {settings.memory_pressure_max:g}%"
    if not in_window(settings.window_minutes(), obs.minute):
        return f"outside the lending window {settings.window}"
    return None


class LendGate:
    """The quiet-period memory between observations."""

    def __init__(self, settings: LendSettings) -> None:
        self.settings = settings
        self.quiet_since: float | None = None

    def reset(self) -> None:
        """Something non-lend just queued: the quiet period starts over."""
        self.quiet_since = None

    def observe(self, obs: Observation) -> Verdict:
        why = busy(self.settings, obs)
        if why is not None:
            self.quiet_since = None
            return Verdict(eligible=False, revoke=why, reason=why, quiet_since=None)
        if self.quiet_since is None:
            self.quiet_since = obs.now
        quiet = obs.now - self.quiet_since
        if quiet >= self.settings.quiet_s:
            return Verdict(True, None, f"idle for {quiet:.0f}s", self.quiet_since)
        return Verdict(
            False,
            None,
            f"idle for {quiet:.0f}s of the {self.settings.quiet_s:g}s quiet period",
            self.quiet_since,
        )
