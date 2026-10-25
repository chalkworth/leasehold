"""How short of memory the machine is, and what the watchdog does about it: the pure decision
behind `[watchdog]`. The machine agent feeds it readings from `leasehold.host` (the machine's
memory, and the trees of the sessions it watches), carries out what it decides through the
host, and reports it.

**Levels** (`judge`, a pure function of one reading, so that the trigger is one place to
change). Available memory counts only once swap is nearly full (`Memory.swap_open`): with no
swap, or with less swap free than `SWAP_FREE` allows. While the kernel swaps, it keeps
available memory near a floor that says nothing of a freeze, and the stall share is the signal
instead. A reading says what the swap is (`swap_kind`) and, for zram, how well its pages
compress (`zram_ratio`), for a rule that treats compressed swap differently.

* `critical`, the last resort before the machine freezes: available memory under
  `critical_below` while swap is nearly full, or every task stalled on memory for more than
  `STALL_CRITICAL` percent of the last couple of seconds (`Pressure.stall_full_recent`, from
  the kernel's running total: the ten-second average lags, and stays high after the cause has
  gone), or the OS's own level `critical` (macOS).
* `warn`: available memory under `hold_below` while swap is nearly full, or the OS's level
  `warn`.
* `ok`: anything else.

Where the machine's memory cannot be read, there is no level, and the reason says why: not
knowing is never reported as `ok`, and the watchdog never acts on it.

**What it does** at `act_at` and above (`critical` by default), with `mode = "on"`:

* it **holds** new heavy work (`held`), and keeps holding while any tree it paused is still
  running, resumed or not, until that tree ends: a resumed tree reads its memory back, and new
  work would take the room it needs;
* it **pauses** the first tree in line, and the next one after `STEP_S` only while available
  memory is still falling (by `FALLING` since the last pause);
* it **stops** (SIGKILL at once: a stopped process does not act on SIGTERM) the newest paused
  tree, a runaway first, once the level has been `critical` for `KILL_AFTER_S` since the
  episode began or since the last stop; a paused runaway or large tree `KILL_AFTER_S` after its
  pause, whatever the level; and any tree paused for `max_pause_s`;
* it **resumes** the tree paused longest once the level has been `ok` for `SETTLE_S`, and, while
  available memory counts, once what is available less the tree's growth before its pause over
  `LEAD_S` is at least `hold_below`; then the next, `SETTLE_S` later.

In `watch` mode it does none of this: `would` says what it would do now (`hold`, or `pause` and
the tree), and the events it would send carry `would: true`.

**Which tree.** A tree is one of a session's (`leasehold.sessions.Branch`). First a
**runaway**: the tree growing fastest over the last `WINDOW_S` seconds, when it grows by at
least `RUNAWAY_MIN_GROWTH` a second and accounts for at least `RUNAWAY_SHARE` of the growth of
all the sessions' trees; or a tree holding more than `RUNAWAY_OVER` times what its leases
declared or learned. Then a **large** tree, holding `LARGE_SHARE` of the machine's memory or
more, the largest first. Then the **newest**, by its root's start, across all sessions. A tree
running a command again alone after the watchdog stopped it (`Work.alone`) comes last.

**Never chosen:** a tree under `min_size`; one that holds the agent process (a launcher the agent
runs under); one made only of leasehold's own processes (`hold`, the shims, the hooks, the
daemon); one whose command line matches a built-in or `[watchdog] protect` pattern (agents and
MCP servers); one whose root started within `AGENT_START_S` of its agent (servers that start
with the session); and, unless `choose_discovered`, any tree of a session found by name, which
never asked to join. leasehold's own processes in a tree (`spared`) are left out of its
footprint, and left running when it is paused; a command that holds its own lease through the
Python API is not one of them, and is paused with its tree.

Pure: the caller supplies the clock (`now`, one that never steps) and the readings."""

from __future__ import annotations

import re
from collections import deque
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
#: while it is `ok` nearer the thresholds, and while it is not `ok` or a tree is paused.
IDLE_S, STEP_S_READ, WARN_STEP_S = 2.0, 0.5, 0.25
FAR = 2.0
#: The stall share (a percentage) still counted as almost nothing.
QUIET_STALL = 1.0
#: How many characters of a command line status and events show.
COMMAND_CHARS = 200
#: The next tree is paused this long after the last, and only while available memory has
#: fallen by `FALLING` bytes since: pausing more while memory is flat pauses everything.
STEP_S = 2.0
FALLING = 50 << 20
#: How long the level stays `critical` (or a paused runaway or large tree stays paused) before
#: the watchdog stops a paused tree.
KILL_AFTER_S = 5.0
#: How long the level stays `ok` before a paused tree runs again, and between two resumes.
SETTLE_S = 10.0
#: A resumed tree is given room for this many seconds of the growth it had before its pause.
LEAD_S = 15.0
#: How many stopped trees status remembers.
STOPPED_KEPT = 20


@dataclass(frozen=True)
class Memory:
    """One reading of the machine's memory, at `now` (a clock that never steps). `stall_recent`
    is the share of the last couple of seconds every task stalled (a percentage). `swap_kind`
    and `zram_ratio` are as `host.Pressure` has them. `how` is `none` when nothing could be
    read, and then `reason` says why."""

    now: float
    available: int | None
    total: int | None = None
    swap_used: int | None = None
    swap_total: int | None = None
    stall_recent: float | None = None
    os_level: str | None = None
    how: str = "exact"
    reason: str | None = None
    swap_kind: str | None = None
    zram_ratio: float | None = None

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


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def judge(m: Memory, limits: WatchdogLimits) -> tuple[str, str]:
    """The level `m` is at, and why, in words. `m` has an `available` reading."""
    avail = m.available
    assert avail is not None
    counts = m.swap_open()
    free = m.swap_free()
    swap = "" if free is None else f", {_gib(free)} of swap free"
    if counts and avail < limits.critical_below:
        return "critical", f"{_gib(avail)} available, under {_gib(limits.critical_below)}{swap}"
    if m.stall_recent is not None and m.stall_recent > STALL_CRITICAL:
        return "critical", f"every task stalled on memory {m.stall_recent:.0f}% of the last 2 s"
    if m.os_level == "critical":
        return "critical", "the system says memory is critical"
    if counts and avail < limits.hold_below:
        return "warn", f"{_gib(avail)} available, under {_gib(limits.hold_below)}{swap}"
    if m.os_level == "warn":
        return "warn", f"the system warns of memory, with {_gib(avail)} available"
    return "ok", f"{_gib(avail)} available{swap}"


@dataclass(frozen=True)
class Work:
    """One tree of a session's, as the watchdog weighs it. `pid` and `fence` are its root's;
    `started` when the root started and `agent_started` when its session's agent did (wall
    seconds; None: not known). `footprint` is its anonymous memory, resident or in swap,
    leasehold's own processes in it (`spared`) left out; `leasehold` is True when it has no
    other process. `expected` is what its leases declared or learned (None: nothing). `leases`
    are the leases its processes run under; `alone` is True when it runs a command again alone
    after the watchdog stopped it."""

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
    leases: tuple[str, ...] = ()
    alone: bool = False

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


@dataclass
class Paused:
    """A tree the watchdog paused (`choice`), since `since`, and, once it runs again, since
    when (`resumed`): it holds new work until it ends."""

    choice: Choice
    since: float
    resumed: float | None = None

    @property
    def key(self) -> tuple[int, str | None]:
        return self.choice.work.key


@dataclass(frozen=True)
class Act:
    """What the watchdog decided to do now: `pause`, `resume` or `stop` the tree of `choice`,
    with `why` in words (for a stop: `critical`, `runaway`, `large` or `max_pause`)."""

    action: str
    choice: Choice
    why: str

    @property
    def work(self) -> Work:
        return self.choice.work


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


@dataclass
class _State:
    now: float = 0.0
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
    """The level the machine's memory is at, the tree first in line, and what the watchdog does
    or would do. Not thread-safe: the caller serialises every call."""

    def __init__(self, settings: WatchdogSettings, limits: WatchdogLimits) -> None:
        self.settings = settings
        self.limits = limits
        self.acting = settings.mode == "on"
        self._protect = [re.compile(p) for p in (*PROTECT, *settings.protect)]
        self._work: list[Work] = []
        self._growth: dict[tuple[int, str | None], list[tuple[float, int]]] = {}
        self._runaways: set[tuple[int, str | None]] = set()
        self._state = _State()
        #: What the last decision would have done: (action, tree key), or None.
        self._last: tuple[str, tuple[int, str | None] | None] | None = None
        #: The trees paused, and those resumed that have not ended, oldest pause first.
        self._paused: dict[tuple[int, str | None], Paused] = {}
        #: Trees not to choose again until they are read anew: being stopped, or not paused.
        self._skip: set[tuple[int, str | None]] = set()
        #: When the level last turned `critical` and `ok` (None: it is not at that level).
        self._critical_since: float | None = None
        self._ok_since: float | None = None
        #: When the last tree was paused, with what was available then, and the last stopped.
        self._last_pause: tuple[float, int] | None = None
        self._last_stop: float | None = None
        #: Whether new work was held at the last reading.
        self._holding = False
        self.stopped: deque[dict] = deque(maxlen=STOPPED_KEPT)

    # --- readings ----------------------------------------------------------------------------

    def work(self, trees: list[Work], *, now: float) -> list[Paused]:
        """The sessions' trees as just read, at `now`. The trees it paused that have ended
        since, which hold nothing any more."""
        self._work = list(trees)
        seen = set()
        for w in trees:
            seen.add(w.key)
            _keep(self._growth.setdefault(w.key, []), now, w.footprint)
        for gone in set(self._growth) - seen:
            del self._growth[gone]
        self._runaways &= seen
        self._skip = set()
        ended = [p for k, p in self._paused.items() if k not in seen]
        for p in ended:
            del self._paused[p.key]
        return ended

    def observe(self, memory: Memory) -> list[dict]:
        """Judge the machine at `memory`: the events the decision gives. In watch mode they say
        what would be done (new heavy work would be held, a tree would now be paused), each with
        `would` true; acting, that a tree is newly a runaway, and that new work is now held."""
        state = _State(now=memory.now)
        self._state = state
        if memory.how == "none" or memory.available is None:
            state.reason = f"the watchdog cannot see memory here: {memory.reason or 'not read'}"
            self._last = None
            self._critical_since = self._ok_since = None
            return []
        state.available = memory.available
        state.swap_free = memory.swap_free()
        state.swap_open = memory.swap_open()
        state.stall_recent = memory.stall_recent
        state.level, state.reason = judge(memory, self.limits)
        if state.level != "critical":
            self._critical_since = None
        elif self._critical_since is None:
            self._critical_since = memory.now
        if state.level != "ok":
            self._ok_since = None
        elif self._ok_since is None:
            self._ok_since = memory.now
        self._choose(state)
        return self._events(state)

    def _at_act(self, level: str | None) -> bool:
        return level is not None and LEVELS.index(level) >= LEVELS.index(self.settings.act_at)

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
            if w.key in self._paused or w.key in self._skip:
                continue
            why = self._why_not(w)
            if why is None:
                found.append(w)
            else:
                s.passed_over[why] = s.passed_over.get(why, 0) + 1
        s.candidates = len(found)
        first = [w for w in found if not w.alone] or found

        def growth(w: Work) -> float:
            return rates.get(w.key) or 0.0

        runaways = [w for w in first if w.expected and w.footprint > RUNAWAY_OVER * w.expected]
        fastest = max(first, key=growth, default=None)
        if (
            fastest is not None
            and growth(fastest) >= RUNAWAY_MIN_GROWTH
            and growth(fastest) >= RUNAWAY_SHARE * total
        ):
            runaways.append(fastest)
        large = [w for w in first if w.footprint >= self.limits.large]
        if runaways:
            w = max(runaways, key=lambda w: (growth(w), w.footprint))
            s.victim = Choice(w, "runaway", rates.get(w.key))
        elif large:
            w = max(large, key=lambda w: w.footprint)
            s.victim = Choice(w, "large", rates.get(w.key))
        elif first:
            w = max(first, key=lambda w: (w.started or 0.0, w.pid))
            s.victim = Choice(w, "newest", rates.get(w.key))
        if not self._at_act(s.level):
            return
        if s.victim is not None:
            s.would = {"action": "pause"} | s.victim.public()
        else:
            s.would = {"action": "hold"}

    def _events(self, s: _State) -> list[dict]:
        out = []
        v = s.victim
        if v is not None and v.why == "runaway" and v.work.key not in self._runaways:
            self._runaways.add(v.work.key)
            out.append({"type": "runaway", "would": not self.acting, "level": s.level} | v.public())
        if self.acting:
            why = self.held()
            if why is not None and not self._holding:
                out.append({"type": "held", "would": False, "level": s.level, "reason": why})
            self._holding = why is not None
            return out
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

    # --- acting ------------------------------------------------------------------------------

    def decide(self) -> list[Act]:
        """What to do now, at the last reading: nothing in watch mode, or with no level. The
        caller does each through the host and reports it (`paused`, `resumed`, `stopped_tree`,
        or `failed`)."""
        s = self._state
        if not self.acting or s.level is None:
            return []
        now = s.now
        acts: list[Act] = []
        paused = [p for p in self._paused.values() if p.resumed is None]
        newest_first = sorted(paused, key=lambda p: -p.since)
        for p in newest_first:
            if p.choice.why in ("runaway", "large") and now - p.since >= KILL_AFTER_S:
                acts.append(Act("stop", p.choice, p.choice.why))
            elif now - p.since >= self.settings.max_pause_s:
                acts.append(Act("stop", p.choice, "max_pause"))
        chosen = {a.work.key for a in acts}
        if s.level == "critical" and self._critical_since is not None:
            start = max(self._critical_since, self._last_stop or self._critical_since)
            left = [p for p in newest_first if p.key not in chosen]
            if now - start >= KILL_AFTER_S and left:
                first = next((p for p in left if p.choice.why == "runaway"), left[0])
                acts.append(Act("stop", first.choice, "critical"))
        if self._at_act(s.level) and s.victim is not None:
            last = self._last_pause
            falling = (
                last is not None
                and now - last[0] >= STEP_S
                and s.available is not None
                and s.available < last[1] - FALLING
            )
            if not paused or falling:
                acts.append(Act("pause", s.victim, s.victim.why))
        if s.level == "ok" and paused and self._ok_since is not None:
            oldest = min(paused, key=lambda p: p.since)
            margin = max(0.0, oldest.choice.growth or 0.0) * LEAD_S
            room = (
                not s.swap_open
                or s.available is None
                or s.available - margin >= self.limits.hold_below
            )
            if now - self._ok_since >= SETTLE_S and room:
                acts.append(Act("resume", oldest.choice, "ok"))
        return acts

    def paused(self, act: Act) -> None:
        """`act`'s tree is paused now."""
        s = self._state
        self._paused[act.work.key] = Paused(act.choice, s.now)
        self._last_pause = (s.now, s.available or 0)

    def resumed(self, act: Act) -> Paused:
        """`act`'s tree runs again; it holds new work until it ends."""
        p = self._paused[act.work.key]
        p.resumed = self._state.now
        self._ok_since = self._state.now
        return p

    def stopped_tree(self, act: Act) -> Paused | None:
        """`act`'s tree was stopped: no longer paused (its pause, if it had one), and not chosen
        again."""
        self._last_stop = self._state.now
        self._skip.add(act.work.key)
        p = self._paused.pop(act.work.key, None)
        record = act.choice.public() | {"stopped_for": act.why}
        if p is not None:
            record["paused_s"] = round(self._state.now - p.since, 1)
        self.stopped.append(record)
        return p

    def failed(self, act: Act) -> None:
        """`act` could not be done: its tree is not chosen again until it is read anew."""
        self._skip.add(act.work.key)

    def held(self) -> str | None:
        """Why new heavy work waits now, in words; None while it does not, and in watch mode."""
        if not self.acting:
            return None
        waiting = [p for p in self._paused.values() if p.resumed is None]
        if waiting:
            return f"{_tree(waiting[-1].choice.work)} is paused until memory frees"
        if self._paused:
            p = list(self._paused.values())[-1]
            return f"{_tree(p.choice.work)} was paused, and new work waits until it ends"
        s = self._state
        if self._at_act(s.level):
            return f"memory is short: {s.reason}"
        return None

    def paused_trees(self) -> list[Paused]:
        """The trees paused or resumed and not ended, oldest pause first."""
        return list(self._paused.values())

    # --- reporting ---------------------------------------------------------------------------

    def interval(self) -> float:
        """How long until the machine's memory should be read again."""
        s = self._state
        if s.level is None:
            return IDLE_S
        if s.level != "ok" or self._paused:
            return WARN_STEP_S
        roomy = not s.swap_open or (
            s.available is not None and s.available >= FAR * self.limits.hold_below
        )
        quiet = s.stall_recent is None or s.stall_recent < QUIET_STALL
        return IDLE_S if roomy and quiet else STEP_S_READ

    def status(self) -> dict:
        """The watchdog's part of the status object's `watchdog`."""
        s, lim = self._state, self.limits
        paused = [
            p.choice.public()
            | {
                "paused_s": round((p.resumed or s.now) - p.since, 1),
                "resumed": p.resumed is not None,
            }
            for p in self._paused.values()
        ]
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
                "max_pause_s": self.settings.max_pause_s,
            },
            "candidates": s.candidates,
            "passed_over": dict(s.passed_over),
            "victim": None if s.victim is None else s.victim.public(),
            "would": s.would,
            "held": self.held(),
            "paused": paused,
            "stopped": list(self.stopped),
        }


def _tree(w: Work) -> str:
    """A tree in words: its command and session."""
    return f"{(w.command or w.name)[:80]} (session {w.session})"
