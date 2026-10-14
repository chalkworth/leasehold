"""`top`: the machine live in the terminal (`leasehold.top`)."""

from __future__ import annotations

import argparse

from leasehold import top
from leasehold.client import ApiError, Unavailable

#: How long the live view waits for one answer before it says the daemon is not answering.
POLL_TIMEOUT_S = 3.0


def register(sub) -> None:
    s = sub.add_parser(
        "top", help="the machine live: slots, pressure, pools, leases, the queue and events"
    )
    s.add_argument(
        "--interval",
        type=_interval,
        default=1.0,
        metavar="S",
        help="seconds between updates (default 1)",
    )
    s.add_argument(
        "--once",
        action="store_true",
        help="print one plain view and exit (also what happens off a terminal)",
    )
    s.set_defaults(run=_top)


def _interval(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not 0.1 <= value <= 3600:
        raise argparse.ArgumentTypeError("the interval is from 0.1 to 3600 seconds")
    return value


def _top(args, ctx) -> int:
    client = ctx.client()
    client.timeout = min(client.timeout, POLL_TIMEOUT_S)
    try:
        return top.run(client, interval=args.interval, once=args.once)
    except (Unavailable, ApiError) as exc:
        return ctx.unreachable(exc)
