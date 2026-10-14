"""`report`: push the machine's status to a URL."""

from __future__ import annotations

import sys
from pathlib import Path


def register(sub) -> None:
    s = sub.add_parser(
        "report",
        help="POST this machine's status to a URL, repeatedly",
        description="POST this machine's status to a URL, repeatedly. "
        "Experimental: may change before 1.0.",
    )
    s.add_argument("--url", required=True, help="where to POST (https, or http on loopback)")
    s.add_argument("--token-file", help="a file holding a bearer token to send")
    s.add_argument(
        "--header", action="append", default=[], metavar="'NAME: VALUE'", help="repeatable"
    )
    s.add_argument(
        "--interval", type=float, default=30, help="seconds between reports (default 30)"
    )
    s.add_argument("--once", action="store_true", help="report once and exit")
    s.set_defaults(run=_report)


def _report(args, ctx) -> int:
    from leasehold.report import ReportError, parse_headers, run

    try:
        return run(
            ctx.client(),
            url=args.url,
            token_file=Path(args.token_file) if args.token_file else None,
            headers=parse_headers(args.header),
            interval=args.interval,
            once=args.once,
        )
    except ReportError as exc:
        sys.stderr.write(f"leasehold: {exc.message}\n")
        return exc.code
