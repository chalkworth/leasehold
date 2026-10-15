"""`commands`, `hook` and `shims`: which commands take a lease, and the coding-agent hooks and
PATH shims that take it."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from leasehold import claude_code, programs, shell, shims
from leasehold.client import ApiError, Unavailable


def register(sub) -> None:
    s = sub.add_parser("commands", help="which commands agents and shims take a lease for")
    s.add_argument("--json", action="store_true")
    s.add_argument("--test", metavar="COMMAND", help="say which rule a shell command matches")
    s.add_argument(
        "--programs", action="store_true", help="the programs the rules name, one per line"
    )
    s.add_argument(
        "--write",
        action="store_true",
        help="with --programs: write them for the Claude Code hook's pre-filter, print the path",
    )
    s.set_defaults(run=_commands)

    s = sub.add_parser("hook", help="a coding agent's hook (reads its event on stdin)")
    s.add_argument("agent", choices=["claude-code"])
    s.add_argument("event", choices=["pre", "post"])
    s.set_defaults(run=_hook)

    s = sub.add_parser("shims", help="PATH shims that run heavy programs under a lease")
    verbs = s.add_subparsers(dest="verb", required=True, metavar="install|list|remove")
    for verb, text in (
        ("install", "write a shim for each program the rules name"),
        ("list", "the shims in the directory"),
        ("remove", "delete the shims (only leasehold's)"),
    ):
        v = verbs.add_parser(verb, help=text)
        v.add_argument("--dir", help="the shim directory (default ~/.local/share/leasehold/shims)")
        if verb != "list":
            v.add_argument("--only", help="these programs only, comma-separated")
        if verb == "install":
            v.add_argument("--force", action="store_true", help="replace files that are not shims")
            v.add_argument("--all", action="store_true", help="also programs not on PATH now")
        v.set_defaults(run=_shims)


def _commands(args, ctx) -> int:
    try:
        answer = ctx.client().commands()
    except (Unavailable, ApiError) as exc:
        return ctx.unreachable(exc)
    settings = shell.served(answer)
    if args.write and not args.programs:
        sys.stderr.write("leasehold: --write goes with --programs\n")
        return 2
    if args.programs:
        return _programs(settings, write=args.write, env=ctx.env)
    if args.test is not None:
        found = shell.match(settings.rules, args.test)
        if args.json:
            print(json.dumps(None if found is None else _describe(found)))
        elif found is None:
            print("no rule: it runs without a lease")
        else:
            print(f"{found.program}: {_rule_line(found.rule)} ({found.rule.source} rule)")
        return 0 if found is not None else 1
    if args.json:
        print(json.dumps(answer, indent=2))
        return 0
    print(f"agents wait up to {answer['wait_s']:g}s, then {_busy(answer['when_busy'])}")
    for rule in settings.rules:
        print(f"  {rule.source:7}  {', '.join(sorted(rule.programs))}: {_rule_line(rule)}")
    return 0


def _programs(settings: shell.Served, *, write: bool, env) -> int:
    names = programs.names(settings.rules)
    if not write:
        for name in names:
            print(name)
        return 0
    target = programs.path(env)
    if target is None:
        sys.stderr.write("leasehold: set HOME or XDG_CACHE_HOME to say where the list goes\n")
        return 1
    try:
        written = programs.write(target, names)
    except OSError as exc:
        sys.stderr.write(f"leasehold: cannot write {target}: {exc}\n")
        return 1
    if not written:
        odd = [n for n in names if not programs.WORD.match(n)]
        sys.stderr.write(
            f"leasehold: the pre-filter cannot match {', '.join(odd)} as a word, so there is no "
            f"list at {target} and it passes every command to the hook\n"
        )
        return 1
    print(target)
    return 0


def _busy(when_busy: str) -> str:
    return "run without a lease" if when_busy == "run" else "are told the machine is busy"


def _rule_line(rule: shell.Rule) -> str:
    args = f" with arguments matching {rule.args!r}" if rule.args else ""
    never = f"; never granted, so refused: {rule.never}" if rule.never else ""
    return f"weight {rule.weight} in pool {rule.pool}{args}{never}"


def _describe(found: shell.Match) -> dict:
    rule = found.rule
    return {
        "program": found.program,
        "programs": sorted(rule.programs),
        "args": rule.args,
        "weight": rule.weight,
        "pool": rule.pool,
        "source": rule.source,
        "never": rule.never,
    }


def _hook(args, ctx) -> int:
    """Always exits 0: whatever goes wrong here, the agent's command runs."""
    try:
        event = json.load(sys.stdin)
        client = ctx.client()
        if args.event == "pre":
            reply = claude_code.pre(
                event,
                client,
                env=os.environ,
                watch_pid=claude_code.agent_pid(os.getppid()),
            )
        else:
            reply = claude_code.post(event, client, env=os.environ)
    except SystemExit:
        return 0  # no daemon found: the command runs as if leasehold were not installed
    except Exception as exc:  # noqa: BLE001 - a hook must never block the agent's work
        sys.stderr.write(f"leasehold hook: {type(exc).__name__}: {exc}\n")
        return 0
    for line in reply.log:
        sys.stderr.write(f"leasehold hook: {line}\n")
    if reply.output is not None:
        sys.stdout.write(reply.text() + "\n")
    return 0


def _shims(args, ctx) -> int:
    directory = Path(args.dir).expanduser() if args.dir else shims.default_dir(os.environ)
    only = [p for p in (getattr(args, "only", None) or "").split(",") if p] or None
    if args.verb == "list":
        for program in shims.installed(directory):
            print(program)
        return 0
    if args.verb == "remove":
        gone = shims.remove(directory, only)
        print(
            f"removed {len(gone)} shims from {directory}" + (f": {', '.join(gone)}" if gone else "")
        )
        return 0
    return _install(args, ctx, directory, only)


def _install(args, ctx, directory: Path, only: list[str] | None) -> int:
    try:
        rules = shell.served(ctx.client().commands()).rules
    except (Unavailable, ApiError, SystemExit):
        from leasehold.commands import CommandSettings  # no daemon yet: the built-in rules

        rules = tuple(CommandSettings().shell_rules())
    named = sorted({p for rule in rules for p in rule.programs if "/" not in p})
    chosen = [p for p in named if only is None or p in only]
    path = os.environ.get("PATH", os.defpath)
    if not args.all:
        missing = [p for p in chosen if shims.real(p, path, directory) is None]
        chosen = [p for p in chosen if p not in missing]
        if missing:
            print(f"not on PATH, so no shim (--all writes them anyway): {', '.join(missing)}")
    for program, what in shims.install(directory, chosen, python=sys.executable, force=args.force):
        print(f"  {program}: {what}")
    entries = [os.path.realpath(e) for e in path.split(os.pathsep) if e]
    if os.path.realpath(directory) not in entries:
        print("put the shims first on PATH, in your shell's profile:")
        print(f"  export PATH={directory}:$PATH")
    return 0
