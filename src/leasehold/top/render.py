"""The screen as lines, as pure functions of what the daemon said.

`render` takes a status object (`GET /v1/status`), recent events (`GET /v1/events`, oldest
first) and a size, and returns the screen as lines of `(text, style)` segments: exactly `height`
lines of exactly `width` cells each, never wrapped. When the height is short, whole sections are
dropped, least important first, and long lists are cut with a count of what is not shown.
`footer`, `help_lines` and `waiting_screen` are the live view's other lines.

Styles are strings: `""` (plain), `title`, `dim`, `bold`, `ok`, `warn`, `bad`, `wait` (why a
lease waits), `key`, `pool:NAME` (a mark in a pool's colour), `bay:NAME` (a slot a lease of that
pool holds), `reserved:NAME` (a slot that pool reserves, free) and `revoked`. Kinds never rely on
colour alone: each has its own mark.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from leasehold.events import EventType
from leasehold.kinds import KINDS
from leasehold.top.cells import (
    Line,
    _ascii,
    _choose,
    _clean,
    _clip,
    _line_cells,
    _Out,
    _pad,
    _rpad,
    _spread,
    cells,
    fit,
)
from leasehold.units import size

#: The lease orders, in the order `s` cycles through them.
SORTS = ("pool", "held", "weight", "user")
#: The pressure resources, in the order shown.
RESOURCES = ("cpu", "memory", "io")
#: Columns left of every section for its name.
GUTTER = 10


@dataclass(frozen=True)
class Glyphs:
    """The characters a render draws with."""

    marks: Mapping[str, str]  # by lease kind
    other: str  # any other kind or pool
    free: str  # a free slot
    reserved: str  # a free slot a pool reserves
    waiting: str  # the rest of a waiting lease's width
    held: str  # bar: held
    open: str  # bar: free within the pool's max
    beyond: str  # bar: past the pool's max
    spark: str  # sparkline levels, lowest first
    blocked: str  # before why a lease waits
    sep: str
    cut: str  # in place of the end of clipped text


UNICODE = Glyphs(
    marks={"work": "■", "external": "▲", "lend": "○", "maintenance": "◇", "observed": "▨"},
    other="◆",
    free="·",
    reserved="╌",
    waiting="░",
    held="█",
    open="░",
    beyond="·",
    spark="▁▂▃▄▅▆▇█",
    blocked="▸",
    sep="·",
    cut="…",
)
ASCII = Glyphs(
    marks={"work": "#", "external": "^", "lend": "o", "maintenance": "M", "observed": "%"},
    other="*",
    free=".",
    reserved="-",
    waiting="~",
    held="#",
    open="-",
    beyond=".",
    spark="_.:-=+*#",
    blocked=">",
    sep="|",
    cut="~",
)


# --- numbers and words ---------------------------------------------------------------------------


def _duration(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600:02d}h"


def _span(seconds: float) -> str:
    """A configured length of time, in its largest whole unit where it has one."""
    s = int(seconds)
    if s and s % 3600 == 0:
        return f"{s // 3600}h"
    if s and s % 60 == 0:
        return f"{s // 60}m"
    return _duration(seconds)


def _gib(n: float) -> str:
    v = n / 2**30
    return f"{v:.0f} GiB" if abs(v - round(v)) < 0.05 else f"{v:.1f} GiB"


def _slots(n: int) -> str:
    return f"{n} slot" if n == 1 else f"{n} slots"


def _range(x: Mapping) -> tuple[int, int]:
    """The least and the most weight a lease or waiting item asked for (its weight for one
    from a daemon without ranges)."""
    weight = int(x.get("weight") or 0)
    least = int(x.get("min_weight") or weight)
    return least, max(least, int(x.get("max_weight") or least))


def _weight_cell(x: Mapping) -> str:
    """A lease's weight, as the W column shows it: `3`, `3/2-4` when it asked a range, and
    `3>4/2-4` when an elastic lease's target is not its weight."""
    least, most = _range(x)
    weight = str(x.get("weight"))
    target = x.get("target")
    if target is not None and str(target) != weight:
        weight = f"{weight}>{target}"
    return weight if least == most else f"{weight}/{least}-{most}"


def _needs(x: Mapping) -> str:
    """What a waiting item needs: `2 slots`, or `2-4 slots` for a range."""
    least, most = _range(x)
    return _slots(least) if least == most else f"{least}-{most} slots"


def _pct(v: float) -> str:
    return f"{v:.1f}%"


def _what(labels: Mapping | None) -> str:
    """What a lease is doing, from its labels: its command, its job, or its observe rule."""
    labels = labels or {}
    if labels.get("cmd"):
        return str(labels["cmd"])
    job = " ".join(str(labels[k]) for k in ("repository", "job") if labels.get(k))
    return job or str(labels.get("rule") or "")


def _mark(kind: str | None, g: Glyphs) -> str:
    return g.marks.get(kind or "", g.other)


# --- sections ------------------------------------------------------------------------------------


@dataclass
class _Section:
    """A block of lines, in groups that are kept or dropped whole. When it is cut, the first
    `head` groups stay and `more(hidden)` says how many groups are not shown; with `trim`, it is
    cut to any length without saying so; with neither, it is shown whole or not at all."""

    priority: int
    groups: list[list[Line]]
    head: int = 0
    more: Callable[[int], Line] | None = None
    trim: bool = False
    #: The most lines it is given before every section has had its share.
    cap: int = 1000
    lines: list[Line] = field(init=False)

    def __post_init__(self) -> None:
        self.lines = [line for group in self.groups for line in group]

    def fit(self, n: int) -> list[Line]:
        if len(self.lines) <= n:
            return self.lines
        if self.trim:
            return self.lines[:n]
        if self.more is None:
            return []
        out = [line for group in self.groups[: self.head] for line in group]
        shown = self.head
        for group in self.groups[self.head :]:
            if len(out) + len(group) + 1 > n:
                break
            out += group
            shown += 1
        if shown == self.head:
            return []
        return [*out, self.more(len(self.groups) - shown)]

    def smallest(self) -> int:
        return next((n for n in range(1, len(self.lines) + 1) if self.fit(n)), 0)


def _labelled(label: str, rows: list[_Out]) -> list[Line]:
    """Rows built after the gutter, with `label` in the gutter of the first."""
    out = []
    for i, row in enumerate(rows):
        gutter: Line = [(_pad(label, GUTTER), "title")] if i == 0 else [(" " * GUTTER, "")]
        out.append(gutter + row.segs)
    return out


@dataclass
class _View:
    status: Mapping
    events: Sequence[Mapping]
    width: int
    now: float
    history: Mapping[str, Sequence[float]]
    sort: str
    g: Glyphs

    @property
    def inner(self) -> int:
        return max(0, self.width - GUTTER)

    def pools(self) -> Mapping[str, Mapping]:
        return self.status.get("pools") or {}

    def slots(self) -> int:
        return int(self.status.get("slots") or (self.status.get("ceiling") or {}).get("slots") or 0)

    def held(self) -> int:
        if self.status.get("held") is not None:
            return int(self.status["held"])
        return sum(int(p.get("held_weight") or 0) for p in self.pools().values())

    def pool_order(self) -> dict[str, int]:
        return {name: i for i, name in enumerate(self.pools())}


def _header(v: _View) -> _Section:
    st, g = v.status, v.g
    machine = st.get("machine") or {}
    facts = st.get("facts") or {}
    parts: list[tuple[int, Line]] = [
        (1, [("leasehold  ", "dim")]),
        (0, [(_clean(machine.get("name") or "?"), "title")]),
    ]
    if st.get("mode"):
        parts.append((2, [(f"  {_clean(st['mode'])}", "dim")]))
    if st.get("version"):
        parts.append((3, [(f"  v{_clean(st['version'])}", "dim")]))
    labels = machine.get("labels") or {}
    if labels:
        text = " ".join(f"{k}={val}" for k, val in labels.items())
        parts.append((4, [(f"  {_clean(text)}", "dim")]))
    if facts.get("cpus_logical"):
        parts.append((5, [(f"  {facts['cpus_logical']} cpus", "dim")]))
    if facts.get("memory_bytes"):
        parts.append((6, [(f"  {_gib(facts['memory_bytes'])}", "dim")]))

    slots, held = v.slots(), v.held()
    over = int(st.get("overcommitted") or max(0, held - slots))
    right = _Out()
    gauge_w = min(24, max(slots, 1))
    spans = [
        (int(p.get("held_weight") or 0), f"pool:{name}", g.held) for name, p in v.pools().items()
    ]
    right.extend(_bar(spans, total=max(slots, held), width=gauge_w, fill=(g.open, "dim")))
    right.put(f"  {held}/{slots} held", "bold")
    if over:
        right.put(f"  {over} over", "bad")
    elif held >= slots:
        right.put("  FULL", "warn")
    else:
        right.put(f"  {slots - held} free", "ok")
    memory = st.get("memory") or {}
    counting = bool(memory.get("counted"))
    if counting and memory.get("budget_bytes"):
        used, budget = int(memory.get("held_bytes") or 0), int(memory["budget_bytes"])
        style = "bad" if used > budget else "warn" if used >= budget * 0.9 else "dim"
        right.put(f"  mem {used / 2**30:.1f}/{_gib(budget)}", style)
    queued = len(st.get("queue") or [])
    if queued:
        right.put(f"  {queued} waiting", "warn")
    if (st.get("guard") or {}).get("on"):
        right.put("  guard: memory short" if counting else "  guard: reserved slots only", "bad")
    if (st.get("contain") or {}).get("mode") == "off":
        parts.append((7, [("  contain off", "dim")]))
    room = max(12, v.width - right.cells - 2)
    return _Section(0, [[_spread(_choose(parts, room), right.segs, v.width)]])


def _bar(
    spans: Sequence[tuple[int, str, str]], *, total: int, width: int, fill: tuple[str, str]
) -> Line:
    """`width` cells standing for `total`: each span (amount, style, char) in turn, then
    `fill` (char, style) for the rest."""
    out: Line = []
    if width <= 0:
        return out
    scale = width / total if total > 0 else 0
    start = 0
    used = 0
    for amount, style, char in spans:
        end = round((start + amount) * scale)
        n = max(0, min(width, end) - used)
        if n:
            out.append((char * n, style))
            used += n
        start += amount
    if used < width:
        out.append((fill[0] * (width - used), fill[1]))
    return out


def _nice_top(values: Sequence[float]) -> float:
    """The top of a sparkline's scale: the first of 10, 20, 50 and 100 that is no lower than
    every value."""
    peak = max(values, default=0.0)
    return next((top for top in (10.0, 20.0, 50.0) if peak <= top), 100.0)


def sparkline(values: Sequence[float], width: int, top: float, g: Glyphs) -> str:
    """The last `width` values as levels from 0 to `top`, right-aligned in `width` cells."""
    levels = g.spark
    tail = list(values)[-width:] if width > 0 else []
    marks = [
        levels[max(0, min(len(levels) - 1, round(val / top * (len(levels) - 1))))] for val in tail
    ]
    return " " * (width - len(marks)) + "".join(marks)


def _row(parts: Sequence[tuple[int, Line]], room: int) -> _Out:
    return _Out().extend(_choose(parts, room))


def _pressure(v: _View) -> _Section:
    st, g = v.status, v.g
    load = st.get("load") or {}
    pressure = load.get("pressure") or {}
    thresholds = (st.get("lend") or {}).get("thresholds") or {}
    spark_w = max(8, min(40, v.inner // 4))
    rows = []
    if not any(pressure.get(r) for r in RESOURCES):
        rows.append(_Out().put("no PSI on this machine (Linux 4.20+ has /proc/pressure)", "dim"))
    for res in RESOURCES if rows == [] else ():
        psi = pressure.get(res) or {}
        some = (psi.get("some") or {}).get("avg10")
        if some is None:
            rows.append(_Out().put(_pad(res, 8)).put("unknown", "dim"))
            continue
        limit = thresholds.get(res)
        above = limit is not None and some > limit
        main: Line = [(_pad(res, 8), ""), (_rpad(_pct(some), 6), "warn" if above else "bold")]
        samples = list(v.history.get(res) or ())
        if samples:
            top = _nice_top([*samples, some])
            main += [("  ", ""), (sparkline(samples, spark_w, top, g), "warn" if above else "")]
            main.append((f" {_pad(f'0-{top:g}%', 6)}", "dim"))
        parts: list[tuple[int, Line]] = [(0, main)]
        if above:
            parts.append((1, [(f"  above the {limit:g}% lending limit", "warn")]))
        avg60 = (psi.get("some") or {}).get("avg60")
        if avg60 is not None:
            parts.append((2, [(f"  60s {_pct(avg60)}", "dim")]))
        full = (psi.get("full") or {}).get("avg10")
        if full is not None:
            parts.append((3, [(f"  full {_pct(full)}", "dim")]))
        rows.append(_row(parts, v.inner))
    avg = load.get("loadavg")
    if avg:
        parts = [(0, [(_pad("load", 8), ""), ("  ".join(f"{x:.1f}" for x in avg), "bold")])]
        cpus = (st.get("facts") or {}).get("cpus_logical")
        if cpus:
            parts.append((1, [(f"  on {cpus} cpus", "dim")]))
    else:
        parts = [(0, [(_pad("load", 8), ""), ("unknown", "dim")])]
    avail, total = load.get("mem_available_bytes"), load.get("mem_total_bytes")
    if avail is not None and total:
        parts.append((2, [(f"    memory available {_gib(avail)} of {_gib(total)}", "dim")]))
    rows.append(_row(parts, v.inner))
    return _Section(4, [_labelled("PRESSURE", rows)])


def _lane_label(lease: Mapping, room: int, g: Glyphs) -> str:
    """A lease's mark, holder and what it runs, in `room` cells: what it runs is cut short, or
    left out when fewer than four cells of it would show."""
    head = _mark(lease.get("kind"), g) + _clean(lease.get("user") or "-")
    what = _clean(_what(lease.get("labels")))
    if not what or cells(head) >= room:
        return _clip(head, room)
    whole = f"{head} {what}"
    if cells(whole) <= room:
        return whole
    if room - cells(head) - 1 >= 4 + cells(g.cut):
        return _clip(whole, room, g.cut)
    return head


def _slots_section(v: _View) -> _Section:
    """One bay per slot, numbered; each lease spans as many bays as it weighs, held in one lane
    and waiting in a second. Bays narrower than one cell are drawn to scale."""
    st, g = v.status, v.g
    slots, held = v.slots(), v.held()
    units = max(slots, held, 1)
    lane = v.inner
    if lane >= units:
        lane = lane // units * units
    scale = lane / units

    def col(slot: float) -> int:
        return round(slot * scale)

    # Number every bay when each number fits its bay, else bay 1 and every `step`th.
    room = len(str(units)) + 1
    step = next(
        (n for n in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000) if n * scale >= room),
        units,
    )
    numbers = _Out()
    for i in sorted({1, *range(step, units + 1, step)}):
        label = str(i)
        start = col(i - 1) + max(0, (col(i) - col(i - 1) - len(label)) // 2)
        if numbers.cells and start <= numbers.cells:
            continue
        numbers.to(start).put(label, "bad" if i > slots else "dim")

    def lay(row: _Out, at: float, weight: int, text: Callable[[int], str], style: str, fill: str):
        """Draw a block of `weight` bays from bay `at`, one cell short so blocks stay apart."""
        a, b = col(at), col(at + weight)
        if b - a <= 0 or a >= lane:
            return
        row.to(a)
        if b - a == 1:
            row.put(text(1)[:1] or fill, style)
            return
        body = text(b - a - 1)
        row.put(body + fill * (b - a - 1 - cells(body)), style).put(" ")

    held_row = _Out()
    pos = 0
    leases = list(st.get("leases") or [])
    order = v.pool_order()
    for name, pool in v.pools().items():
        for item in st.get("standing") or []:
            if item.get("pool") == name:
                weight = int(item.get("weight") or 0)
                label = _mark(item.get("kind"), g) + _clean(item.get("name") or "")
                lay(held_row, pos, weight, lambda n, t=label: _clip(t, n), f"bay:{name}", " ")
                pos += weight
        mine = [x for x in leases if x.get("pool") == name]
        for lease in sorted(mine, key=lambda x: x.get("acquired_at") or 0):
            weight = int(lease.get("weight") or 0)
            style = "revoked" if lease.get("revoked") else f"bay:{name}"
            lay(held_row, pos, weight, lambda n, x=lease: _lane_label(x, n, g), style, " ")
            pos += weight
        spare = int(pool.get("reserved") or 0) - int(pool.get("held_weight") or 0)
        if spare > 0:
            text = _clean(name)
            lay(held_row, pos, spare, lambda n, t=text: _clip(t, n), f"reserved:{name}", g.reserved)
            pos += spare
    for lease in leases:
        if lease.get("pool") not in order:
            weight = int(lease.get("weight") or 0)
            lay(held_row, pos, weight, lambda n, x=lease: _lane_label(x, n, g), "bad", " ")
            pos += weight
    held_row.to(col(pos))
    if lane > held_row.cells:
        held_row.put(g.free * (lane - held_row.cells), "dim")

    lines = _labelled("SLOTS", [numbers])
    lines.append([(_pad("  held", GUTTER), "dim"), *held_row.segs])
    queue = list(st.get("queue") or [])
    if queue:
        wait_row = _Out()
        pos = 0
        for item in queue:
            weight = int(item.get("weight") or 0)
            style = f"pool:{item.get('pool')}"
            lay(wait_row, pos, weight, lambda n, x=item: _lane_label(x, n, g), style, g.waiting)
            pos += weight
        lines.append([(_pad("  waiting", GUTTER), "dim"), *wait_row.segs])
    return _Section(1, [lines])


@dataclass
class _Column:
    """One column of a table: a title (may be empty) and one cell per row."""

    title: str
    rows: list[Line]
    #: When the table is too wide, the column with the lowest `keep` goes first.
    keep: int
    right: bool = False

    @property
    def width(self) -> int:
        return max([cells(self.title), *(_line_cells(r) for r in self.rows)])

    def cell(self, i: int) -> Line:
        line = self.rows[i]
        gap = (" " * (self.width - _line_cells(line)), "")
        return [gap, *line] if self.right else [*line, gap]

    def head(self) -> str:
        return (_rpad if self.right else _pad)(self.title, self.width)


def _narrow(cols: list[_Column], room: int) -> list[_Column]:
    """`cols` less the least kept ones, until they and a two-cell gap after each fit `room`."""
    cols = list(cols)
    while sum(c.width + 2 for c in cols) > room and len(cols) > 1:
        cols.remove(min(cols, key=lambda c: c.keep))
    return cols


def _pools(v: _View) -> _Section:
    g = v.g
    pools = v.pools()
    if not pools:
        return _Section(6, [_labelled("POOLS", [_Out().put("none", "dim")])])
    slots = max(v.slots(), 1)
    bar_w = max(8, min(24, v.inner // 4))
    if bar_w >= slots:
        bar_w = bar_w // slots * slots
    items = list(pools.items())

    def bar(name: str, p: Mapping) -> Line:
        held, cap = int(p.get("held_weight") or 0), int(p.get("max") or 0)
        spans = [(min(held, cap), f"pool:{name}", g.held)]
        spans.append((held - cap, "bad", g.held) if held > cap else (cap - held, "dim", g.open))
        return _bar(spans, total=max(slots, held), width=bar_w, fill=(g.beyond, "dim"))

    def flags(p: Mapping) -> Line:
        out: Line = []
        for on, text, style in (
            (p.get("closed"), "closed", "warn"),
            (p.get("revoked"), f"{p.get('revoked')} revoked", "bad"),
            (p.get("overcommitted"), f"{p.get('overcommitted')} over", "bad"),
            (p.get("preemptible"), "preemptible", "dim"),
            (p.get("observed"), "observed", "dim"),
        ):
            if on:
                out += [("  " if out else "", ""), (text, style)]
        return out

    def waiting(p: Mapping) -> Line:
        n = int(p.get("queued") or 0)
        return [(f"{n} waiting", "warn" if n else "dim")]

    def reserved(p: Mapping) -> Line:
        n = int(p.get("reserved") or 0)
        return [(f"reserved {n}", "dim")] if n else []

    cols = [
        _Column(
            "",
            [
                [(_mark(n, g), f"pool:{n}"), (" ", ""), (_clip(_clean(n), 12, g.cut), "bold")]
                for n, _ in items
            ],
            100,
        ),
        _Column("", [bar(n, p) for n, p in items], 90),
        _Column(
            "",
            [[(f"{p.get('held_weight', 0)}/{p.get('max', 0)}", "bold")] for _, p in items],
            95,
            right=True,
        ),
        _Column("", [reserved(p) for _, p in items], 5),
        _Column("", [[(f"{p.get('running', 0)} running", "")] for _, p in items], 4),
        _Column("", [waiting(p) for _, p in items], 7),
        _Column("", [flags(p) for _, p in items], 8),
    ]
    cols = _narrow(cols, v.inner + 2)
    rows = []
    for i in range(len(items)):
        row = _Out()
        for n, c in enumerate(cols):
            row.extend(c.cell(i) + ([("  ", "")] if n < len(cols) - 1 else []))
        rows.append(row)
    return _Section(
        6,
        [[line] for line in _labelled("POOLS", rows)],
        more=lambda n: [(" " * GUTTER, ""), (f"+{n} more pools", "dim")],
    )


def _sorted_leases(v: _View) -> list[Mapping]:
    leases = list(v.status.get("leases") or [])
    order = v.pool_order()

    def since(x: Mapping) -> float:
        return x.get("acquired_at") or 0.0

    if v.sort == "held":
        return sorted(leases, key=since)
    if v.sort == "weight":
        return sorted(leases, key=lambda x: (-(x.get("weight") or 0), since(x)))
    if v.sort == "user":
        return sorted(leases, key=lambda x: (str(x.get("user") or ""), since(x)))
    return sorted(leases, key=lambda x: (order.get(x.get("pool"), len(order)), since(x)))


def _leases(v: _View) -> _Section:
    g = v.g
    leases = _sorted_leases(v)
    if not leases:
        return _Section(3, [_labelled("LEASES", [_Out().put("none held", "dim")])])
    now = v.now

    def expires(x: Mapping) -> Line:
        if x.get("revoked"):
            return [("revoked", "bad")]
        at = x.get("expires_at")
        if at is None:
            return [("at exit", "dim")]
        left = at - now
        return [("overdue", "warn")] if left < 0 else [(f"in {_duration(left)}", "")]

    def held_for(x: Mapping) -> Line:
        at = x.get("acquired_at")
        return [(_duration(now - at) if at is not None else "-", "")]

    def text(value: object, style: str = "") -> Line:
        return [(_clip(_clean(value), 12, g.cut), style)]

    redundant = all(x.get("pool") == x.get("kind") for x in leases)
    cols = [
        _Column(
            "KIND",
            [
                [(_mark(x.get("kind"), g), f"pool:{x.get('pool')}"), (f" {x.get('kind')}", "")]
                for x in leases
            ],
            9,
        ),
        _Column("USER", [text(x.get("user") or "-", "bold") for x in leases], 8),
        _Column("SESSION", [text(x.get("session") or "-", "dim") for x in leases], 3),
        _Column("POOL", [text(x.get("pool")) for x in leases], 1 if redundant else 5),
        _Column("W", [[(_weight_cell(x), "")] for x in leases], 7, right=True),
        _Column("HELD", [held_for(x) for x in leases], 4, right=True),
        _Column("EXPIRES", [expires(x) for x in leases], 6, right=True),
    ]
    cols = _narrow(cols, v.inner - 16)
    header = _Out()
    for c in cols:
        header.put(c.head() + "  ", "dim")
    header.put(f"LEASE  (sort: {v.sort})", "dim")
    rows = []
    for i, x in enumerate(leases):
        row = _Out()
        for c in cols:
            row.extend([*c.cell(i), ("  ", "")])
        row.put(x.get("lease_id"))
        if x.get("revoked"):
            row.put(f"  revoked: {x.get('revoke_reason') or ''}", "bad")
        elif x.get("giving_back"):
            back = x.get("giving_back")
            row.put(f"  giving back {_slots(back)}: waiting for its jobs to finish", "warn")
        what = _what(x.get("labels"))
        if what:
            row.put(f"  {what}", "dim")
        rows.append(row)
    return _Section(
        3,
        [[line] for line in _labelled("LEASES", [header, *rows])],
        head=1,
        more=lambda n: [(" " * GUTTER, ""), (f"+{n} more held", "dim")],
        cap=8,
    )


def _queue(v: _View) -> _Section:
    g = v.g
    queue = list(v.status.get("queue") or [])
    if not queue:
        return _Section(2, [_labelled("QUEUE", [_Out().put("nobody waiting", "dim")])])
    places = [f"#{x.get('position') or '?'} {_clean(x.get('pool'))}" for x in queue]
    place_w = max(cells(p) for p in places)
    user_w = min(12, max(cells(_clean(x.get("user") or "-")) for x in queue))
    groups = []
    for i, (x, place) in enumerate(zip(queue, places, strict=True)):
        parts: list[tuple[int, Line]] = [
            (0, [(_pad(place, place_w), "bold"), ("  ", "")]),
            (
                1,
                [
                    (_mark(x.get("kind"), g), f"pool:{x.get('pool')}"),
                    (" ", ""),
                    (_pad(_clean(x.get("user") or "-"), user_w, g.cut), "bold"),
                ],
            ),
        ]
        if x.get("session"):
            parts.append((4, [(f"  {_clean(x['session'])}", "dim")]))
        what = _what(x.get("labels"))
        if what:
            parts.append((3, [(f"  {_clean(what)}", "")]))
        parts.append((5, [(f"  {_clean(x.get('lease_id'))}", "dim")]))
        right = _Out().put(f"needs {_needs(x)}", "")
        if x.get("memory_counted_bytes"):
            right.put(f", {_gib(x['memory_counted_bytes'])}", "")
        if x.get("queued_at") is not None:
            right.put(f"  waiting {_duration(v.now - x['queued_at'])}", "bold")
        gutter = _pad("QUEUE", GUTTER) if i == 0 else " " * GUTTER
        room = max(12, v.inner - right.cells - 2)
        first: Line = [(gutter, "title"), *_spread(_choose(parts, room), right.segs, v.inner)]
        reason = x.get("blocked")
        why: Line = [(" " * (GUTTER + 2), "")]
        if reason:
            why.append((f"{g.blocked} {_clean(reason)}", "wait"))
        else:
            why.append((f"{g.blocked} waiting for its turn", "dim"))
        groups.append([first, why])
    return _Section(
        2,
        groups,
        more=lambda n: [(" " * GUTTER, ""), (f"+{n} more waiting", "dim")],
        cap=7,
    )


def _lending(v: _View) -> _Section:
    lend = v.status.get("lend") or {}
    g = v.g
    if not lend:
        return _Section(5, [_labelled("LENDING", [_Out().put("unknown", "dim")])])
    on = lend.get("switch", True)
    if not on:
        state = ("off", "warn")
    elif lend.get("eligible"):
        state = ("open", "ok")
    else:
        state = ("closed", "bold")
    parts: list[tuple[int, Line]] = [(0, [state])]
    if lend.get("reason"):
        parts.append((1, [(f"  {_clean(lend['reason'])}", "")]))
    sep = (f"  {g.sep}  ", "dim")
    quiet_s = lend.get("quiet_s")
    if on and not lend.get("eligible") and quiet_s and lend.get("quiet_since") is None:
        parts.append((3, [sep, (f"needs {_span(quiet_s)} quiet", "dim")]))
    if lend.get("window"):
        parts.append((2, [sep, (f"window {_clean(lend['window'])}", "dim")]))
    if lend.get("pools"):
        parts.append((4, [sep, ("into " + ", ".join(_clean(p) for p in lend["pools"]), "dim")]))
    return _Section(5, [_labelled("LENDING", [_row(parts, v.inner)])], cap=1)


def _who(e: Mapping) -> str:
    return e.get("user") or "-"


def _size(e: Mapping) -> str:
    return _slots(int(e.get("weight") or 0))


def _queued(e: Mapping) -> str:
    place = f", #{e['position']}" if e.get("position") else ""
    return f"{_who(e)} queued for {_needs(e)} in {e.get('pool')}{place}"


def _granted(e: Mapping) -> str:
    after = " after waiting" if e.get("waited") else ""
    return f"{_who(e)} took {_size(e)} in {e.get('pool')}{after}"


def _resized(e: Mapping) -> str:
    before = int(e.get("previous_weight") or 0)
    after = int(e.get("weight") or 0)
    how = "took" if after > before else "gave back"
    return f"{_who(e)} {how} {_slots(abs(after - before))} in {e.get('pool')}, now {after}"


def _agent(e: Mapping) -> str:
    """A session event's agent and how it joined: `claude 3120, hook`."""
    agent = " ".join(str(x) for x in (e.get("agent"), e.get("agent_pid")) if x is not None)
    return ", ".join(x for x in (agent, str(e.get("source") or "")) if x)


def _pool_max(e: Mapping) -> str:
    how = "set to" if e.get("overridden") else "reset to"
    return f"pool {e.get('pool')} max {how} {e.get('max')}"


class _Shown(NamedTuple):
    """How one event type is shown: a word, in a style, then a sentence."""

    word: str
    style: str
    text: Callable[[Mapping], str]


#: How each event type is shown.
_EVENTS: dict[EventType, _Shown] = {
    EventType.QUEUED: _Shown("queued", "warn", _queued),
    EventType.GRANTED: _Shown("granted", "ok", _granted),
    EventType.LEFT: _Shown(
        "left", "dim", lambda e: f"{_who(e)} gave up a place in {e.get('pool')}"
    ),
    EventType.RELEASED: _Shown(
        "released",
        "",
        lambda e: f"{_who(e)} released {_size(e)} in {e.get('pool')}",
    ),
    EventType.ENDED: _Shown(
        "ended",
        "",
        lambda e: f"{_who(e)}'s {_size(e)} in {e.get('pool')} ended: {e.get('reason')}",
    ),
    EventType.REVOKED: _Shown(
        "revoked",
        "bad",
        lambda e: f"revoked {_who(e)}'s {_size(e)} in {e.get('pool')}: {e.get('reason')}",
    ),
    EventType.LEND_OPENED: _Shown("lend on", "ok", lambda e: f"lending opened: {e.get('reason')}"),
    EventType.LEND_CLOSED: _Shown(
        "lend off", "warn", lambda e: f"lending closed: {e.get('reason')}"
    ),
    EventType.POOL_MAX: _Shown("pool max", "bold", _pool_max),
    EventType.RESIZED: _Shown("resized", "", _resized),
    EventType.PARKED: _Shown(
        "parked", "bad", lambda e: f"{_who(e)}'s command paused, its memory moved to swap"
    ),
    EventType.RESUMED: _Shown("resumed", "ok", lambda e: f"{_who(e)}'s command runs again"),
    EventType.SESSION_STARTED: _Shown(
        "session", "", lambda e: f"session {e.get('session')} ({_agent(e)}) is watched"
    ),
    EventType.SESSION_ENDED: _Shown(
        "session",
        "dim",
        lambda e: f"session {e.get('session')} ({_agent(e)}) ended: {e.get('reason')}",
    ),
    EventType.LEND_SWITCH: _Shown(
        "switch",
        "bold",
        lambda e: "an administrator turned lending " + ("on" if e.get("open") else "off"),
    ),
}


def _session_row(s: Mapping, v: _View) -> _Out:
    """One watched session: its id, agent, how it joined, and what its work holds in memory
    besides the agent, or why that cannot be read here."""
    g = v.g
    agent = " ".join(str(x) for x in (s.get("agent"), s.get("agent_pid")) if x is not None)
    parts: list[tuple[int, Line]] = [(0, [(_clean(s.get("session") or "?"), "bold")])]
    if agent:
        parts.append((2, [(f"  {_clean(agent)}", "")]))
    parts.append((3, [(f"  {s.get('source')}", "dim")]))
    sep = (f"  {g.sep}  ", "dim")
    if s.get("how") == "none" or s.get("footprint_bytes") is None:
        why = s.get("reason") or "not read yet"
        parts.append((1, [sep, (f"memory not known: {_clean(why)}", "warn")]))
        return _row(parts, v.inner)
    trees = int(s.get("trees") or 0)
    held = f"{size(int(s['footprint_bytes']))} in {trees} tree{'' if trees == 1 else 's'}"
    parts.append((1, [sep, (held, "")]))
    largest = s.get("largest")
    if largest:
        text = f"largest {largest.get('name')} {size(int(largest.get('bytes') or 0))}"
        parts.append((4, [sep, (_clean(text), "dim")]))
    return _row(parts, v.inner)


def _sessions(v: _View) -> _Section:
    """The agent sessions the daemon watches, when there are any."""
    watched = (v.status.get("watchdog") or {}).get("sessions") or []
    if not watched:
        return _Section(6, [])
    rows = [_session_row(s, v) for s in watched]
    return _Section(6, [_labelled("SESSIONS", rows)], trim=True, cap=2)


def _event_text(e: Mapping) -> str:
    """The event as a sentence; for a type not in `_EVENTS`, its `reason`."""
    shown = _EVENTS.get(str(e.get("type")))
    return shown.text(e) if shown else str(e.get("reason") or "")


def _events(v: _View) -> _Section:
    if not v.events:
        return _Section(7, [_labelled("EVENTS", [_Out().put("none yet", "dim")])], trim=True)
    verb_w = max(len(shown.word) for shown in _EVENTS.values())
    rows = []
    for e in reversed(v.events):
        ago = v.now - (e.get("time") or v.now)
        when = "now" if ago < 1 else f"{_duration(ago)} ago"
        shown = _EVENTS.get(str(e.get("type")))
        word, style = (shown.word, shown.style) if shown else (str(e.get("type"))[:verb_w], "bold")
        if e.get("type") == EventType.ENDED and e.get("reason") == "expired":
            style = "warn"
        main: Line = [(_rpad(when, 11), "dim"), ("  ", ""), (_pad(word, verb_w), style)]
        parts: list[tuple[int, Line]] = [(0, [*main, ("  ", ""), (_clean(_event_text(e)), "")])]
        if e.get("lease_id"):
            parts.append((1, [(f"  {_clean(e['lease_id'])}", "dim")]))
        rows.append(_row(parts, v.inner))
    return _Section(7, [_labelled("EVENTS", rows)], trim=True, cap=3)


#: The sections top to bottom.
_SECTIONS = (
    _header,
    _pressure,
    _slots_section,
    _pools,
    _leases,
    _queue,
    _sessions,
    _lending,
    _events,
)


def _allocate(sections: Sequence[_Section], height: int) -> tuple[dict[int, int], int]:
    """How many lines each shown section gets, by index, and how many blank lines go between
    sections (the first ones, top down). Most important first, each up to its cap; then the
    gaps; then each grows to its whole length while there is room."""
    left = height
    sizes: dict[int, int] = {}
    by_priority = sorted(range(len(sections)), key=lambda i: sections[i].priority)
    for i in by_priority:
        s = sections[i]
        got = len(s.fit(min(left, max(s.cap, s.smallest()))))
        if got:
            sizes[i] = got
            left -= got
    gaps = min(left, max(0, len(sizes) - 1))
    left -= gaps
    for i in by_priority:
        if i in sizes and left > 0:
            got = len(sections[i].fit(sizes[i] + left))
            left -= got - sizes[i]
            sizes[i] = got
    return sizes, gaps


def render(
    status: Mapping,
    events: Sequence[Mapping],
    width: int,
    height: int,
    *,
    history: Mapping[str, Sequence[float]] | None = None,
    now: float | None = None,
    sort: str = "pool",
    unicode: bool = True,
) -> list[Line]:
    """The screen for `status` and `events` (oldest first), as exactly `height` lines of
    exactly `width` cells. `history` holds recent PSI `some avg10` samples per resource (`cpu`,
    `memory`, `io`), oldest first, for the sparklines. Ages and countdowns are measured from
    `now`, by default `status.time`. `sort` is one of `SORTS`. Without `unicode`, only ASCII is
    drawn, client text included."""
    if width <= 0 or height <= 0:
        return []
    view = _View(
        status=status,
        events=events,
        width=width,
        now=float(status.get("time") or 0.0) if now is None else now,
        history=history or {},
        sort=sort if sort in SORTS else "pool",
        g=UNICODE if unicode else ASCII,
    )
    sections = [make(view) for make in _SECTIONS]
    sizes, gaps = _allocate(sections, height)
    lines: list[Line] = []
    shown = [i for i in range(len(sections)) if i in sizes]
    for n, i in enumerate(shown):
        lines += sections[i].fit(sizes[i])
        if n < gaps:
            lines.append([])
    lines += [[] for _ in range(height - len(lines))]
    if not unicode:
        lines = [[(_ascii(t), style) for t, style in line] for line in lines]
    return [fit(line, width) for line in lines[:height]]


# --- the live view -------------------------------------------------------------------------------


def footer(
    width: int,
    *,
    sort: str,
    paused: bool,
    error: str | None = None,
    age: float | None = None,
) -> Line:
    """The live view's last line: the keys on the left, the connection on the right. `age` is
    how long ago the daemon last answered; `error`, why the last poll failed. A failure takes
    the line before the keys do."""
    keys = _Out()
    for key, word in (("q", "quit"), ("p", "resume" if paused else "pause")):
        keys.put(key, "key").put(f" {word}  ", "dim")
    keys.put("s", "key").put(f" sort: {sort}  ", "dim").put("?", "key").put(" help", "dim")
    if error:
        since = f", last answer {_duration(age)} ago" if age is not None else ""
        parts: list[tuple[int, Line]] = [
            (1, [*keys.segs, ("  ", "")]),
            (0, [(f"no daemon answers{since}; retrying", "bad")]),
            (2, [(f": {_clean(error)}", "dim")]),
        ]
        return fit(_choose(parts, width), width)
    right = _Out()
    if paused:
        right.put("PAUSED", "warn")
    elif age is not None:
        right.put(f"updated {_duration(age)} ago" if age >= 2 else "live", "dim")
    return fit(_spread(keys.segs, right.segs, width), width)


#: What `?` shows.
HELP = (
    ("q", "quit"),
    ("p", "pause or resume: the view stops updating"),
    ("s", "sort the leases: " + ", ".join(SORTS)),
    ("?", "show or hide this help"),
)


def help_lines(unicode: bool = True) -> list[Line]:
    """The help overlay's lines, all one width."""
    g = UNICODE if unicode else ASCII
    rows: list[Line] = [[("leasehold top", "title")], []]
    rows += [[(f"  {key}  ", "key"), (text, "")] for key, text in HELP]
    rows += [
        [],
        [("Marks: ", "dim")] + [(f"{g.marks[k]} {k}  ", "") for k in KINDS],
        [
            ("Slots: ", "dim"),
            (f"{g.free} free  {g.reserved} reserved, free  {g.waiting} waiting", ""),
        ],
    ]
    w = max(_line_cells(r) for r in rows) + 4
    return [fit([("  ", ""), *r], w) for r in rows]


def waiting_screen(width: int, height: int, error: str) -> list[Line]:
    """The live view before any daemon has answered."""
    body: list[Line] = [
        [("leasehold top", "title")],
        [],
        [("no daemon answers: ", "bad"), (_clean(error), "")],
        [("retrying; start one with `leasehold serve`, or see `leasehold doctor`", "dim")],
    ]
    top = max(0, (height - len(body)) // 2)
    lines: list[Line] = [[] for _ in range(top)]
    for line in body:
        pad = max(0, (width - _line_cells(line)) // 2)
        lines.append([(" " * pad, ""), *line])
    lines += [[] for _ in range(height - len(lines))]
    return [fit(line, width) for line in lines[:height]]
