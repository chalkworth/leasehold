"""Weights as people write them: a count of slots, `N`, or a range, `MIN-MAX`, the least a
piece of work needs and the most it can use. A range is fixed at admission: the lease is
granted some size from MIN to MAX and keeps it (`leasehold.admission.grants` chooses it).

`LEASEHOLD_WANT` is how a caller asks for its own size. Set in the environment of a command a
PATH shim or the Claude Code hook leases, it replaces the weight of whichever rule the command
matches. Both ends are cut to the most one lease under that rule could ever be granted, so a
size too large for the machine is cut to fit and never refused. `leasehold hold` removes it
from its command's environment: it is what that lease asked for, and the commands that command
runs ask for their own size.

Standard library only: the shims and hooks import it on every command.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping

#: The variable a caller sets to ask for its own size: `N` or `MIN-MAX`.
ENV_WANT = "LEASEHOLD_WANT"

_FORM = re.compile(r"^\s*([0-9]+)\s*(?:-\s*([0-9]+)\s*)?$")


def parse(text: str) -> tuple[int, int]:
    """`N` as (N, N), `MIN-MAX` as (MIN, MAX); a ValueError unless 1 <= MIN <= MAX."""
    match = _FORM.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not a weight: N or MIN-MAX, e.g. 4 or 2-4")
    least = int(match.group(1))
    most = least if match.group(2) is None else int(match.group(2))
    if least < 1:
        raise ValueError(f"{text!r}: a weight is at least 1")
    if most < least:
        raise ValueError(f"{text!r}: the most, {most}, is below the least, {least}")
    return least, most


def text(least: int, most: int | None = None) -> str:
    """`N` for a single weight (`most` None or equal), else `MIN-MAX`."""
    return str(least) if most is None or most == least else f"{least}-{most}"


def asked(
    env: Mapping[str, str],
    least: int,
    most: int | None,
    say: Callable[[str], None],
    ceiling: int | None = None,
) -> tuple[int, int]:
    """The least and the most weight a shim or hook asks for: what `LEASEHOLD_WANT` in `env`
    says, else `least` and `most` (None: `least`), its rule's. A `LEASEHOLD_WANT` above
    `ceiling` (the most the rule's lease could ever be granted; None: unknown) is cut to it, and
    said. One that is set but not a weight is said (`say`) and ignored, never fatal to the
    command."""
    rule = (least, least if most is None else most)
    value = env.get(ENV_WANT)
    if not value:
        return rule
    try:
        wanted = parse(value)
    except ValueError as exc:
        say(f"{ENV_WANT} ignored: {exc}; asking for the rule's {text(*rule)}")
        return rule
    if ceiling is None or ceiling < 1 or wanted[1] <= ceiling:
        return wanted
    cut = (min(wanted[0], ceiling), ceiling)
    say(f"{ENV_WANT}={value.strip()} cut to {text(*cut)}, the most one lease could be granted")
    return cut
