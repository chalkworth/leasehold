"""`setup`: a user's own leasehold in one step, and `--remove` to undo it (`leasehold.setup`)."""

from __future__ import annotations


def register(sub) -> None:
    s = sub.add_parser(
        "setup",
        help="set up your own leasehold: config, daemon, shims; --remove undoes it",
        description="Write a config sized for this machine (unless there is one), install and "
        "start a systemd user unit running your daemon, install the PATH shims, and say how to "
        "run your coding agents under leasehold. Every step is safe to repeat.",
    )
    s.add_argument(
        "--remove",
        action="store_true",
        help="stop and delete the unit and the shims (the config and ledger stay)",
    )
    s.set_defaults(run=_setup)


def _setup(args, ctx) -> int:
    from leasehold import setup

    def say(line: str) -> None:
        print(f"  {line}")

    if args.remove:
        return setup.remove(ctx.env, say)
    return setup.setup(ctx.env, say)
