"""What leasehold asks of the operating system, behind one small interface: `current()` is the
`Host` for this platform.

For now a host tells whether a command that has ended was most likely killed for memory.
`mark()` takes the counters before the command starts, and `ended(status, since)` reads them
again once it has ended. `memory_max(pid)` is the hard memory limit a process runs under,
where the platform has one, so that a kill can name the limit rather than the machine.

A host also says how a hard cap on one command's memory (`memory_max`) works here
(`memory_cap()`), gives the properties that set one on a command's scope (`cap_properties`),
and, once a capped command has ended, whether the OOM killer ended it (`cap_ended`).

Each answer says how it was measured (`how`): `exact` when the kernel counted it, `none` when
this platform has no way to tell, and then why (`reason`), so that no message claims more than
the platform gave.

`linux` reads the kernel's OOM kill counter, and caps a command with `MemoryMax` on its systemd
scope. `posix` is the fallback for every other system: it knows nothing, and caps nothing. On
macOS no kernel limit holds a tree of processes without root, so a cap there will be polled;
until that is built, `memory_cap()` says there is none, and why. A command's own cgroup
(`leasehold.measure`) says more than either, and stays outside this interface for now.

This package imports only leasehold's leaf modules, and never the daemon.
"""

from __future__ import annotations

import signal
import sys
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Mark:
    """Counters taken before a command starts. `oom_kills` is how many processes the OOM killer
    had ended on this machine (None: not known here)."""

    oom_kills: int | None = None

    def encode(self) -> str:
        """The mark as one word, for a note kept between two processes (`decode`)."""
        return "-" if self.oom_kills is None else str(self.oom_kills)

    @classmethod
    def decode(cls, word: str | None) -> Mark:
        """The mark `encode` wrote; an empty mark for anything else."""
        return cls(int(word)) if word and word.isdigit() else cls()


@dataclass(frozen=True)
class Ending:
    """How a command ended, as far as memory goes. `memory` is `kernel` when the OOM killer most
    likely ended it, `cap` when it ended it at the command's own hard cap, else None.
    `confidence` is `certain` or `likely` (None with no memory verdict). `kills` is how many
    processes the OOM killer ended on the machine while it ran (None: not known). `how` and
    `reason` say how this was measured, and why not when `how` is `none`."""

    memory: str | None = None
    confidence: str | None = None
    kills: int | None = None
    how: str = "none"
    reason: str | None = None


@dataclass(frozen=True)
class Cap:
    """How a hard cap on one command's memory works here. `how` is `exact` when the kernel
    holds the command's whole tree to it, `polled` when leasehold reads the tree's memory and
    kills it past the cap, and `none` when no cap can be set here, and then why (`reason`)."""

    how: str
    reason: str | None = None


class Host(Protocol):
    def mark(self) -> Mark: ...

    def ended(self, status: int, since: Mark) -> Ending: ...

    def memory_max(self, pid: int) -> int | None: ...

    def memory_cap(self) -> Cap: ...

    def cap_properties(self, limit_bytes: int) -> list[str]: ...

    def cap_ended(self, scope: str) -> bool | None: ...


def killed_by(status: int) -> int | None:
    """The signal that ended a command with `status`: negative as `subprocess` reports it, or
    128 and the signal as a shell reports it. None for a plain exit."""
    if status < 0:
        return -status
    if 128 < status < 128 + 65:
        return status - 128
    return None


def sigkilled(status: int) -> bool:
    return killed_by(status) == signal.SIGKILL


def current() -> Host:
    """The host for this platform."""
    if sys.platform.startswith("linux"):
        from leasehold.host import linux

        return linux.Linux()
    from leasehold.host import posix

    return posix.Posix()


def mark() -> Mark:
    return current().mark()


def ended(status: int, since: Mark) -> Ending:
    return current().ended(status, since)


def memory_max(pid: int) -> int | None:
    """The lowest hard memory limit on process `pid` and the groups above it, in bytes; None
    when there is none, or the platform cannot say."""
    return current().memory_max(pid)


def memory_cap() -> Cap:
    """How a hard cap on one command's memory works on this platform."""
    return current().memory_cap()


def cap_properties(limit_bytes: int) -> list[str]:
    """The properties of a command's scope that cap its memory at `limit_bytes`; empty where
    the platform caps nothing (`memory_cap`)."""
    return current().cap_properties(limit_bytes)


def cap_ended(scope: str) -> bool | None:
    """Whether the OOM killer ended the capped command of `scope` once it has ended; None when
    the platform cannot say."""
    return current().cap_ended(scope)
