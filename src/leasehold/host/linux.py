"""The Linux host: the kernel counts every process its OOM killer ends (`oom_kill` in
`/proc/vmstat`), whether the machine or a cgroup's limit ran out of memory.

A command that ended by SIGKILL while that count rose most likely died for memory. The count
is for the whole machine, so the process killed may have been another one: the verdict is
`likely`, never `certain`.

A process's hard memory limit is the lowest `memory.max` on its cgroup and those above it
(`leasehold.cgroup`).

A command's own cap is `MemoryMax` on its systemd scope with `MemorySwapMax=0`, so that a
runaway is killed rather than pushed to swap, and `memory.oom.group` (set as the scope starts,
`leasehold.contain.spawn`), so that the whole command dies together. It holds only for a
command in a scope (`leasehold.contain.probe` says where one can be made). Whether the OOM
killer ended a capped command is what systemd says of its scope (`Result=oom-kill`).

Running processes are adopted into a new scope of the user manager (`leasehold.contain.adopt`),
where `leasehold.contain.probe` says a scope can be made for them: never from under a
`memory.max` they already run under. A negative `oom_score_adj` they inherited is raised to 0,
as for a command `leasehold.contain.spawn` starts. An uncapped scope keeps running when the OOM
killer ends one of its processes; a capped one is ended whole (`memory.oom.group`).
"""

from __future__ import annotations

import contextlib
from pathlib import Path, PurePosixPath

from leasehold import cgroup, contain
from leasehold.host import Adoption, Cap, Ending, Mark, TreeSpec, sigkilled


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

    def memory_cap(self) -> Cap:
        if not (self._cgroup_root / "cgroup.controllers").is_file():
            return Cap("none", f"no cgroup v2 at {self._cgroup_root}")
        return Cap("exact")

    def cap_properties(self, limit_bytes: int) -> list[str]:
        return [f"MemoryMax={limit_bytes}", "MemorySwapMax=0"]

    def cap_ended(self, scope: str) -> bool | None:
        result = contain.cap_result(scope)
        return None if result is None else result == "oom-kill"

    def adoption(self) -> Cap:
        return self.memory_cap()

    def adopt(self, pids: list[int], spec: TreeSpec) -> Adoption:
        if not pids:
            return Adoption("none", reason="no process to adopt")
        found = contain.probe(
            cgroup_root=self._cgroup_root,
            self_cgroup=self._proc / str(pids[0]) / "cgroup",
            answers=lambda: True,  # asked by the move itself
        )
        if not found.enforced:
            return Adoption("none", reason=found.reason)
        props = contain.scope_properties(
            weight=spec.weight, memory=spec.memory, hard_cpu=spec.hard_cpu
        )
        capped = spec.cap_bytes is not None
        if capped:
            props += self.cap_properties(spec.cap_bytes)
        why = contain.adopt(
            pids, spec.unit, spec.slice_, props, oom_continue=not capped, proc=self._proc
        )
        if why is not None:
            return Adoption("none", reason=why)
        for pid in pids:
            _raise_oom_score(self._proc / str(pid) / "oom_score_adj")
        path = cgroup.own_cgroup(self._proc, str(pids[0]))
        if capped and path is not None:
            with contextlib.suppress(OSError):
                (self._cgroup_root / path.lstrip("/") / "memory.oom.group").write_text("1")
        limit = None
        if path is not None:
            parent = str(PurePosixPath(path).parent)
            above = cgroup.limit(self._cgroup_root, parent, files=("memory.max",))
            limit = None if above is None else above.limit_bytes
        return Adoption("exact", spec.unit, limit_bytes=limit, capped=capped)

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


def _raise_oom_score(path: Path) -> None:
    """Raise a negative `oom_score_adj` to 0, and leave any other alone (`contain.oom_reset_code`
    does the same for a command `contain.spawn` starts)."""
    try:
        if int(path.read_text()) < 0:
            path.write_text("0")
    except (OSError, ValueError):
        pass
