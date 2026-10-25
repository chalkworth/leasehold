"""`run`: an agent (or any command) as one session of its own (`leasehold.agentrun`)."""

from __future__ import annotations

import argparse


def register(sub) -> None:
    s = sub.add_parser(
        "run",
        help="run an agent as one session: its heavy commands leased and contained",
        description="Run an agent (or any command) as one leasehold session: its heavy "
        "commands go through the PATH shims under leases, in the session's own systemd slice "
        "where commands can be contained, and what it leaves running is stopped when it ends.",
    )
    s.add_argument("--session", help="the session's id (default: the program's name and a tag)")
    s.add_argument(
        "--leave-running",
        action="store_true",
        help="leave what the agent started running when it exits (a dev server, say)",
    )
    s.add_argument("--quiet", action="store_true", help="say nothing before the agent starts")
    s.add_argument(
        "--no-contain",
        action="store_true",
        help='run the agent uncontained (also: [contain] mode = "off" in the config)',
    )
    s.add_argument(
        "--tool-timeout",
        type=float,
        metavar="S",
        help="the agent's own timeout on a shell command, in seconds: a shim waits in line at "
        "most half of it, then says why the command did not run (0: none; default: what "
        "leasehold knows of the agent, 120 for Claude Code)",
    )
    s.add_argument("argv", nargs=argparse.REMAINDER, metavar="-- agent ...")
    s.set_defaults(run=_run)


def _run(args, ctx) -> int:
    from leasehold import agentrun

    cmd = list(args.argv)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    return agentrun.run(
        cmd,
        env=ctx.env,
        session=args.session,
        leave_running=args.leave_running,
        contain_mode="off" if args.no_contain else _contain_mode(ctx),
        quiet=args.quiet,
        tool_timeout_s=args.tool_timeout,
    )


def _contain_mode(ctx) -> str:
    """The config's `[contain] mode` (the one a daemon of the caller's would read), `auto`
    when there is no config or it cannot be read: `serve` and `doctor` report a bad one."""
    from leasehold import machine, paths

    try:
        found = paths.find_config(ctx.config, ctx.env)
        return machine.load(found).contain.mode if found else "auto"
    except (OSError, ValueError):
        return "auto"
