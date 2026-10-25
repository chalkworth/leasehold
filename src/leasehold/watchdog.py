"""How short of memory the machine is, and what the watchdog would do about it: the pure decision
behind `[watchdog]`. The machine agent feeds it readings from `leasehold.host` (the machine's
memory, and the trees of the sessions it watches) and reports what it decides.

**Levels.** Available memory counts only once swap is nearly full (`swap_open`): with no swap,
or with less swap free than `SWAP_FREE` allows. While the kernel swaps, it keeps available
memory near a floor that says nothing of a freeze, and the stall share is the signal instead.

* `critical`, the last resort before the machine freezes: available memory under
  `critical_below` while swap is nearly full, or every task stalled on memory for more than
  `STALL_CRITICAL` percent of the last couple of seconds (`Pressure.stall_full_recent`, from
  the kernel's running total: the ten-second average lags, and stays high after the cause has
  gone), or the OS's own level `critical` (macOS).
* `warn`: available memory under `hold_below` while swap is nearly full, or the OS's level
  `warn`.
* `ok`: anything else.

Where the machine's memory cannot be read, there is no level, and the reason says why: not
knowing is never reported as `ok`.

**What it would do.** At `warn` and above it would hold new heavy work, touching nothing that
runs. At `act_at` and above (`critical` by default) it would pause a tree. In `watch` mode it
does neither: `would` says what it would do now (`hold`, or `pause` and the tree), and the
events it would send carry `would: true`.

**Which tree.** A tree is one of a session's (`leasehold.sessions.Branch`). First a
**runaway**: the tree growing fastest over the last `WINDOW_S` seconds, when it grows by at
least `RUNAWAY_MIN_GROWTH` a second and accounts for at least `RUNAWAY_SHARE` of the growth of
all the sessions' trees; or a tree holding more than `RUNAWAY_OVER` times what its leases
declared or learned. Then a **large** tree, holding `LARGE_SHARE` of the machine's memory or
more, the largest first. Then the **newest**, by its root's start, across all sessions.

**Never chosen:** a tree under `min_size`; one that holds the agent process (a launcher the agent
runs under); one made only of leasehold's own processes (`hold`, the shims, the hooks, the
daemon); one whose command line matches a built-in or `[watchdog] protect` pattern (agents and
MCP servers); one whose root started within `AGENT_START_S` of its agent (servers that start
with the session); and, unless `choose_discovered`, any tree of a session found by name, which
never asked to join. leasehold's own processes in a tree (`spared`) are left out of its
footprint, and would be left running if it were paused; a command that holds its own lease
through the Python API is not one of them, and is paused with its tree.

Pure: the caller supplies the clock (`now`, one that never steps) and the readings."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from leasehold.machine import (
    LEVELS,
    PROTECT,
    STALL_CRITICAL,
    SWAP_FREE,
    WatchdogLimits,
    WatchdogSettings,
)

#: The seconds over which a tree's growth is measured.
WINDOW_S = 5.0
#: A runaway grows by at least this many bytes a second ...
RUNAWAY_MIN_GROWTH = 20 << 20
#: ... and by at least this share of all the sessions' trees' growth.
RUNAWAY_SHARE = 0.5
#: A tree holding more than this many times what its leases declared or learned is a runaway.
#: Real builds and test runs repeat their peaks to within about 10%, so twice is far beyond.
RUNAWAY_OVER = 2.0
#: A tree whose root started this soon after its agent started with the session.
AGENT_START_S = 10.0
#: How often the machine's memory is read: while memory is plentiful (the level is `ok`, swap
#: has room or at least `FAR` times `hold_below` is available, and almost nothing stalls),
#: while it is `ok` nearer the thresholds, and while it is not `ok`.
IDLE_S, STEP_S, WARN_STEP_S = 2.0, 0.5, 0.25
FAR = 2.0
#: The stall share (a percentage) still counted as almost nothing.
QUIET_STALL = 1.0
#: How many characters of a command line status and events show.
COMMAND_CHARS = 200


@dataclass(frozen=True)
class Memory:
    """One reading of the machine's memory, at `now` (a clock that never steps). `stall_recent`
    is the share of the last couple of seconds every task stalled (a percentage). `how` is
    `none` when nothing could be read, and then `reason` says why."""

    now: float
    available: int | None
    total: int | None = None
    swap_used: int | None = None
    swap_total: int | None = None
    stall_recent: float | None = None
    os_level: str | None = None
    how: str = "exact"
    reason: str | None = None

    def swap_free(self) -> int | None:
        if not self.swap_total or self.swap_used is None:
            return None
        return max(0, self.swap_total - self.swap_used)

    def swap_open(self) -> bool:
        """Whether available memory counts: no swap, or swap nearly full."""
        free = self.swap_free()
        if free is None:
            return True
        floor, share = SWAP_FREE
        return free < max(floor, int(share * (self.swap_total or 0)))


@dataclass(frozen=True)
class Work:
    """One tree of a session's, as the watchdog weighs it. `pid` and `fence` are its root's;
    `started` when the root started and `agent_started` when its session's agent did (wall
    seconds; None: not known). `footprint` is its anonymous memory, resident or in swap,
    leasehold's own processes in it (`spared`) left out; `leasehold` is True when it has no
    other process. `expected` is what its leases declared or learned (None: nothing)."""

    session: str
    source: str
    pid: int
    fence: str | None
    name: str
    command: str | None
    footprint: int
    started: float | None = None
    agent_started: float | None = None
    spared: tuple[int, ...] = ()
    leasehold: bool = False
    agent_line: bool = False
    expected: int | None = None

    @property
    def key(self) -> tuple[int, str | None]:
        return self.pid, self.fence


@dataclass(frozen=True)
class Choice:
    """The tree the watchdog would pause first, and why: `runaway`, `large` or `newest`.
    `growth` is its growth in bytes a second over the last `WINDOW_S` (None: not measured
    yet)."""

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
            "spared": list(w.spared),
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
    swap_free: int | None = None
    swap_open: bool = True
    stall_recent: float | None = None
    victim: Choice | None = None
    would: dict | None = None
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
        self._runaways: set[tuple[int, str | None]] = set()
        self._state = _State()
        #: What the last decision would have done: (action, tree key), or None.
        self._last: tuple[str, tuple[int, str | None] | None] | None = None

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
        """Judge the machine at `memory`: the events the decision gives (new heavy work would be
        held, a tree is newly a runaway, a tree would now be paused), each with `would` true, as
        nothing is done."""
        state = _State()
        self._state = state
        if memory.how == "none" or memory.available is None:
            state.reason = f"the watchdog cannot see memory here: {memory.reason or 'not read'}"
            self._last = None
            return []
        state.available = memory.available
        state.swap_free = memory.swap_free()
        state.swap_open = memory.swap_open()
        state.stall_recent = memory.stall_recent
        state.level, state.reason = self._level(memory, state)
        self._choose(state)
        return self._events(state)

    def _level(self, m: Memory, s: _State) -> tuple[str, str]:
        lim, avail = self.limits, m.available
        assert avail is not None
        swap = "" if s.swap_free is None else f", {_gib(s.swap_free)} of swap free"
        if s.swap_open and avail < lim.critical_below:
            return "critical", f"{_gib(avail)} available, under {_gib(lim.critical_below)}{swap}"
        if s.stall_recent is not None and s.stall_recent > STALL_CRITICAL:
            return "critical", f"every task stalled on memory {s.stall_recent:.0f}% of the last 2 s"
        if m.os_level == "critical":
            return "critical", "the system says memory is critical"
        if s.swap_open and avail < lim.hold_below:
            return "warn", f"{_gib(avail)} available, under {_gib(lim.hold_below)}{swap}"
        if m.os_level == "warn":
            return "warn", f"the system warns of memory, with {_gib(avail)} available"
        return "ok", f"{_gib(avail)} available{swap}"

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

    def _choose(self, s: _State) -> None:
        if s.level == "ok":
            return
        rates = {w.key: _rate(self._growth.get(w.key, [])) for w in self._work}
        total = sum(max(0.0, r) for r in rates.values() if r is not None)
        found: list[Work] = []
        for w in self._work:
            why = self._why_not(w)
            if why is None:
                found.append(w)
            else:
                s.passed_over[why] = s.passed_over.get(why, 0) + 1
        s.candidates = len(found)

        def growth(w: Work) -> float:
            return rates.get(w.key) or 0.0

        runaways = [w for w in found if w.expected and w.footprint > RUNAWAY_OVER * w.expected]
        fastest = max(found, key=growth, default=None)
        if (
            fastest is not None
            and growth(fastest) >= RUNAWAY_MIN_GROWTH
            and growth(fastest) >= RUNAWAY_SHARE * total
        ):
            runaways.append(fastest)
        large = [w for w in found if w.footprint >= self.limits.large]
        if runaways:
            w = max(runaways, key=lambda w: (growth(w), w.footprint))
            s.victim = Choice(w, "runaway", rates.get(w.key))
        elif large:
            w = max(large, key=lambda w: w.footprint)
            s.victim = Choice(w, "large", rates.get(w.key))
        elif found:
            w = max(found, key=lambda w: (w.started or 0.0, w.pid))
            s.victim = Choice(w, "newest", rates.get(w.key))
        acts = LEVELS.index(s.level or "ok") >= LEVELS.index(self.settings.act_at)
        if acts and s.victim is not None:
            s.would = {"action": "pause"} | s.victim.public()
        else:
            s.would = {"action": "hold"}

    def _events(self, s: _State) -> list[dict]:
        out = []
        v = s.victim
        if v is not None and v.why == "runaway" and v.work.key not in self._runaways:
            self._runaways.add(v.work.key)
            out.append({"type": "runaway", "would": True, "level": s.level} | v.public())
        if s.would is None:
            self._last = None
            return out
        about = {"would": True, "level": s.level, "reason": s.reason}
        if s.would["action"] == "pause":
            assert v is not None
            now = ("pause", v.work.key)
            if now != self._last:
                out.append({"type": "paused"} | about | v.public())
        else:
            now = ("hold", None)
            if self._last is None:
                out.append({"type": "held"} | about)
        self._last = now
        return out

    # --- reporting ---------------------------------------------------------------------------

    def interval(self) -> float:
        """How long until the machine's memory should be read again."""
        s = self._state
        if s.level is None:
            return IDLE_S
        if s.level != "ok":
            return WARN_STEP_S
        roomy = not s.swap_open or (
            s.available is not None and s.available >= FAR * self.limits.hold_below
        )
        quiet = s.stall_recent is None or s.stall_recent < QUIET_STALL
        return IDLE_S if roomy and quiet else STEP_S

    def status(self) -> dict:
        """The watchdog's part of the status object's `watchdog`."""
        s, lim = self._state, self.limits
        return {
            "mode": self.settings.mode,
            "act_at": self.settings.act_at,
            "level": s.level,
            "reason": s.reason,
            "available_bytes": s.available,
            "swap_free_bytes": s.swap_free,
            "available_counts": s.swap_open if s.level is not None else None,
            "stall_recent": None if s.stall_recent is None else round(s.stall_recent, 1),
            "limits": {
                "critical_below": lim.critical_below,
                "hold_below": lim.hold_below,
                "min_size": lim.min_size,
                "large": lim.large,
            },
            "candidates": s.candidates,
            "passed_over": dict(s.passed_over),
            "victim": None if s.victim is None else s.victim.public(),
            "would": s.would,
        }
