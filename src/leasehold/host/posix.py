"""The fallback host, for a system leasehold has no port for yet (macOS among them): it cannot
tell a memory kill from any other, so it never gives a verdict, and it cannot cap a command's
memory, which `memory_cap` says.

macOS has no limit on a tree of processes' memory that a user can set without root
(`RLIMIT_AS` bounds address space, which every process reserves far more of than it uses), so
its cap will be polled, by the macOS port; until then a cap asked for there is not enforced,
and `hold`, `doctor` and the status say so. For the same reason it adopts nothing: a lease taken
for an agent's command there only counts it, and `adoption` says why."""

from __future__ import annotations

import sys

from leasehold.host import Adoption, Cap, Ending, Mark, TreeSpec


class Posix:
    def mark(self) -> Mark:
        return Mark()

    def ended(self, status: int, since: Mark) -> Ending:
        return Ending(reason=f"leasehold cannot tell memory kills apart on {sys.platform} yet")

    def memory_max(self, pid: int) -> int | None:
        return None

    def memory_cap(self) -> Cap:
        if sys.platform == "darwin":
            return Cap(
                "none",
                "macOS has no per-command memory limit without root, and leasehold's polled "
                "cap for macOS is not built yet",
            )
        return Cap("none", f"leasehold cannot cap a command's memory on {sys.platform} yet")

    def cap_properties(self, limit_bytes: int) -> list[str]:
        return []

    def cap_ended(self, scope: str) -> bool | None:
        return None

    def adoption(self) -> Cap:
        if sys.platform == "darwin":
            return Cap(
                "none",
                "macOS has no per-command group without root, and leasehold's polled tree for "
                "macOS is not built yet: an agent's commands are leased, not contained",
            )
        return Cap("none", f"leasehold cannot contain a running command on {sys.platform} yet")

    def adopt(self, pids: list[int], spec: TreeSpec) -> Adoption:
        return Adoption("none", reason=self.adoption().reason)
