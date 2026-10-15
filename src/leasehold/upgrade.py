"""Bringing a user's config from leasehold 0.2 up to today's defaults, only when asked.

0.2's `init` wrote a user's config (`mode = "user"`) with no `[memory]` table: memory not
counted, and the guard holding pools to their reservations. The table is optional: a config
without it works as it did. `outdated` says what such a config lacks; `plan` works out the new
text and `apply` writes it, the old file kept beside it first:

* a config exactly as 0.2's `init` wrote it is rendered afresh from today's template, for the
  same name, slots, CPUs, per-lease memory and description (the built-in rules keep their
  weights);
* any other keeps every line it has and gains only a `[memory]` table at the end (and today's
  comment above `[guard]`, if 0.2's is there unchanged);
* neither is edited when a `[[commands.rule]]` declares more memory than the new budget or the
  `[ceiling]` memory, since a declared size is then refused, not only held to: `plan` names
  the rules to change first.

`apply` writes only a regular file with one link (not a symbolic link, not a hard-linked one),
through a new temporary file synced to disk, and only if the file is still the one `plan`
read.

Nothing here runs on its own: `setup` and `doctor` only report what `outdated` finds.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from leasehold import template
from leasehold.machine import MachineConfig, MemorySettings
from leasehold.units import memory_to_bytes

#: The `[memory]` table an edited config gains.
MEMORY_TABLE = (
    "\n# Memory admission (added by `leasehold setup --upgrade-config`): a command starts only\n"
    "# while the memory it counts fits the budget; see docs/reference.md.\n"
    "[memory]\n"
    'budget = "auto"          # the machine\'s memory less `kept`; or a size such as "12g"\n'
    'kept = "25%"             # kept back for you: a share of the memory, or a size\n'
    'assumed_per_slot = "auto"\n'
)
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
    """A user's own config, as `init --user` writes it: `mode = "user"` named. A config that
    leaves the mode out may be either kind, and is left alone."""
    return config.mode == "user"


def _lacks_memory(config: MachineConfig) -> bool:
    return _user(config) and config.memory is None


def outdated(config: MachineConfig) -> list[str]:
    """What a user's own config lacks of today's defaults, in words; empty when nothing, or
    when it does not name `mode = "user"` (the machine's config, which `init` never wrote
    this way, or one written by hand)."""
    return ["no [memory] table: memory is not counted"] if _lacks_memory(config) else []


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
    #: The file `plan` read: (device, inode, size, modification time in ns).
    identity: tuple[int, int, int, int] | None = None

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


def _identity(path: Path) -> tuple[int, int, int, int]:
    st = path.stat(follow_symlinks=False)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _unsafe(path: Path) -> str | None:
    """Why the file at `path` is not one to write over, or None."""
    st = path.stat(follow_symlinks=False)
    if path.is_symlink():
        return (
            f"{path} is a symbolic link (to {os.readlink(path)}); run it on the file itself "
            "with --config, or change it by hand"
        )
    if st.st_nlink > 1:
        return (
            f"{path} has {st.st_nlink} hard links, which writing a new file would part; "
            "change it by hand"
        )
    return None


def _oversized_rules(config: MachineConfig, memory_bytes: int | None) -> list[str]:
    """The rules whose memory a `[memory]` table would refuse: more than the budget it would
    have on this machine, or than `[ceiling] memory`."""
    limits = []
    if memory_bytes:
        budget = MemorySettings().resolve(memory_bytes=memory_bytes, slots=config.ceiling.slots)
        limits.append(("the memory budget it would have", budget.budget))
    if config.ceiling.memory is not None:
        limits.append(("[ceiling] memory", memory_to_bytes(config.ceiling.memory)))
    out = []
    for n, rule in enumerate(config.commands.rule):
        if rule.memory is None:
            continue
        size = memory_to_bytes(rule.memory)
        for what, limit in limits:
            if size > limit:
                out.append(
                    f"commands.rule[{n}] ({', '.join(rule.programs)}) declares {rule.memory}, "
                    f"more than {what} ({limit / 2**30:.1f} GiB)"
                )
                break
    return out


def manual_steps(config: MachineConfig) -> str:
    """What to add by hand, for a config `plan` will not edit."""
    return (
        'add a [memory] table (budget = "auto", kept = "25%", assumed_per_slot = "auto"); '
        "docs/reference.md describes it"
    )


def plan(path: Path, config: MachineConfig, memory_bytes: int | None = None) -> Plan | None:
    """The upgrade of the config at `path` (already loaded as `config`) on a machine of
    `memory_bytes` (None: unknown), or None when it lacks nothing."""
    lacks = outdated(config)
    if not lacks:
        return None
    identity = _identity(path)
    old = path.read_text()
    unsafe = _unsafe(path)
    if unsafe is not None:
        return Plan(path, old, None, False, (), manual=unsafe, identity=identity)
    oversized = _oversized_rules(config, memory_bytes) if _lacks_memory(config) else []
    if oversized:
        manual = (
            "; ".join(oversized)
            + ". With memory counted, a declared size over these is refused, not only held "
            "to: lower or remove those rules' memory, then run it again"
        )
        return Plan(path, old, None, False, (), manual=manual, identity=identity)
    if _pristine(config, old):
        c = config.ceiling
        assert c.cpus is not None and c.memory is not None
        text = template.render(
            name=config.name, mode="user", slots=c.slots, cpus=c.cpus, memory=c.memory,
            about=_about(old),
        )  # fmt: skip
        changes = (
            "rewritten from today's template (it was exactly as leasehold 0.2 wrote it): "
            '[memory] budget = "auto", kept = "25%"; every other default and comment as '
            "today's",
        )
        return Plan(path, old, text, True, changes, identity=identity)
    text = old
    changes: list[str] = []
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
        return Plan(path, old, None, False, (), manual=manual_steps(config), identity=identity)
    return Plan(path, old, text, False, tuple(changes), identity=identity)


def backup_path(path: Path) -> Path:
    """Where `apply` keeps the old file: `<name>.0.2.bak`, numbered if that is taken."""
    candidate = path.with_name(f"{path.name}.0.2.bak")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.0.2.bak.{n}")
        n += 1
    return candidate


def _unchanged(p: Plan) -> None:
    """Raise ValueError unless the file is still the one `plan` read, and still safe to write."""
    unsafe = _unsafe(p.path)
    if unsafe is not None:
        raise ValueError(unsafe)
    if _identity(p.path) != p.identity or p.path.read_text() != p.old:
        raise ValueError(f"{p.path} changed while it was being upgraded; run it again")


def _write_new(path: Path, text: str, mode: int, *, prefix: str) -> Path:
    """`text` in a new file beside `path` (never an existing one, never through a link),
    synced to disk."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".upgrading")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


def apply(p: Plan) -> Path:
    """Write `p.text` over its file, after copying the file as it is to `backup_path`; the
    backup's path. Refuses (ValueError) a symbolic or hard-linked file, and one that changed
    since `plan` read it: checked first, and again just before the new file takes its place."""
    if p.text is None:
        raise ValueError(f"{p.path} cannot be upgraded automatically: {p.manual}")
    _unchanged(p)
    mode = p.path.stat().st_mode & 0o777
    backup = backup_path(p.path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(backup, flags, mode), "w") as out:
        out.write(p.old)
        out.flush()
        os.fsync(out.fileno())
    tmp = _write_new(p.path, p.text, mode, prefix=f".{p.path.name}.")
    try:
        _unchanged(p)
        os.replace(tmp, p.path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        directory = os.open(p.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return backup
