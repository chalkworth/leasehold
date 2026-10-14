"""The word list the Claude Code hook's pre-filter reads (`integrations/claude-code/hooks/
prefilter.sh`): every program basename the served `[commands]` rules name, one per line, in
`$XDG_CACHE_HOME/leasehold/programs` (`~/.cache/leasehold/programs` by default).

The pre-filter starts the Python hook only when a word of a Bash command is in the list, or
when there is no list. So the list is either complete or absent: when a rule names a program
the pre-filter cannot see as one word (a character outside `WORD`), `write` removes the list
instead of writing it.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from leasehold import shell

#: The characters the pre-filter reads as part of a word; any other ends one.
WORD = re.compile(r"^[A-Za-z0-9._+-]+$")


def path(env: Mapping[str, str]) -> Path | None:
    """Where the list is, or None when `env` sets neither `XDG_CACHE_HOME` nor `HOME`."""
    cache = env.get("XDG_CACHE_HOME") or (
        os.path.join(env["HOME"], ".cache") if env.get("HOME") else None
    )
    return None if cache is None else Path(cache) / "leasehold" / "programs"


def names(rules: Iterable[shell.Rule]) -> list[str]:
    """The program basenames `rules` name, sorted. A name with a `/` never matches a program
    (rules compare basenames), so it is left out."""
    return sorted({p for rule in rules for p in rule.programs if p and "/" not in p})


def write(target: Path, programs: Iterable[str]) -> bool:
    """Write `programs` to `target`, replacing it whole; or, when one of them is not a `WORD`,
    remove `target` so the pre-filter passes every command on. Whether a list was written."""
    programs = list(programs)
    if not all(WORD.match(p) for p in programs):
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(f"{p}\n" for p in programs)
    with contextlib.suppress(OSError):
        if target.read_text() == text:
            return True
    partial = target.with_name(f".{target.name}.{os.getpid()}")
    partial.write_text(text)
    os.replace(partial, target)
    return True
