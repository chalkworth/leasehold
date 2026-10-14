"""Text in terminal cells: how wide text is, cutting and padding it to a width, building a
line of styled segments, and choosing what fits on one. Text put on a line is shown with control
characters replaced, so nothing a client sends can drive the terminal."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

#: One run of text in one style.
Segment = tuple[str, str]
#: One screen line.
Line = list[Segment]


# --- text ----------------------------------------------------------------------------------------


def _clean(text: object) -> str:
    """`text` with control and format characters as `?` and other separators as spaces."""
    s = str(text)
    if s.isprintable():
        return s
    return "".join(
        ch if ch.isprintable() else ("?" if unicodedata.category(ch)[0] == "C" else " ") for ch in s
    )


def _char_cells(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cells(text: str) -> int:
    """How many terminal cells `text` takes."""
    return len(text) if text.isascii() else sum(_char_cells(ch) for ch in text)


def _clip(text: str, n: int, cut: str = "") -> str:
    """`text` cut to at most `n` cells, ending in `cut` when anything was cut off."""
    if cells(text) <= n:
        return text
    room = n - cells(cut)
    if room < 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = _char_cells(ch)
        if used + w > room:
            break
        out.append(ch)
        used += w
    return "".join(out) + cut


def _pad(text: str, n: int, cut: str = "") -> str:
    text = _clip(text, n, cut)
    return text + " " * (n - cells(text))


def _rpad(text: str, n: int) -> str:
    text = _clip(text, n)
    return " " * (n - cells(text)) + text


class _Out:
    """One line being built."""

    def __init__(self) -> None:
        self.segs: Line = []
        self.cells = 0

    def put(self, text: object, style: str = "") -> _Out:
        text = _clean(text)
        if text:
            self.segs.append((text, style))
            self.cells += cells(text)
        return self

    def extend(self, line: Line) -> _Out:
        for text, style in line:
            self.put(text, style)
        return self

    def to(self, col: int) -> _Out:
        return self.put(" " * (col - self.cells)) if col > self.cells else self


def _line_cells(line: Line) -> int:
    return sum(cells(t) for t, _ in line)


def fit(line: Line, width: int) -> Line:
    """`line` cut or padded to exactly `width` cells, runs of one style merged."""
    out: Line = []
    used = 0
    for text, style in line:
        if used >= width:
            break
        text = _clip(text, width - used)
        if not text:
            continue
        used += cells(text)
        if out and out[-1][1] == style:
            out[-1] = (out[-1][0] + text, style)
        else:
            out.append((text, style))
    if used < width:
        if out and out[-1][1] == "":
            out[-1] = (out[-1][0] + " " * (width - used), "")
        else:
            out.append((" " * (width - used), ""))
    return out


def _spread(left: Line, right: Line, width: int) -> Line:
    """`left` at the start and `right` against the end; `left` is cut first, and `right` is
    dropped when it would leave `left` fewer than 12 cells."""
    rw = _line_cells(right)
    room = width - rw - 1
    if room < 12:
        return left
    lw = _line_cells(left)
    if lw > room:
        return [*fit(left, room), (" ", ""), *right]
    return [*left, (" " * (width - lw - rw), ""), *right]


def _choose(parts: Sequence[tuple[int, Line]], width: int) -> Line:
    """The parts, in their order, that fit in `width`, kept most important (lowest rank) first;
    the most important is cut to fit when it alone does not."""
    kept: set[int] = set()
    used = 0
    for i in sorted(range(len(parts)), key=lambda i: parts[i][0]):
        n = _line_cells(parts[i][1])
        if used + n <= width or not kept:
            kept.add(i)
            used += n
    out = [seg for i, (_, line) in enumerate(parts) if i in kept for seg in line]
    return fit(out, width) if used > width else out


def plain(lines: Sequence[Line]) -> str:
    """The lines as text: styles dropped, trailing spaces and trailing blank lines removed."""
    text = ["".join(t for t, _ in line).rstrip() for line in lines]
    while text and not text[-1]:
        text.pop()
    return "\n".join(text)


def _ascii(text: str) -> str:
    """`text` with each character outside ASCII as one `?` per cell it took."""
    if text.isascii():
        return text
    return "".join(ch if ch.isascii() else "?" * _char_cells(ch) for ch in text)
