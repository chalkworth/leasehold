"""The `leasehold` command.

Each module here registers one group of subcommands (`register`) and handles them: `setup`
(`setup`), `run` (`run`), `daemon`
(`serve`, `init`, `doctor`, `observe`), `leases` (`hold`, `acquire`, `keep`, `release`),
`views` (`status`, `leases`, `facts`, `ui`, `metrics`), `watch` (`top`), `admin` (`lend`,
`pool`), `push` (`report`), `hooks` (`commands`, `hook`, `shims`), `agents` (`mcp`) and
`demo`. A handler takes the parsed arguments and a `Context`, and returns the exit code. A
command that Ctrl-C (SIGINT) interrupts ends by SIGINT, with no traceback (`exits.end_by`).
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from leasehold import __version__, discovery, paths
from leasehold.client import Client
from leasehold.exits import EXIT_UNAVAILABLE, end_by

# The commands `leasehold --help` lists, in this order. Every other command still works and
# is named in the help's closing line. A command listed here that no module registers is
# skipped.
VISIBLE = (
    "setup",
    "run",
    "hold",
    "top",
    "status",
    "leases",
    "doctor",
    "demo",
    "commands",
    "serve",
    "init",
)


class _Formatter(argparse.HelpFormatter):
    """Lists only the `VISIBLE` commands in the command list."""

    def _iter_indented_subactions(self, action):
        for sub in super()._iter_indented_subactions(action):
            if sub.dest in VISIBLE:
                yield sub


Handler = Callable[[argparse.Namespace, "Context"], int]


@dataclass
class Context:
    """What a handler needs beyond its arguments: the environment, and the daemon's client."""

    socket: str | None
    config: str | None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    def socket_path(self) -> Path:
        try:
            return discovery.find(self.socket, self.config, self.env)
        except discovery.DiscoveryError as exc:
            raise SystemExit(f"leasehold: {exc}") from None

    def client(self) -> Client:
        return Client(self.socket_path())

    def unreachable(self, error: Exception) -> int:
        """Say no daemon could be reached, and where it looked; `EXIT_UNAVAILABLE`."""
        try:
            found = discovery.candidates(self.socket, self.config, self.env)
        except discovery.DiscoveryError:
            found = []
        tried = "; ".join(f"{c.source} {c.path}: {c.reason}" for c in found)
        sys.stderr.write(
            f"leasehold: no daemon answers ({error}).\n  tried: {tried}\n"
            "  start one with `leasehold serve`, or see `leasehold doctor`\n"
        )
        return EXIT_UNAVAILABLE


def _parser() -> argparse.ArgumentParser:
    from leasehold.cli import (
        admin,
        agents,
        daemon,
        demo,
        hooks,
        leases,
        push,
        run,
        setup,
        views,
        watch,
    )

    p = argparse.ArgumentParser(
        prog="leasehold",
        description="Fair, load-aware admission for one shared Linux machine.",
        formatter_class=_Formatter,
    )
    p.add_argument("--version", action="version", version=f"leasehold {__version__}")
    p.add_argument(
        "--config",
        help=f"machine config (default ${paths.ENV_CONFIG}, then the user's, then the system's)",
    )
    p.add_argument(
        "--socket",
        help=f"daemon socket (default ${discovery.ENV_SOCKET}, else the first daemon that answers)",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for module in (setup, run, daemon, leases, views, watch, admin, push, hooks, agents, demo):
        module.register(sub)
    advanced = _order_commands(sub)
    p.epilog = f"advanced: {', '.join(advanced)}  (`leasehold COMMAND --help`)"
    _also_after_the_command(p, ("--config", "--socket"))
    return p


def _order_commands(sub: argparse._SubParsersAction) -> list[str]:
    """Put the `VISIBLE` commands first, in order, then the rest as registered; return the
    names of the rest."""
    names = list(sub.choices)
    rank = {name: i for i, name in enumerate(VISIBLE)}
    names.sort(key=lambda n: rank.get(n, len(rank)))  # stable: the rest keep their order
    sub.choices = {n: sub.choices[n] for n in names}
    sub._choices_actions.sort(key=lambda a: rank.get(a.dest, len(rank)))
    return [n for n in names if n not in rank]


def _also_after_the_command(parser: argparse.ArgumentParser, names: Sequence[str]) -> None:
    """Accept `names`, the top-level options, after any command as well as before it
    (`leasehold serve --config x`), the later one winning."""
    top = {a.option_strings[0]: a for a in parser._actions if a.option_strings}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for child in dict.fromkeys(action.choices.values()):  # aliases share a parser
            have = {o for a in child._actions for o in a.option_strings}
            for name in names:
                if name not in have:
                    child.add_argument(name, help=top[name].help, default=argparse.SUPPRESS)
            _also_after_the_command(child, names)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args, Context(socket=args.socket, config=args.config))
    except KeyboardInterrupt:
        return end_by(signal.SIGINT)
