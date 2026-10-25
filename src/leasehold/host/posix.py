"""The fallback host, for a system leasehold has no port for yet (macOS among them): it cannot
tell a memory kill from any other, so it never gives a verdict."""

from __future__ import annotations

import sys

from leasehold.host import Ending, Mark


class Posix:
    def mark(self) -> Mark:
        return Mark()

    def ended(self, status: int, since: Mark) -> Ending:
        return Ending(reason=f"leasehold cannot tell memory kills apart on {sys.platform} yet")

    def memory_max(self, pid: int) -> int | None:
        return None
