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

* it holds new heavy work (`held`), and keeps holding while a tree it paused is paused, and
  after it runs again until it ends or settles (`FLAT_S` flat at `ok` with room for it, or
  `max_pause_s` after it ran again): a resumed tree reads its memory back, and new work would
  take the room it needs;
* it pauses the first tree in line, and the next one after `STEP_S` only while available
  memory is still falling (by `FALLING` over the last `STEP_S`);
* it stops (SIGKILL at once: a stopped process does not act on SIGTERM) a tree paused for at
  least `KILL_AFTER_S`: the newest, once the level has been `critical` for `KILL_AFTER_S` since
  the episode began or since the last stop; a paused runaway while the level is at least
  `warn`. One at a time. Any tree paused for `max_pause_s` is stopped whatever else holds;
* it pauses or stops only a tree that would help (`_helps`): a runaway, or one whose stop
  would return at least `HELPS` of the deficit (what available memory lacks of `hold_below`)
  to available memory (`Work.frees`, measured before choosing: forked workers' shared pages
  count once, and memory in swap not at all). When no tree paused or in line would help, the
  shortage is outside the sessions (`outside`): the trees it paused run again, since they
  cannot help, and it only holds new work while the level is at `act_at`. A tree that grows
  enough to help ends that at once;
* it resumes the tree paused longest once the level has been `ok` for `SETTLE_S`, and, while
  available memory counts, once what is available less the tree's growth before its pause over
  `LEAD_S` is at least `hold_below`; then the next, `SETTLE_S` later.

In `watch` mode it does none of this: `would` says what it would do now (`hold`, or `pause` and
the tree), and the events it would send carry `would: true`.

**Which tree.** A tree is one of a session's (`leasehold.sessions.Branch`). First a
runaway: a tree growing by at least `RUNAWAY_MIN_GROWTH` a second over each of the last two
`WINDOW_S` windows, that either holds more than `RUNAWAY_OVER` times what its leases declared
or learned, or grows by at least `RUNAWAY_SHARE` of the machine's fall in available memory.
Then a large tree, holding `LARGE_SHARE` of the machine's memory or more, the largest first.
Then the newest, by its root's start, across all sessions; each among the trees that would
help. A tree of a hook session that is not
a tool's command (`Work.tool`), and a tree running a command again alone after the watchdog
stopped it (`Work.alone`), come last.

**Never chosen:** a tree under `min_size`; one that holds the agent process (a launcher the agent
runs under); one made only of leasehold's own processes (`hold`, the shims, the hooks, the
daemon); one with a coding agent among its processes (`PROTECT_ANY`); one whose heads (its
root, and what its tool shells run) match a built-in or `[watchdog] protect` pattern (MCP
servers, and browsers and editors that are not headless); one whose root
started within `AGENT_START_S` of its agent (servers that start
with the session); and, unless `choose_discovered`, any tree of a session found by name, which
never asked to join. leasehold's own processes in a tree (`spared`) are left out of its
footprint, and left running when it is paused; a command that holds its own lease through the
Python API is not one of them, and is paused with its tree.

Pure: the caller supplies the clock (`now`, one that never steps) and the readings."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field, replace

from leasehold.machine import (
    LEVELS,
    PROTECT,
    PROTECT_ANY,
    STALL_CRITICAL,
    SWAP_FREE,
    WatchdogLimits,
    WatchdogSettings,
)

#: The seconds over which a tree's growth is measured.
WINDOW_S = 5.0
#: A runaway grows by at least this many bytes a second over two windows in a row ...
RUNAWAY_MIN_GROWTH = 20 << 20
#: ... and by at least this share of the machine's fall in available memory (or past
#: `RUNAWAY_OVER`).
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
#: How long a tree stays paused, and the level `critical`, before the watchdog stops it.
KILL_AFTER_S = 5.0
#: A tree helps when stopping it would return at least this share of the deficit to available
#: memory; a paused one keeps helping down to `KEEPS`, so that a deficit near the line does not
#: pause and resume it in turn.
HELPS, KEEPS = 0.25, 0.125
#: A tree that runs again holds new work until its footprint has been flat this long.
FLAT_S = 30.0
#: Why new work waits when no tree in a session would help.
OUTSIDE = "memory is short outside leasehold's sessions"

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
    after the watchdog stopped it.

    What acting on it touches: `members`, the (pid, fence) of each process it would pause or
    stop; `spare`, the processes it never signals (leasehold's own, and the shells that wait
    for them, so that `hold` lives to report the stop); and `scopes`, the kernel groups of its
    leases, paused and stopped as a whole. `lines` are its processes' command lines, tried
    against `PROTECT_ANY`, and `heads` those of its root and of what its tool shells run, tried
    against the other protect patterns. `frees` is what stopping it would return to available
    memory (`host.freeable`; None: not measured). `tool` is False for a tree of a hook
    session that is neither under a tool shell nor in a lease's scope (a server its agent
    started), which is chosen only when nothing else is."""

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
    members: tuple[tuple[int, str | None], ...] = ()
    spare: tuple[int, ...] = ()
    scopes: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    heads: tuple[str, ...] = ()
    frees: int | None = None
    tool: bool = True

    @property
    def key(self) -> tuple[int, str | None]:
        return self.pid, self.fence

    @property
    def gain(self) -> int:
        """What stopping it would return to available memory: `frees` where measured, else its
        footprint."""
        return self.footprint if self.frees is None else self.frees


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
            "frees_bytes": w.frees,
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
    when (`resumed`): it holds new work until it ends, or until it has settled (`flat_since`,
    when its footprint last moved by `FALLING`; `footprint`, what it was then)."""

    choice: Choice
    since: float
    resumed: float | None = None
    flat_since: float | None = None
    footprint: int | None = None

    @property
    def key(self) -> tuple[int, str | None]:
        return self.choice.work.key


@dataclass(frozen=True)
class Act:
    """What the watchdog decided to do now: `pause`, `resume` or `stop` the tree of `choice`,
    with `why` in words (for a stop: `critical`, `runaway` or `max_pause`)."""

    action: str
    choice: Choice
    why: str

    @property
    def work(self) -> Work:
        return self.choice.work


def _at_or_before(samples: list[tuple[float, int]], when: float) -> tuple[float, int] | None:
    """The newest sample taken at or before `when`."""
    found = None
    for sample in samples:
        if sample[0] > when:
            break
        found = sample
    return found


def _rates(samples: list[tuple[float, int]], now: float) -> tuple[float | None, float | None]:
    """Bytes a second over the last `WINDOW_S`, and over the `WINDOW_S` before it (each None
    where the samples do not reach back that far)."""
    if not samples:
        return None, None
    last = samples[-1]
    mid = _at_or_before(samples, now - WINDOW_S)
    if mid is None or last[0] <= mid[0]:
        return None, None
    recent = (last[1] - mid[1]) / (last[0] - mid[0])
    first = _at_or_before(samples, mid[0] - WINDOW_S)
    if first is None or mid[0] <= first[0]:
        return recent, None
    return recent, (mid[1] - first[1]) / (mid[0] - first[0])


def _keep(samples: list[tuple[float, int]], now: float, value: int) -> None:
    """Add a sample, keeping two windows back (`_rates`)."""
    if samples and samples[-1][0] == now:
        samples[-1] = (now, value)
    else:
        samples.append((now, value))
    while len(samples) > 2 and samples[1][0] <= now - 2 * WINDOW_S - IDLE_S:
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
        self._protect_any = [re.compile(p) for p in PROTECT_ANY]
        self._protect = [re.compile(p) for p in (*PROTECT, *settings.protect)]
        self._work: list[Work] = []
        self._growth: dict[tuple[int, str | None], list[tuple[float, int]]] = {}
        #: Available memory as read, two windows back: how fast the machine is falling.
        self._available: list[tuple[float, int]] = []
        self._runaways: set[tuple[int, str | None]] = set()
        self._state = _State()
        #: The level at the last reading, set once it is judged: what `short` reads, from
        #: another thread too.
        self._level: str | None = None
        #: What the last decision would have done: (action, tree key), or None.
        self._last: tuple[str, tuple[int, str | None] | None] | None = None
        #: The trees paused, and those resumed that have not ended, oldest pause first.
        self._paused: dict[tuple[int, str | None], Paused] = {}
        #: Trees not to choose again until they are read anew: being stopped, or not paused.
        self._skip: set[tuple[int, str | None]] = set()
        #: When the level last turned `critical` and `ok` (None: it is not at that level).
        self._critical_since: float | None = None
        self._ok_since: float | None = None
        #: When the last tree was paused or stopped, and the last stopped (this episode).
        self._last_act: float | None = None
        self._last_stop: float | None = None
        #: Whether the shortage is outside the sessions, at the last reading: no tree paused or
        #: in line would help.
        self.outside = False
        #: Why new work was held at the last reading (None: it was not).
        self._holding: str | None = None
        self.stopped: deque[dict] = deque(maxlen=STOPPED_KEPT)

    # --- readings ----------------------------------------------------------------------------

    def work(self, trees: list[Work], *, now: float) -> list[Paused]:
        """The sessions' trees as just read, at `now`. The trees it paused that have ended
        since, which hold nothing any more."""
        self._work = list(trees)
        seen = {}
        for w in trees:
            seen[w.key] = w
            _keep(self._growth.setdefault(w.key, []), now, w.footprint)
        for gone in set(self._growth) - set(seen):
            del self._growth[gone]
        self._runaways &= set(seen)
        self._skip = set()
        ended = [p for k, p in self._paused.items() if k not in seen]
        for p in ended:
            del self._paused[p.key]
        for p in self._paused.values():
            fp = seen[p.key].footprint
            if p.resumed is not None and (p.footprint is None or abs(fp - p.footprint) >= FALLING):
                p.footprint, p.flat_since = fp, now
        return ended

    def observe(self, memory: Memory) -> list[dict]:
        """Judge the machine at `memory`: the events the decision gives. In watch mode they say
        what would be done (new heavy work would be held, a tree would now be paused), each with
        `would` true; acting, that a tree is newly a runaway, and that new work is now held, or
        held for another reason."""
        state = _State(now=memory.now)
        self._state = state
        if memory.how == "none" or memory.available is None:
            state.reason = f"the watchdog cannot see memory here: {memory.reason or 'not read'}"
            self._last = None
            self._critical_since = self._ok_since = self._level = None
            return []
        state.available = memory.available
        state.swap_free = memory.swap_free()
        state.swap_open = memory.swap_open()
        state.stall_recent = memory.stall_recent
        state.level, state.reason = judge(memory, self.limits)
        _keep(self._available, memory.now, memory.available)
        if state.level != "critical":
            self._critical_since = None
        elif self._critical_since is None:
            self._critical_since = memory.now
        if state.level != "ok":
            self._ok_since = None
        else:
            if self._ok_since is None:
                self._ok_since = memory.now
            self._last_stop = None
        self._let_go(state)
        self._choose(state)
        self._level = state.level
        return self._events(state)

    def _let_go(self, s: _State) -> None:
        """Stop holding new work for a tree that runs again once it has settled: its footprint
        flat for `FLAT_S` while the level is `ok` with room for it, or `max_pause_s` after it
        ran again, whichever comes first."""
        for p in list(self._paused.values()):
            if p.resumed is None:
                continue
            growth = _rates(self._growth.get(p.key, []), s.now)[0] or 0.0
            room = (
                not s.swap_open
                or s.available is None
                or s.available >= self.limits.hold_below + max(0.0, growth) * LEAD_S
            )
            settled = (
                s.level == "ok"
                and room
                and p.flat_since is not None
                and s.now - p.flat_since >= FLAT_S
            )
            if settled or s.now - p.resumed >= self.settings.max_pause_s:
                del self._paused[p.key]

    def _at(self, level: str | None, least: str) -> bool:
        return level is not None and LEVELS.index(level) >= LEVELS.index(least)

    def _at_act(self, level: str | None) -> bool:
        return self._at(level, self.settings.act_at)

    def short(self) -> bool:
        """Whether memory is short, or a tree is paused: while it is, what stopping each tree
        would free is worth measuring."""
        return self._level not in (None, "ok") or bool(self._paused)

    def _helps(self, w: Work, why: str | None, share: float = HELPS) -> bool:
        """Whether stopping `w` would help: a runaway, or a stop that returns `share` of the
        deficit (what available memory lacks of `hold_below`) to available memory. Every tree
        helps while available memory does not count, or nothing lacks."""
        s = self._state
        if why == "runaway" or not s.swap_open or s.available is None:
            return True
        deficit = self.limits.hold_below - s.available
        return deficit <= 0 or w.gain >= share * deficit

    def _falling(self, s: _State) -> bool:
        """Whether available memory fell by `FALLING` over the last `STEP_S`."""
        then = _at_or_before(self._available, s.now - STEP_S)
        return then is not None and s.available is not None and s.available < then[1] - FALLING

    # --- choosing ----------------------------------------------------------------------------

    def _why_not(self, w: Work) -> str | None:
        """Why `w` is never chosen; None when it may be."""
        if w.agent_line:
            return "agent"
        if w.leasehold:
            return "leasehold"
        if w.source == "auto" and not self.settings.choose_discovered:
            return "discovered"
        lines = w.lines or (w.command or w.name,)
        heads = w.heads or (w.command or w.name,)
        if any(p.search(line) for line in lines for p in self._protect_any) or any(
            p.search(line) for line in heads for p in self._protect
        ):
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

    def _runaway(self, w: Work, fall: float | None) -> float | None:
        """`w`'s growth when it runs away: growing by `RUNAWAY_MIN_GROWTH` a second over two
        windows in a row, and either holding over `RUNAWAY_OVER` times what its leases expect or
        growing by `RUNAWAY_SHARE` of the machine's fall in available memory. A tree growing
        while something else takes the memory is not one."""
        recent, earlier = _rates(self._growth.get(w.key, []), self._state.now)
        if recent is None or earlier is None:
            return None
        if min(recent, earlier) < RUNAWAY_MIN_GROWTH:
            return None
        over = bool(w.expected) and w.footprint > RUNAWAY_OVER * (w.expected or 0)
        share = fall is not None and fall > 0 and recent >= RUNAWAY_SHARE * fall
        return recent if over or share else None

    def _choose(self, s: _State) -> None:
        self.outside = False
        if s.level == "ok":
            return
        fall = _rates(self._available, s.now)[0]
        fall = None if fall is None else -fall
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
        first = (
            [w for w in found if not w.alone and w.tool]
            or [w for w in found if not w.alone]
            or found
        )

        def growth(w: Work) -> float | None:
            return _rates(self._growth.get(w.key, []), s.now)[0]

        runaways = [(r, w) for w in first if (r := self._runaway(w, fall)) is not None]
        first = [w for w in first if self._helps(w, None)]
        large = [w for w in first if w.gain >= self.limits.large]
        if runaways:
            _, w = max(runaways, key=lambda rw: (rw[0], rw[1].footprint))
            s.victim = Choice(w, "runaway", growth(w))
        elif large:
            w = max(large, key=lambda w: w.gain)
            s.victim = Choice(w, "large", growth(w))
        elif first:
            w = max(first, key=lambda w: (w.started or 0.0, w.pid))
            s.victim = Choice(w, "newest", growth(w))
        if not self._at_act(s.level):
            return
        paused = [p for p in self._paused.values() if p.resumed is None]
        self.outside = s.victim is None and not any(
            self._helps(self._now_of(p), p.choice.why, KEEPS) for p in paused
        )
        if s.victim is not None:
            s.would = {"action": "pause"} | s.victim.public()
        else:
            s.would = {"action": "hold"}

    def _now_of(self, p: Paused) -> Work:
        """Paused tree `p` as last read."""
        return next((w for w in self._work if w.key == p.key), p.choice.work)

    def _as_read(self, p: Paused) -> Choice:
        """Paused tree `p`'s choice, with the tree as last read: a stop reaches the leases and
        processes found since its pause."""
        return replace(p.choice, work=self._now_of(p))

    def _events(self, s: _State) -> list[dict]:
        out = []
        v = s.victim
        if v is not None and v.why == "runaway" and v.work.key not in self._runaways:
            self._runaways.add(v.work.key)
            out.append({"type": "runaway", "would": not self.acting, "level": s.level} | v.public())
        if self.acting:
            why = self.held()
            began = why is not None and (
                self._holding is None or (self.outside and OUTSIDE not in self._holding)
            )
            if began:
                out.append(
                    {"type": "held", "would": False, "level": s.level, "reason": why}
                    | {"outside": self.outside}
                )
            self._holding = why
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
        or `failed`).

        A tree is stopped only `KILL_AFTER_S` after its pause, one at a time, and only while it
        would help; after a stop, nothing more is paused unless available memory is still
        falling. While the shortage is outside the sessions (`outside`), the trees paused run
        again. A tree paused for `max_pause_s` is stopped whatever else holds."""
        s = self._state
        if not self.acting or s.level is None:
            return []
        now = s.now
        acts: list[Act] = []
        paused = [p for p in self._paused.values() if p.resumed is None]
        longest = max(paused, key=lambda p: now - p.since, default=None)
        if longest is not None and now - longest.since >= self.settings.max_pause_s:
            return [Act("stop", self._as_read(longest), "max_pause")]
        if self.outside:
            return [Act("resume", p.choice, "outside") for p in paused]
        ripe = [
            p
            for p in sorted(paused, key=lambda p: -p.since)
            if now - p.since >= KILL_AFTER_S and self._helps(self._now_of(p), p.choice.why, KEEPS)
        ]
        if ripe:
            runaway = next((p for p in ripe if p.choice.why == "runaway"), None)
            start = self._critical_since
            if start is not None and self._last_stop is not None:
                start = max(start, self._last_stop)
            if runaway is not None and self._at(s.level, "warn"):
                acts.append(Act("stop", self._as_read(runaway), "runaway"))
            elif s.level == "critical" and start is not None and now - start >= KILL_AFTER_S:
                acts.append(Act("stop", self._as_read(ripe[0]), "critical"))
        if self._at_act(s.level) and s.victim is not None and not acts:
            spaced = self._last_act is None or now - self._last_act >= STEP_S
            first = not paused and self._last_stop is None
            if first or (spaced and self._falling(s)):
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
        self._last_act = s.now

    def resumed(self, act: Act) -> Paused:
        """`act`'s tree runs again; it holds new work until it ends or settles, unless it ran
        again because it could not help (`outside`)."""
        p = self._paused[act.work.key]
        now = self._state.now
        if act.why == "outside":
            del self._paused[act.work.key]
            p.resumed = now
            return p
        p.resumed = p.flat_since = now
        p.footprint = act.work.footprint
        self._ok_since = now
        return p

    def stopped_tree(self, act: Act) -> Paused | None:
        """`act`'s tree was stopped: no longer paused (its pause, if it had one), and not chosen
        again."""
        s = self._state
        self._last_stop = self._last_act = s.now
        self._skip.add(act.work.key)
        p = self._paused.pop(act.work.key, None)
        record = act.choice.public() | {"stopped_for": act.why}
        if p is not None:
            record["paused_s"] = round(s.now - p.since, 1)
        self.stopped.append(record)
        return p

    def failed(self, act: Act) -> None:
        """`act` could not be done: its tree is not chosen again until it is read anew."""
        self._skip.add(act.work.key)

    def held(self) -> str | None:
        """Why new heavy work waits now, in words; None while it does not, and in watch mode."""
        if not self.acting:
            return None
        s = self._state
        if self.outside:
            return f"{OUTSIDE}: {s.reason}"
        waiting = [p for p in self._paused.values() if p.resumed is None]
        if waiting:
            return f"{_tree(waiting[-1].choice.work)} is paused until memory frees"
        if self._paused:
            p = list(self._paused.values())[-1]
            return f"{_tree(p.choice.work)} was paused, and new work waits until it settles"
        if self._at_act(s.level):
            return f"memory is short: {s.reason}"
        return None

    def paused_trees(self) -> list[Paused]:
        """The trees paused or resumed and not settled or ended, oldest pause first."""
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
            "outside": self.outside,
            "paused": paused,
            "stopped": list(self.stopped),
        }


def _tree(w: Work) -> str:
    """A tree in words: its command and session."""
    return f"{(w.command or w.name)[:80]} (session {w.session})"
