"""`hold`, `acquire`, `keep` and `release`: taking leases and giving them back."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Sequence

from leasehold import handoff
from leasehold.hold import hold
from leasehold.kinds import GRANTED, KINDS
from leasehold.units import declared_to_bytes

ENV_SESSION = "LEASEHOLD_SESSION"
ENV_IF_UNAVAILABLE = "LEASEHOLD_IF_UNAVAILABLE"
IF_UNAVAILABLE = ("run", "fail")
#: The kinds a client may ask for, in `KINDS` order.
KIND_CHOICES = tuple(k for k in KINDS if k in GRANTED)


_SESSION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # the daemon's id pattern


def session(value: str | None) -> str | None:
    """`value` if the daemon will take it as a session name (None for none, and for an empty
    one: an exported-but-empty $LEASEHOLD_SESSION means no session); exit with a message
    otherwise."""
    if value == "":
        return None
    if value is not None and not _SESSION_ID.match(value):
        raise SystemExit(
            f"leasehold: session {value!r} is not 1-64 of A-Z a-z 0-9 _ . : - "
            f"(from --session or ${ENV_SESSION})"
        )
    return value


def labels(values: Sequence[str]) -> dict[str, str]:
    out = {}
    for value in values:
        key, sep, val = value.partition("=")
        if not sep or not key:
            raise SystemExit(f"leasehold: --label {value!r} is not key=value")
        out[key] = val
    return out


def _lease_options(s: argparse.ArgumentParser) -> None:
    """What `hold` and `acquire` share: what to ask for, and how long to wait."""
    s.add_argument("--kind", choices=KIND_CHOICES, default="work", help="default: work")
    s.add_argument("--pool", help="the pool (default: the kind's)")
    s.add_argument(
        "--weight",
        type=int,
        help="slots to hold (hold: as the [commands] rules weigh the command, else 1; acquire: 1)",
    )
    s.add_argument("--label", action="append", default=[], metavar="K=V", help="repeatable")
    s.add_argument(
        "--session",
        default=os.environ.get(ENV_SESSION),
        help=f"which of your sessions this is for; they take turns (default ${ENV_SESSION})",
    )
    s.add_argument("--timeout", type=float, help="give up waiting after S seconds (exit 75)")
    s.add_argument("--ttl", type=float, help="the lease's TTL (default: the machine's)")
    s.add_argument(
        "--memory",
        type=_memory,
        help="the memory the work will use, e.g. 4g: counted against the machine's memory "
        "budget, and hold holds a contained command to it (MemoryHigh). hold's default: the "
        "matching [commands] rule's memory",
    )


def _keep_limits(s: argparse.ArgumentParser) -> None:
    s.add_argument(
        "--max-hold",
        type=float,
        default=handoff.DEFAULT_MAX_HOLD_S,
        help="stop renewing after S seconds whatever happens (default 6 h)",
    )
    s.add_argument("--watch-pid", type=int, help="stop renewing once this process is gone")


def register(sub) -> None:
    s = sub.add_parser("hold", help="run a command under a lease")
    _lease_options(s)
    s.add_argument("--grace", type=float, default=30.0, help="SIGTERM-to-SIGKILL grace on revoke")
    s.add_argument(
        "--jobserver",
        action="store_true",
        help="hand the command a GNU make jobserver holding its lease's job count",
    )
    s.add_argument(
        "--no-job-env",
        dest="job_env",
        action="store_false",
        help="leave CARGO_BUILD_JOBS, GOMAXPROCS and the other job-count variables alone",
    )
    s.add_argument(
        "--if-unavailable",
        default=os.environ.get(ENV_IF_UNAVAILABLE) or "run",
        metavar="run|fail",
        help="with no daemon: run the command without a lease, with a warning (the default), or "
        f"fail with exit 69 (default ${ENV_IF_UNAVAILABLE})",
    )
    s.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- cmd ...")
    s.set_defaults(run=_hold)

    s = sub.add_parser("acquire", help="wait for a lease another process will release")
    s.add_argument("--lease-id", required=True, help="the lease's id, for `release` to name")
    _lease_options(s)
    s.add_argument("--keep", action="store_true", help="start a detached renewer (`keep`)")
    _keep_limits(s)
    s.set_defaults(run=_acquire)

    s = sub.add_parser("keep", help="renew a lease until it is released or a limit is hit")
    s.add_argument("lease_id")
    s.add_argument("--ttl", type=float, required=True, help="the TTL each renewal sets")
    _keep_limits(s)
    s.set_defaults(run=lambda args, ctx: handoff.keep_main(ctx.client(), args))

    s = sub.add_parser("release", help="give a lease back")
    s.add_argument("lease_id")
    s.set_defaults(run=lambda args, ctx: handoff.release(ctx.client(), args.lease_id))


def _hold(args, ctx) -> int:
    cmd = list(args.argv)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if args.if_unavailable not in IF_UNAVAILABLE:
        raise SystemExit(
            f"leasehold: --if-unavailable {args.if_unavailable!r} is not run or fail "
            f"(from the option or ${ENV_IF_UNAVAILABLE})"
        )
    try:
        client = ctx.client()
    except SystemExit:
        if args.if_unavailable == "fail":
            raise
        client = None  # no daemon to be found: as if none answered
    return hold(
        client,
        cmd,
        kind=args.kind,
        pool=args.pool,
        weight=args.weight,
        labels=labels(args.label),
        session=session(args.session),
        timeout_s=args.timeout,
        ttl_s=args.ttl,
        grace_s=args.grace,
        job_env=args.job_env,
        jobserver=args.jobserver,
        memory=args.memory,
        if_unavailable=args.if_unavailable,
    )


def _memory(value: str) -> str:
    try:
        declared_to_bytes(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def _acquire(args, ctx) -> int:
    args.labels = labels(args.label)
    args.session = session(args.session)
    args.weight = 1 if args.weight is None else args.weight
    client = ctx.client()
    return handoff.acquire_main(client, client.path, args)
