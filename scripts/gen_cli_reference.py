"""Write the command-line section of docs/reference.md from the parser itself.

    uv run python scripts/gen_cli_reference.py          # rewrite it
    uv run python scripts/gen_cli_reference.py --check  # exit 1 when it is out of date

The section is everything between `<!-- cli:start -->` and `<!-- cli:end -->`: for each command,
its one-line help and its usage, as `leasehold COMMAND --help` prints them.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "reference.md"
START, END = "<!-- cli:start -->", "<!-- cli:end -->"


def _commands(parser: argparse.ArgumentParser) -> list[tuple[str, argparse.ArgumentParser]]:
    """Each command under `parser` with its help, a command with commands of its own
    (`shims install`) as each of those."""
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if sub is None:
        return []
    helps = {choice.dest: choice.help or "" for choice in sub._choices_actions}
    out = []
    for name, child in sub.choices.items():
        inner = _commands(child)
        out += inner if inner else [(helps.get(name, ""), child)]
    return out


def section() -> str:
    os.environ["COLUMNS"] = "1000"  # one line each: argparse wraps usage to the terminal
    from leasehold.cli import _parser

    out = []
    for help_text, child in _commands(_parser()):
        usage = child.format_usage().removeprefix("usage: ").strip()
        for common in (" [-h]", " [--config CONFIG]", " [--socket SOCKET]"):
            usage = usage.replace(common, "")
        out.append(f"{help_text}:\n\n```\n{usage}\n```\n")
    return "\n".join(out)


def rewrite(text: str) -> str:
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n\n{section()}\n{END}{tail}"


def main() -> int:
    check = "--check" in sys.argv[1:]
    text = REFERENCE.read_text()
    new = rewrite(text)
    if check:
        if new != text:
            print("docs/reference.md: the command line is out of date; run", __file__)
            return 1
        return 0
    REFERENCE.write_text(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
