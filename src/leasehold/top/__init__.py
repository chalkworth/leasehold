"""`leasehold top`: the machine in a terminal, redrawn as it changes.

* `cells`: text in terminal cells (`cells`, `fit`, `plain`).
* `render`: the screen as lines, a pure function of a status object, recent events and a size.
* `watch`: polling the daemon (`Watch`).
* `screen`: `run`, and the curses live view with its colours.

`run` keeps the last `KEEP_EVENTS` events and `HISTORY` pressure samples and redraws. Off a
terminal, on one smaller than `MIN_SIZE`, or with `once`, it prints one plain render instead.

Every public name of the modules is importable from `leasehold.top` as well.
"""

from __future__ import annotations

from leasehold.top.cells import Line, Segment, cells, fit, plain
from leasehold.top.render import (
    ASCII,
    GUTTER,
    HELP,
    RESOURCES,
    SORTS,
    UNICODE,
    Glyphs,
    footer,
    help_lines,
    render,
    sparkline,
    waiting_screen,
)
from leasehold.top.screen import MIN_SIZE, PLAIN_SIZE, Look, look, pool_colour, run, use_color
from leasehold.top.watch import HISTORY, KEEP_EVENTS, Watch

__all__ = [
    "ASCII",
    "GUTTER",
    "HELP",
    "HISTORY",
    "KEEP_EVENTS",
    "MIN_SIZE",
    "PLAIN_SIZE",
    "RESOURCES",
    "SORTS",
    "UNICODE",
    "Glyphs",
    "Line",
    "Look",
    "Segment",
    "Watch",
    "cells",
    "fit",
    "footer",
    "help_lines",
    "look",
    "plain",
    "pool_colour",
    "render",
    "run",
    "sparkline",
    "use_color",
    "waiting_screen",
]
