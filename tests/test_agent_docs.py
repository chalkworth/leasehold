"""The instruction files agents read stay truthful: every path they name exists, every
`leasehold` command they name is registered, and each stays short enough to be read whole."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from leasehold.cli import _parser

ROOT = Path(__file__).resolve().parent.parent

#: Each instruction file and the most bytes it may take (its size when the cap was set, plus
#: about a quarter).
SIZE_CAPS = {
    "AGENTS.md": 4400,
    "integrations/AGENTS.md.snippet": 1300,
    "integrations/claude-code/skills/leasehold/SKILL.md": 2800,
}

#: Backticked tokens that look like repo paths but are not meant to be in the repo, each with
#: its reason, keyed by file and then token.
OUTSIDE_THE_REPO: dict[str, dict[str, str]] = {}

#: Where a bare name such as `cli/__init__.py` may be found, besides the repository root.
SEARCH_DIRS = (ROOT, ROOT / "src" / "leasehold")

#: Files at the top of the repository that a name alone identifies.
TOP_LEVEL_FILES = {".importlinter", "LICENSE", "Makefile"}
EXTENSIONS = (".md", ".py", ".toml", ".json", ".yml", ".yaml", ".sh", ".snippet", ".lock")

_FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.M | re.S)
_SPAN = re.compile(r"`([^`\n]+(?:\n[^`\n]+)?)`")


def _spans(text: str):
    """Yield `(line, token)` for each inline code span in `text`, fenced blocks left out. A
    span may wrap onto a second line; its whitespace is then collapsed."""
    blanked = _FENCE.sub(lambda m: "\n" * m.group().count("\n"), text)
    for m in _SPAN.finditer(blanked):
        yield blanked.count("\n", 0, m.start()) + 1, " ".join(m.group(1).split())


def _looks_like_path(token: str) -> bool:
    if re.search(r"[\s<>*{}$=(),;:]", token) or token.startswith(("-", "http")):
        return False
    return "/" in token or token.endswith(EXTENSIONS) or token in TOP_LEVEL_FILES


def _exists(token: str) -> bool:
    return any((base / token).exists() for base in SEARCH_DIRS)


def _subcommands() -> set[str]:
    sub = next(a for a in _parser()._actions if hasattr(a, "choices") and a.choices)
    return set(sub.choices)


def _problems(rel: str) -> list[str]:
    text = (ROOT / rel).read_text()
    outside = OUTSIDE_THE_REPO.get(rel, {})
    commands = _subcommands()
    found = []
    for line, token in _spans(text):
        if _looks_like_path(token) and token not in outside and not _exists(token):
            found.append(f"{rel}:{line}: `{token}` is not a path in the repository")
        if token.startswith("leasehold "):
            try:
                words = shlex.split(token)
            except ValueError:
                words = token.split()
            if len(words) > 1 and not words[1].startswith("-") and words[1] not in commands:
                found.append(f"{rel}:{line}: `{token}` names no registered subcommand")
    return found


def test_every_exemption_has_a_reason():
    for rel, tokens in OUTSIDE_THE_REPO.items():
        assert all(tokens.values()), f"{rel}: an entry in OUTSIDE_THE_REPO has no reason"


@pytest.mark.parametrize("rel", SIZE_CAPS)
def test_instruction_files_name_only_what_exists(rel):
    assert not (problems := _problems(rel)), "\n".join(problems)


@pytest.mark.parametrize("rel", SIZE_CAPS)
def test_instruction_files_stay_short(rel):
    size = (ROOT / rel).stat().st_size
    assert size <= SIZE_CAPS[rel], (
        f"{rel} is {size} bytes, over its cap of {SIZE_CAPS[rel]} in tests/test_agent_docs.py: "
        "shorten the file, or raise the cap with a reason"
    )
