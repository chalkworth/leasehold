"""`session`: register an agent session with the daemon, or unregister one, from any harness
(`leasehold.joining`)."""

from __future__ import annotations

import os
import sys

from leasehold.client import ApiError, Unavailable
from leasehold.exits import EXIT_REFUSED


def register(sub) -> None:
    s = sub.add_parser(
        "session",
        help="have the daemon watch an agent session, or stop watching one",
        description="Register an agent session with the daemon, so that it watches everything "
        "the agent starts and reports the session's memory in `status` and `top`; or "
        "unregister one. For harnesses that do not use `leasehold run`.",
    )
    verbs = s.add_subparsers(dest="verb", required=True, metavar="register|unregister")
    r = verbs.add_parser(
        "register",
        help="watch a session; prints its id, for LEASEHOLD_SESSION",
        description="Watch a session whose agent is process PID, until it is unregistered or "
        "that process ends. Prints the session's id: export it as LEASEHOLD_SESSION to the "
        "agent's commands, so that what they leave running is counted too.",
    )
    r.add_argument("--session", help="the session's id (default: the agent's name and a tag)")
    r.add_argument(
        "--pid",
        type=int,
        help="the agent's process (default: the process that runs this command)",
    )
    r.add_argument("--agent", help="what to call the agent (default: its program's name)")
    r.set_defaults(run=_register)
    u = verbs.add_parser("unregister", help="stop watching a session")
    u.add_argument("session_id", metavar="SESSION")
    u.set_defaults(run=_unregister)


def _register(args, ctx) -> int:
    from leasehold import joining

    pid = args.pid if args.pid is not None else os.getppid()
    sid = args.session or joining.new_id(args.agent or "session")
    try:
        ctx.client().register_session(sid, pid=pid, agent=args.agent, source="api")
    except Unavailable as exc:
        return ctx.unreachable(exc)
    except ApiError as exc:
        sys.stderr.write(f"leasehold: {exc.payload.get('detail')}\n")
        return EXIT_REFUSED
    print(sid)
    return 0


def _unregister(args, ctx) -> int:
    try:
        ctx.client().unregister_session(args.session_id)
    except Unavailable as exc:
        return ctx.unreachable(exc)
    except ApiError as exc:
        sys.stderr.write(f"leasehold: {exc.payload.get('error')}: {exc.payload.get('detail')}\n")
        return EXIT_REFUSED
    return 0
