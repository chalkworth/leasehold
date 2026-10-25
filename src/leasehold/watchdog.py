"""How short of memory the machine is, and which agent work the watchdog would pause to make room:
the pure decision behind `[watchdog]`. The machine agent feeds it readings from `leasehold.host`
(the machine's memory, and the trees of the sessions it watches) and reports what it decides.

**Levels.** `critical` is the last resort before the machine freezes: available memory under
`kill_below`, or every task stalled on memory for more than `STALL_CRITICAL` percent of the last
ten seconds (PSI `full avg10`), or the OS's own level `critical` (macOS). `warn` is available
memory under `pause_below`, or available memory that would reach `kill_below` within `lead_s` at
the rate it fell over the last `WINDOW_S` seconds (the projection: memory pressure confirms a
shortage only once it is there, while the fall warns of one), or the OS's level `warn` with
available memory under `resume_above`. Anything else is `ok`. Where the machine's memory cannot
be read, there is no level, and the reason says why: not knowing is never reported as `ok`.

**Acting.** The watchdog acts only at `act_at` and above (`critical` by default; the levels
under it are for watching). In `watch` mode it acts at no level: `would` says what it would
do now, and the events it would send carry `would: true`.

**Which tree.** A tree is one of a session's (`leasehold.sessions.Branch`). First a
**runaway**: a tree whose own growth over the last `WINDOW_S` seconds is at least
`RUNAWAY_SHARE` of the machine's fall in available memory while the projection is under
`lead_s`, or one holding more than `RUNAWAY_OVER` times what its leases declared or learned;
the fastest growing first. Then the **newest**, by its root's start, across all sessions.

**Never chosen:** a tree under `min_size`; one that holds the agent process (a launcher the agent
runs under); one made only of leasehold's own processes (its keepers and the daemon); one whose
command line matches a built-in or `[watchdog] protect` pattern (agents, MCP servers, leasehold);
one whose root started within `AGENT_START_S` of its agent (servers that start with the
session); and, unless `choose_discovered`, any tree of a session found by name, which never
asked to join. A tree's keepers (the `leasehold hold` that waits for a leased command) are left
out of its footprint, and would be left running if the tree were paused.

Pure: the caller supplies the clock (`now`, one that never steps) and the readings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from leasehold.machine import LEVELS, PROTECT, STALL_CRITICAL, WatchdogLimits, WatchdogSettings

#: The seconds over which a fall in available memory, and a tree's growth, are measured.
WINDOW_S = 5.0
#: A tree growing by at least this share of the machine's fall in available memory is a runaway.
RUNAWAY_SHARE = 0.5
#: A tree holding more than this many times what its leases declared or learned is a runaway.
#: Real builds and test runs repeat their peaks to within about 10%, so twice is far beyond.
RUNAWAY_OVER = 2.0
#: A tree whose root started this soon after its agent started with the session.
AGENT_START_S = 10.0
#: How often the machine's memory is read: while memory is plentiful (the level is `ok`, with
#: at least `FAR` times `pause_below` available and no fall that reaches `kill_below` within
#: `FAR` times `lead_s`), while it is `ok` nearer the thresholds, and while it is not `ok`.
IDLE_S, STEP_S, WARN_STEP_S = 2.0, 0.5, 0.25
FAR = 2.0
#: How many characters of a command line status and events show.
COMMAND_CHARS = 200


@dataclass(frozen=True)
class Memory:
    """One reading of the machine's memory, at `now` (a clock that never steps). `how` is
    `none` when nothing could be read, and then `reason` says why."""

    now: float
    available: int | None
    total: int | None = None
    stall_full: float | None = None
    os_level: str | None = None
    how: str = "exact"
    reason: str | None = None


@dataclass(frozen=True)
class Work:
    """One tree of a session's, as the watchdog weighs it. `pid` and `fence` are its root's;
    `started` when the root started and `agent_started` when its session's agent did (wall
    seconds; None: not known). `footprint` is its anonymous memory, resident or in swap, its
    keepers' (`keepers`, leasehold's own processes in it) left out; `leasehold` is True when it
    has no other process. `expected` is what its leases declared or learned (None: nothing)."""

    session: str
    source: str
    pid: int
    fence: str | None
    name: str
    command: str | None
    footprint: int
    started: float | None = None
    agent_started: float | None = None
    keepers: tuple[int, ...] = ()
    leasehold: bool = False
    agent_line: bool = False
    expected: int | None = None

    @property
    def key(self) -> tuple[int, str | None]:
        return self.pid, self.fence


@dataclass(frozen=True)
class Choice:
    """The tree the watchdog would pause first, and why: `runaway` or `newest`. `growth` is its
    growth in bytes a second over the last `WINDOW_S` (None: not measured yet)."""

    work: Work
    why: str
    growth: float | None

    def public(self) -> dict:
        w = self.work
        command = w.command or w.name
        return {
            "session": w.session,
            "pid": w.pid,
            "cmd": command[:COMMAND_CHARS],
            "footprint_bytes": w.footprint,
            "expected_bytes": w.expected,
            "growth_bytes_per_s": None if self.growth is None else round(self.growth),
            "started": w.started,
            "why": self.why,
            "runaway": self.why == "runaway",
            "spared": list(w.keepers),
        }


def _rate(samples: list[tuple[float, int]]) -> float | None:
    """Bytes a second from the oldest sample kept to the newest (None: under two, or no time
    between them)."""
    if len(samples) < 2 or samples[-1][0] <= samples[0][0]:
        return None
    (t0, v0), (t1, v1) = samples[0], samples[-1]
    return (v1 - v0) / (t1 - t0)


def _keep(samples: list[tuple[float, int]], now: float, value: int) -> None:
    """Add a sample, keeping the newest one at least `WINDOW_S` old as the window's start."""
    if samples and samples[-1][0] == now:
        samples[-1] = (now, value)
    else:
        samples.append((now, value))
    while len(samples) > 2 and samples[1][0] <= now - WINDOW_S:
        del samples[0]


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


@dataclass
class _State:
    level: str | None = None
    reason: str | None = None
    available: int | None = None
    fall: float | None = None
    projected_s: float | None = None
    victim: Choice | None = None
    would: Choice | None = None
    candidates: int = 0
    passed_over: dict[str, int] = field(default_factory=dict)


class Watchdog:
    """The level the machine's memory is at, the tree first in line, and what the watchdog would
    do. Not thread-safe: the caller serialises `work` and `observe`."""

    def __init__(self, settings: WatchdogSettings, limits: WatchdogLimits) -> None:
        self.settings = settings
        self.limits = limits
        self._protect = [re.compile(p) for p in (*PROTECT, *settings.protect)]
        self._work: list[Work] = []
        self._growth: dict[tuple[int, str | None], list[tuple[float, int]]] = {}
        self._available: list[tuple[float, int]] = []
        self._runaways: set[tuple[int, str | None]] = set()
        self._state = _State()
        #: The tree the last decision would have paused (None: none).
        self._last_would: tuple[int, str | None] | None = None

    # --- readings ----------------------------------------------------------------------------

    def work(self, trees: list[Work], *, now: float) -> None:
        """The sessions' trees as just read, at `now`."""
        self._work = list(trees)
        seen = set()
        for w in trees:
            seen.add(w.key)
            _keep(self._growth.setdefault(w.key, []), now, w.footprint)
        for gone in set(self._growth) - seen:
            del self._growth[gone]
        self._runaways &= seen

    def observe(self, memory: Memory) -> list[dict]:
        """Judge the machine at `memory`: the events the decision gives (a tree newly a runaway,
        a tree the watchdog would now pause), each with `would` true, as nothing is done."""
        state = _State()
        self._state = state
        if memory.how == "none" or memory.available is None:
            self._available.clear()
            state.reason = f"the watchdog cannot see memory here: {memory.reason or 'not read'}"
            return []
        _keep(self._available, memory.now, memory.available)
        state.available = memory.available
        rate = _rate(self._available)
        state.fall = None if rate is None else max(0.0, -rate)
        lim = self.limits
        if memory.available <= lim.kill_below:
            state.projected_s = 0.0
        elif state.fall:
            state.projected_s = (memory.available - lim.kill_below) / state.fall
        state.level, state.reason = self._level(memory, state)
        self._choose(state)
        return self._events(state)

    def _level(self, m: Memory, s: _State) -> tuple[str, str]:
        lim, avail = self.limits, m.available
        assert avail is not None
        if avail < lim.kill_below:
            return "critical", f"{_gib(avail)} available, under {_gib(lim.kill_below)}"
        if m.stall_full is not None and m.stall_full > STALL_CRITICAL:
            return "critical", f"every task stalled on memory {m.stall_full:.0f}% of the last 10 s"
        if m.os_level == "critical":
            return "critical", "the system says memory is critical"
        if avail < lim.pause_below:
            return "warn", f"{_gib(avail)} available, under {_gib(lim.pause_below)}"
        if s.projected_s is not None and s.projected_s < self.settings.lead_s:
            return "warn", (
                f"{_gib(avail)} available, falling {_gib(int(s.fall or 0))}/s: under "
                f"{_gib(lim.kill_below)} in {s.projected_s:.0f} s"
            )
        if m.os_level == "warn" and avail < lim.resume_above:
            return "warn", f"the system warns of memory, with {_gib(avail)} available"
        return "ok", f"{_gib(avail)} available"

    # --- choosing ----------------------------------------------------------------------------

    def _why_not(self, w: Work) -> str | None:
        """Why `w` is never chosen; None when it may be."""
        if w.agent_line:
            return "agent"
        if w.leasehold:
            return "leasehold"
        if w.source == "auto" and not self.settings.choose_discovered:
            return "discovered"
        line = w.command or w.name
        if any(p.search(line) for p in self._protect):
            return "protected"
        if (
            w.started is not None
            and w.agent_started is not None
            and w.started - w.agent_started < AGENT_START_S
        ):
            return "started with its agent"
        if w.footprint < self.limits.min_size:
            return "small"
        return None

    def _runaway(self, w: Work, growth: float | None, s: _State) -> bool:
        if w.expected and w.footprint > RUNAWAY_OVER * w.expected:
            return True
        near = s.projected_s is not None and s.projected_s < self.settings.lead_s
        return bool(near and s.fall and growth is not None and growth >= RUNAWAY_SHARE * s.fall)

    def _choose(self, s: _State) -> None:
        if s.level == "ok":
            return
        found: list[tuple[Work, float | None, bool]] = []
        for w in self._work:
            why = self._why_not(w)
            if why is not None:
                s.passed_over[why] = s.passed_over.get(why, 0) + 1
                continue
            growth = _rate(self._growth.get(w.key, []))
            found.append((w, growth, self._runaway(w, growth, s)))
        s.candidates = len(found)
        runaways = [f for f in found if f[2]]
        if runaways:
            w, growth, _ = max(runaways, key=lambda f: (f[1] or 0.0, f[0].footprint))
            s.victim = Choice(w, "runaway", growth)
        elif found:
            w, growth, _ = max(found, key=lambda f: (f[0].started or 0.0, f[0].pid))
            s.victim = Choice(w, "newest", growth)
        acts = LEVELS.index(s.level or "ok") >= LEVELS.index(self.settings.act_at)
        s.would = s.victim if acts else None

    def _events(self, s: _State) -> list[dict]:
        out = []
        v = s.victim
        if v is not None and v.why == "runaway" and v.work.key not in self._runaways:
            self._runaways.add(v.work.key)
            out.append({"type": "runaway", "would": True, "level": s.level} | v.public())
        if s.would is not None:
            key = s.would.work.key
            if key != self._last_would:
                out.append(
                    {"type": "paused", "would": True, "level": s.level, "reason": s.reason}
                    | s.would.public()
                )
            self._last_would = key
        else:
            self._last_would = None
        return out

    # --- reporting ---------------------------------------------------------------------------

    def interval(self) -> float:
        """How long until the machine's memory should be read again."""
        s = self._state
        if s.level is None:
            return IDLE_S
        if s.level != "ok":
            return WARN_STEP_S
        far = s.available is not None and s.available >= FAR * self.limits.pause_below
        if far and (s.projected_s is None or s.projected_s > FAR * self.settings.lead_s):
            return IDLE_S
        return STEP_S

    def status(self) -> dict:
        """The watchdog's part of the status object's `watchdog`."""
        s, lim = self._state, self.limits
        return {
            "mode": self.settings.mode,
            "act_at": self.settings.act_at,
            "level": s.level,
            "reason": s.reason,
            "available_bytes": s.available,
            "fall_bytes_per_s": None if s.fall is None else round(s.fall),
            "projected_s": None if s.projected_s is None else round(s.projected_s, 1),
            "limits": {
                "pause_below": lim.pause_below,
                "kill_below": lim.kill_below,
                "resume_above": lim.resume_above,
                "min_size": lim.min_size,
                "lead_s": self.settings.lead_s,
            },
            "candidates": s.candidates,
            "passed_over": dict(s.passed_over),
            "victim": None if s.victim is None else s.victim.public(),
            "would": None if s.would is None else {"action": "pause"} | s.would.public(),
        }
