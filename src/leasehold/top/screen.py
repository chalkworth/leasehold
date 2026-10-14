"""The terminal: `run`, and the live view it draws with curses and colour.

`run` polls the daemon through a `Client` (`Watch`) and redraws. Off a terminal, on one smaller
than `MIN_SIZE`, or with `once`, it prints one plain render instead.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TextIO

from leasehold.client import Client
from leasehold.top.cells import _ascii, _line_cells, cells, fit, plain
from leasehold.top.render import SORTS, footer, help_lines, render, waiting_screen
from leasehold.top.watch import KEEP_EVENTS, Watch

#: The smallest terminal `run` draws its live view on.
MIN_SIZE = (80, 20)
#: The size of a plain render off a terminal.
PLAIN_SIZE = (100, 40)


def use_color(env: Mapping[str, str], has_colors: bool) -> bool:
    """Whether to draw in colour: the terminal has it and `NO_COLOR` is unset or empty."""
    return has_colors and not env.get("NO_COLOR")


def _unicode(stream: TextIO) -> bool:
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    return encoding.startswith("utf")


def _size(stdout: TextIO) -> tuple[int, int] | None:
    """The terminal's size, or None when `stdout` is not one."""
    try:
        if not stdout.isatty():
            return None
        size = os.get_terminal_size(stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return None
    return size.columns, size.lines


def run(
    client: Client, *, interval: float = 1.0, once: bool = False, stdout: TextIO | None = None
) -> int:
    """Show the machine until `q`; 0. With `once`, off a terminal, or on one smaller than
    `MIN_SIZE`, print one plain render instead (sized to the terminal, else `PLAIN_SIZE`); that
    raises `Unavailable` or `ApiError` when the daemon does not answer. The live view keeps
    retrying instead, and says so."""
    stdout = sys.stdout if stdout is None else stdout
    size = _size(stdout)
    if once or size is None or size[0] < MIN_SIZE[0] or size[1] < MIN_SIZE[1]:
        status = client.status()
        events = (client.events(0).get("events") or [])[-KEEP_EVENTS:]
        width, height = size or PLAIN_SIZE
        stdout.write(plain(render(status, events, width, height, unicode=_unicode(stdout))))
        stdout.write("\n")
        stdout.flush()
        return 0
    import curses

    try:
        return curses.wrapper(_Screen(Watch(client), interval, _unicode(stdout)).loop)
    except KeyboardInterrupt:
        return 0


# --- curses --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Look:
    """How a style is drawn: curses colour numbers (-1: the terminal's own) and attributes."""

    fg: int = -1
    bg: int = -1
    attr: int = 0


#: Pool colours (256-colour, 8-colour), by pool name; other pools take `_OTHER` in turn.
_POOL_COLOURS = {"work": (32, 4), "external": (214, 3), "lend": (36, 2), "observed": (247, 7)}
_OTHER = ((74, 6), (175, 5), (221, 3))


def pool_colour(name: str, order: Sequence[str], colors: int) -> int:
    """The colour number for pool `name`; `order` is the pools in status order."""
    rich = colors >= 256
    if name in _POOL_COLOURS:
        return _POOL_COLOURS[name][0 if rich else 1]
    others = [p for p in order if p not in _POOL_COLOURS]
    i = others.index(name) if name in others else 0
    return _OTHER[i % len(_OTHER)][0 if rich else 1]


def look(style: str, *, color: bool, colors: int = 8, pools: Sequence[str] = ()) -> Look:
    """How to draw `style`. Without `color`, only attributes: held slots in reverse video,
    warnings and why a lease waits in bold, so nothing depends on colour."""
    import curses

    rich = colors >= 256
    base, _, pool = style.partition(":")
    if not color:
        attrs = {
            "title": curses.A_BOLD,
            "bold": curses.A_BOLD,
            "key": curses.A_BOLD,
            "dim": curses.A_DIM,
            "warn": curses.A_BOLD,
            "bad": curses.A_BOLD,
            "wait": curses.A_BOLD,
            "bay": curses.A_REVERSE,
            "revoked": curses.A_REVERSE | curses.A_UNDERLINE,
            "reserved": curses.A_DIM,
        }
        return Look(attr=attrs.get(base, 0))
    status_colours = {
        "ok": (78 if rich else 2, 0),
        "warn": (214 if rich else 3, curses.A_BOLD),
        "bad": (203 if rich else 1, curses.A_BOLD),
        "wait": (222 if rich else 3, curses.A_BOLD),
    }
    if base in status_colours:
        fg, attr = status_colours[base]
        return Look(fg=fg, attr=attr)
    if base == "pool":
        return Look(fg=pool_colour(pool, pools, colors))
    if base == "reserved":
        return Look(fg=pool_colour(pool, pools, colors), attr=curses.A_DIM)
    if base == "bay":
        bg = pool_colour(pool, pools, colors)
        fg = 231 if rich and pool == "work" else (7 if pool == "work" else 0)
        return Look(fg=fg, bg=bg)
    if base == "revoked":
        return Look(fg=231 if rich else 7, bg=160 if rich else 1, attr=curses.A_UNDERLINE)
    attrs = {"title": curses.A_BOLD, "bold": curses.A_BOLD, "key": curses.A_BOLD}
    if base == "dim":
        return Look(fg=245 if rich else -1, attr=0 if rich else curses.A_DIM)
    return Look(attr=attrs.get(base, 0))


class _Painter:
    """Styles to curses attributes, allocating colour pairs as they are first needed."""

    def __init__(self, color: bool) -> None:
        import curses

        self.curses = curses
        self.color = color
        self.colors = curses.COLORS if color else 0
        self.pairs: dict[tuple[int, int], int] = {}
        self.cache: dict[tuple[str, tuple[str, ...]], int] = {}

    def attr(self, style: str, pools: Sequence[str], dim: bool = False) -> int:
        key = (style, tuple(pools))
        if key not in self.cache:
            lk = look(style, color=self.color, colors=self.colors, pools=pools)
            attr = lk.attr
            if (lk.fg, lk.bg) != (-1, -1):
                pair = self.pairs.get((lk.fg, lk.bg))
                if pair is None and len(self.pairs) + 1 < self.curses.COLOR_PAIRS:
                    pair = len(self.pairs) + 1
                    try:
                        self.curses.init_pair(pair, lk.fg, lk.bg)
                        self.pairs[(lk.fg, lk.bg)] = pair
                    except self.curses.error:
                        pair = None
                if pair is not None:
                    attr |= self.curses.color_pair(pair)
            self.cache[key] = attr
        return self.cache[key] | (self.curses.A_DIM if dim else 0)


class _Screen:
    def __init__(self, watch: Watch, interval: float, unicode: bool) -> None:
        self.watch = watch
        self.interval = interval
        self.unicode = unicode
        self.sort = SORTS[0]
        self.paused = False
        self.help = False
        self.frozen: float | None = None

    def loop(self, scr) -> int:
        import curses

        with contextlib.suppress(curses.error):
            curses.curs_set(0)
        color = False
        if curses.has_colors():
            curses.start_color()
            with contextlib.suppress(curses.error):
                curses.use_default_colors()
            color = use_color(os.environ, True)
        painter = _Painter(color)
        due = 0.0
        while True:
            if not self.paused and time.monotonic() >= due:
                self.watch.poll()
                due = time.monotonic() + self.interval
            self.draw(scr, painter)
            # Redraw for the next poll, or within a second for the running clocks; paused, the
            # screen changes only on a key.
            wait = min(1.0, max(0.05, due - time.monotonic()))
            scr.timeout(-1 if self.paused else int(wait * 1000))
            key = scr.getch()
            if key in (ord("q"), ord("Q")):
                return 0
            if key == ord("p"):
                self.paused = not self.paused
                self.frozen = self.watch.now() if self.paused and self.watch.status else None
                due = 0.0
            elif key == ord("s"):
                self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
            elif key in (ord("?"), ord("h")):
                self.help = not self.help
            elif key == 27 and self.help:
                self.help = False
            elif key == curses.KEY_RESIZE:
                curses.update_lines_cols()

    def draw(self, scr, painter: _Painter) -> None:
        height, width = scr.getmaxyx()
        scr.erase()
        w = self.watch
        pools = list((w.status or {}).get("pools") or {})
        if w.status is None:
            lines = waiting_screen(width, height, w.error or "connecting")
            stale = False
        else:
            now = self.frozen if self.frozen is not None else w.now()
            lines = render(
                w.status,
                list(w.events),
                width,
                height - 1,
                history=w.history,
                now=now,
                sort=self.sort,
                unicode=self.unicode,
            )
            age = None if self.paused and not w.error else time.monotonic() - w.received
            lines.append(footer(width, sort=self.sort, paused=self.paused, error=w.error, age=age))
            stale = w.error is not None
        for y, line in enumerate(lines[:height]):
            self._put(scr, y, 0, line, painter, pools, dim=stale and y < height - 1)
        if self.help:
            box = help_lines(self.unicode)
            top = max(0, (height - len(box)) // 2)
            left = max(0, (width - _line_cells(box[0])) // 2)
            for y, line in enumerate(box[: max(0, height - top)]):
                room = min(width - left, _line_cells(line))
                segs = [(t, s or "bold") for t, s in fit(line, room)]
                self._put(scr, top + y, left, segs, painter, pools, reverse=True)
        scr.refresh()

    def _put(self, scr, y, x, line, painter, pools, *, dim=False, reverse=False) -> None:
        import curses

        for text, style in line:
            text = text if self.unicode else _ascii(text)
            attr = painter.attr(style, pools, dim) | (curses.A_REVERSE if reverse else 0)
            # Writing the bottom-right cell succeeds but raises: curses cannot move past it.
            with contextlib.suppress(curses.error):
                scr.addstr(y, x, text, attr)
            x += cells(text)
