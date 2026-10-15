"""`setup`: a user's own leasehold in one step, and `--remove` to undo it (`leasehold.setup`)."""

from __future__ import annotations

from pathlib import Path


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
    s.add_argument(
        "--upgrade-config",
        action="store_true",
        help="only bring the config (--config, else yours) from leasehold 0.2's defaults to "
        "today's: memory counted, and the memory guard counting memory instead of capping "
        "pools. Shows the change and keeps the old file beside it",
    )
    s.add_argument(
        "--dry-run",
        action="store_true",
        help="with --upgrade-config: show the change, write nothing",
    )
    s.set_defaults(run=_setup)


def _setup(args, ctx) -> int:
    from leasehold import setup

    def say(line: str) -> None:
        print(f"  {line}")

    if args.dry_run and not args.upgrade_config:
        raise SystemExit("leasehold setup: --dry-run goes with --upgrade-config")
    if args.remove:
        return setup.remove(ctx.env, say)
    if args.upgrade_config:
        path = Path(ctx.config).expanduser() if ctx.config else None
        return setup.upgrade_config(path, ctx.env, say, dry_run=args.dry_run)
    return setup.setup(ctx.env, say)
