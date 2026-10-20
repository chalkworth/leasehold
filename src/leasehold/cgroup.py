"""The memory limit a process's cgroup holds it to (cgroup v2 only).

A process in a container, in a systemd slice or scope with `MemoryMax` or `MemoryHigh`, or in a
user slice with a limit, may have far less memory than the machine: everything under one cgroup
shares the limits set on that cgroup and on each of its ancestors. `limit` finds the lowest of
them, `memory.max` or `memory.high`, from the process's own cgroup (`/proc/<pid>/cgroup`) up to
the root of the cgroup mount, ignoring `max` (no limit). Inside a cgroup namespace (a
container) the mount's root is the container's own cgroup, and its limit counts too.

On cgroup v1, or when a file cannot be read, there is no limit (None). Both roots are
injectable, so tests read fixture trees.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

#: The files whose value bounds a cgroup's memory: past `memory.high` the kernel throttles and
#: reclaims, past `memory.max` it kills.
LIMIT_FILES = ("memory.max", "memory.high")


@dataclass(frozen=True)
class MemoryLimit:
    #: The lowest limit, in bytes.
    limit_bytes: int
    #: The cgroup that sets it, as a path under the cgroup root (`/` for the root itself).
    cgroup: str


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def own_cgroup(proc: Path = Path("/proc"), pid: str = "self") -> str | None:
    """The cgroup v2 path of process `pid` (`/user.slice/...`), or None when it has none."""
    for line in (_read(proc / pid / "cgroup") or "").splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if hierarchy == "0" and controllers == "" and path.startswith("/"):
            return path.strip()
    return None


def _bytes(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def limit(cgroup_root: Path, cgroup: str) -> MemoryLimit | None:
    """The lowest memory limit on `cgroup` and its ancestors up to the root of `cgroup_root`,
    or None when none of them sets one (or `cgroup_root` is not cgroup v2)."""
    if not (cgroup_root / "cgroup.controllers").is_file():
        return None
    path = PurePosixPath(cgroup)
    if not path.is_absolute() or ".." in path.parts:
        return None
    lowest: MemoryLimit | None = None
    for group in (path, *path.parents):
        base = cgroup_root / str(group).lstrip("/")
        for name in LIMIT_FILES:
            value = _bytes(_read(base / name))
            if value is not None and (lowest is None or value < lowest.limit_bytes):
                lowest = MemoryLimit(value, str(group))
    return lowest


def used(cgroup_root: Path, cgroup: str) -> int | None:
    """The memory everything under `cgroup` uses now, in bytes: `memory.current` less the
    inactive file cache (`inactive_file` in `memory.stat`), which the kernel reclaims first
    when the cgroup nears its limit. None when `memory.current` cannot be read."""
    base = cgroup_root / cgroup.lstrip("/")
    current = _bytes(_read(base / "memory.current"))
    if current is None:
        return None
    for line in (_read(base / "memory.stat") or "").splitlines():
        name, _, value = line.partition(" ")
        if name == "inactive_file" and value.strip().isdigit():
            return max(0, current - int(value))
    return current
