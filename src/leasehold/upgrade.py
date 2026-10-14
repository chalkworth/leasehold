"""Bringing a user's config from leasehold 0.2 up to today's defaults, only when asked.

0.2's `init` wrote a user's config with no `[memory]` table (memory not counted, and the guard
holding pools to their reservations) and left the built-in rules at weight 4, so on a typical
machine one build or test run ran at a time. `outdated` says what such a config lacks; `plan`
works out the new text and `apply` writes it, the old file kept beside it first:

* a config exactly as 0.2's `init` wrote it is rendered afresh from today's template, for the
  same name, slots, CPUs, per-lease memory and description;
* any other keeps every line it has and gains only what it lacks: `heavy_weight` and
  `light_weight` under `[commands]`, and a `[memory]` table at the end. A `[commands]` written
  other than as a `[commands]` header (a dotted key, an inline table) is not edited: `plan`
  says what to add by hand instead.

Nothing here runs on its own: `setup` and `doctor` only report what `outdated` finds.
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leasehold import template
from leasehold.commands import HEAVY_WEIGHT
from leasehold.machine import MachineConfig

#: The `[memory]` table an edited config gains.
MEMORY_TABLE = (
    "\n# Memory admission (added by `leasehold setup --upgrade-config`): a command starts only\n"
    "# while the memory it counts fits the budget; see docs/reference.md.\n"
    "[memory]\n"
    'budget = "auto"          # the machine\'s memory less `kept`; or a size such as "12g"\n'
    'kept = "25%"             # kept back for you: a share of the memory, or a size\n'
    'assumed_per_slot = "auto"\n'
)
_COMMANDS = re.compile(r"^\s*\[\s*commands\s*\]\s*(#.*)?$")
#: 0.2's comment above `[guard]`, which no longer holds once memory is counted, and today's.
GUARD_COMMENT_0_2 = (
    "# While memory is short, every pool admits only its reserved slots: what runs keeps running,\n"
    "# the rest waits in line and says why.\n"
)
GUARD_COMMENT = (
    "# While memory is short, what runs keeps running and new work starts only within the memory\n"
    "# measured free: none while pressure is high, then a little at a time as it eases. What "
    "waits\n# says why.\n"
)


def _user(config: MachineConfig) -> bool:
    return (config.mode or "user") == "user"


def _lacks_memory(config: MachineConfig) -> bool:
    return _user(config) and config.memory is None


def _lacks_weights(config: MachineConfig) -> bool:
    """Whether the built-in heavy rules are left at 0.2's weight where that lets only one run
    at a time, though half the slots would let two."""
    commands, slots = config.commands, config.ceiling.slots
    return (
        _user(config)
        and commands.defaults
        and "heavy_weight" not in commands.model_fields_set
        and 4 <= slots < HEAVY_WEIGHT * 2
    )


def outdated(config: MachineConfig) -> list[str]:
    """What a user's own config lacks of today's defaults, in words; empty when nothing (or
    when it is the machine's config, which `init` never wrote this way)."""
    lacks = []
    if _lacks_memory(config):
        lacks.append("memory is not counted (no [memory] table)")
    slots = config.ceiling.slots
    if _lacks_weights(config):
        lacks.append(
            f"the built-in build and test rules weigh {HEAVY_WEIGHT} of {slots} slots, so "
            "one runs at a time"
        )
    return lacks


@dataclass(frozen=True)
class Plan:
    """What `apply` would write: `text` (None when the file cannot be edited safely, `manual`
    then says what to add by hand), whether the file was 0.2's own (`pristine`), and each
    change in words."""

    path: Path
    old: str
    text: str | None
    pristine: bool
    changes: tuple[str, ...]
    manual: str | None = None

    def diff(self) -> str:
        if self.text is None:
            return ""
        return "".join(
            difflib.unified_diff(
                self.old.splitlines(keepends=True),
                self.text.splitlines(keepends=True),
                fromfile=str(self.path),
                tofile=f"{self.path} (upgraded)",
            )
        )


def _about(text: str) -> str:
    lines = text.splitlines()
    return lines[1][2:] if len(lines) > 1 and lines[1].startswith("# ") else ""


def _pristine(config: MachineConfig, text: str) -> bool:
    c = config.ceiling
    if c.cpus is None or c.memory is None:
        return False
    return any(
        template.render_user_0_2(
            name=config.name, slots=c.slots, cpus=c.cpus, memory=c.memory, about=_about(text),
            release=release,
        ) == text
        for release in template.USER_0_2
    )  # fmt: skip


def _weights_lines(slots: int) -> str:
    heavy, light = template.weights(slots)
    return (
        "# The built-in rules' weights (added by `leasehold setup --upgrade-config`): builds and\n"
        "# test runs, and JavaScript tooling.\n"
        f"heavy_weight = {heavy}\n"
        f"light_weight = {light}\n"
    )


def manual_steps(config: MachineConfig) -> str:
    """What to add by hand, for a config `plan` will not edit."""
    heavy, light = template.weights(config.ceiling.slots)
    steps = []
    if _lacks_memory(config):
        steps.append('a [memory] table (budget = "auto", kept = "25%", assumed_per_slot = "auto")')
    if _lacks_weights(config):
        steps.append(f"heavy_weight = {heavy} and light_weight = {light} under [commands]")
    return "add " + ", and ".join(steps) + "; docs/reference.md describes each"


def plan(path: Path, config: MachineConfig) -> Plan | None:
    """The upgrade of the config at `path` (already loaded as `config`), or None when it lacks
    nothing."""
    lacks = outdated(config)
    if not lacks:
        return None
    old = path.read_text()
    if _pristine(config, old):
        c = config.ceiling
        assert c.cpus is not None and c.memory is not None
        text = template.render(
            name=config.name, mode="user", slots=c.slots, cpus=c.cpus, memory=c.memory,
            about=_about(old),
        )  # fmt: skip
        heavy, light = template.weights(c.slots)
        changes = (
            "rewritten from today's template (it was exactly as leasehold 0.2 wrote it): "
            f'[memory] budget = "auto", kept = "25%"; heavy_weight = {heavy}, '
            f"light_weight = {light}; every other default and comment as today's",
        )
        return Plan(path, old, text, True, changes)
    text = old
    changes: list[str] = []
    if _lacks_weights(config):
        lines = text.splitlines(keepends=True)
        at = next((i for i, line in enumerate(lines) if _COMMANDS.match(line)), None)
        if at is None and "commands" in tomllib.loads(text):
            return Plan(path, old, None, False, (), manual=manual_steps(config))
        weights = _weights_lines(config.ceiling.slots)
        if at is None:
            if not text.endswith("\n"):
                text += "\n"
            text += "\n[commands]\n" + weights
        else:
            lines.insert(at + 1, weights)
            text = "".join(lines)
        heavy, light = template.weights(config.ceiling.slots)
        changes.append(f"[commands] gains heavy_weight = {heavy} and light_weight = {light}")
    if _lacks_memory(config):
        if not text.endswith("\n"):
            text += "\n"
        text += MEMORY_TABLE
        changes.append('a [memory] table: budget = "auto", kept = "25%", assumed_per_slot "auto"')
        if text.count(GUARD_COMMENT_0_2) == 1:
            text = text.replace(GUARD_COMMENT_0_2, GUARD_COMMENT)
            changes.append("the comment above [guard], which described 0.2's guard")
    try:
        MachineConfig.model_validate(tomllib.loads(text))
    except (ValueError, tomllib.TOMLDecodeError):
        return Plan(path, old, None, False, (), manual=manual_steps(config))
    return Plan(path, old, text, False, tuple(changes))


def backup_path(path: Path) -> Path:
    """Where `apply` keeps the old file: `<name>.0.2.bak`, numbered if that is taken."""
    candidate = path.with_name(f"{path.name}.0.2.bak")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.0.2.bak.{n}")
        n += 1
    return candidate


def apply(p: Plan) -> Path:
    """Write `p.text` over its file, after copying the file as it is to `backup_path`; the
    backup's path. Refuses (ValueError) when the file changed since `plan` read it."""
    if p.text is None:
        raise ValueError(f"{p.path} cannot be upgraded automatically: {p.manual}")
    if p.path.read_text() != p.old:
        raise ValueError(f"{p.path} changed while it was being upgraded; run it again")
    backup = backup_path(p.path)
    mode = p.path.stat().st_mode & 0o777
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(backup, flags, mode), "w") as out:
        out.write(p.old)
    tmp = p.path.with_name(f".{p.path.name}.upgrading")
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w") as out:
        out.write(p.text)
    os.replace(tmp, p.path)
    return backup
