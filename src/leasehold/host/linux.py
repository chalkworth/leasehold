"""The Linux host: the kernel counts every process its OOM killer ends (`oom_kill` in
`/proc/vmstat`), whether the machine or a cgroup's limit ran out of memory.

A command that ended by SIGKILL while that count rose most likely died for memory. The count
is for the whole machine, so the process killed may have been another one: the verdict is
`likely`, never `certain`.

A process's hard memory limit is the lowest `memory.max` on its cgroup and those above it
(`leasehold.cgroup`).
"""

from __future__ import annotations

from pathlib import Path

from leasehold import cgroup
from leasehold.host import Ending, Mark, sigkilled


def oom_kills(proc: Path = Path("/proc")) -> int | None:
    """How many processes the OOM killer has ended since boot; None where it cannot be read."""
    try:
        text = (proc / "vmstat").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == "oom_kill" and value.strip().isdigit():
            return int(value)
    return None


class Linux:
    def __init__(
        self, proc: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")
    ) -> None:
        self._proc = proc
        self._cgroup_root = cgroup_root

    def memory_max(self, pid: int) -> int | None:
        path = cgroup.own_cgroup(self._proc, str(pid))
        if path is None:
            return None
        found = cgroup.limit(self._cgroup_root, path, files=("memory.max",))
        return None if found is None else found.limit_bytes

    def mark(self) -> Mark:
        return Mark(oom_kills(self._proc))

    def ended(self, status: int, since: Mark) -> Ending:
        now = oom_kills(self._proc)
        if since.oom_kills is None or now is None:
            return Ending(reason="the kernel's OOM kill count cannot be read")
        kills = max(0, now - since.oom_kills)
        if sigkilled(status) and kills:
            return Ending("kernel", "likely", kills, how="exact")
        return Ending(kills=kills, how="exact")
