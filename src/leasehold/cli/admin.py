"""`lend` and `pool`: what an administrator may change while the daemon runs."""

from __future__ import annotations

import sys

from leasehold.client import ApiError, Unavailable


def register(sub) -> None:
    s = sub.add_parser(
        "lend",
        help="switch lending on or off (administrators)",
        description="Switch lending on or off (administrators). "
        "Experimental: may change before 1.0.",
    )
    s.add_argument("state", choices=("on", "off"))
    s.set_defaults(run=_lend)

    s = sub.add_parser("pool", help="change a pool's max until restart (administrators)")
    actions = s.add_subparsers(dest="pool_command", required=True, metavar="ACTION")
    s = actions.add_parser("set", help="set a pool's max, or --reset it to the configured one")
    s.add_argument("name")
    group = s.add_mutually_exclusive_group(required=True)
    group.add_argument("--max", type=int, help="the pool's new max")
    group.add_argument("--reset", action="store_true", help="back to the configured max")
    s.set_defaults(run=_pool)


def _lend(args, ctx) -> int:
    def ask(client):
        answer = client.lend(args.state == "on")
        return f"lending {'on' if answer['switch'] else 'off'}: {answer['reason']}"

    return _admin(ctx, ask)


def _pool(args, ctx) -> int:
    def ask(client):
        answer = client.set_pool_max(args.name, None if args.reset else args.max)
        state = "overridden" if answer["overridden"] else "configured"
        return f"pool {answer['pool']}: max {answer['max']} ({state})"

    return _admin(ctx, ask)


def _admin(ctx, ask) -> int:
    try:
        print(ask(ctx.client()))
    except ApiError as exc:
        sys.stderr.write(f"leasehold: {exc.payload.get('detail')}\n")
        return 1
    except Unavailable as exc:
        return ctx.unreachable(exc)
    return 0
