"""Who holds a lease: a process, named by its pid *and* its birth token, so a reused pid is never
mistaken for the holder that died.

The birth token is the kernel's own start time, as a decimal string: on Linux field 22 of
`/proc/<pid>/stat` (`starttime`, clock ticks since boot). Elsewhere there is none:
`process_birth` is None and a holder is judged by its pid alone. Any program that writes the
same strings can share a ledger row's holder with leasehold.
"""

from __future__ import annotations

import os
from pathlib import Path


def _linux_stat(pid: int) -> tuple[str, int] | None:
    """(state, starttime) from procfs, without being confused by ')' in ``comm``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return str(fields[0]), int(fields[19])  # original fields 3 (state) and 22 (starttime)
    except (OSError, ValueError, IndexError):
        return None


def process_birth(pid: int | None = None) -> str | None:
    """The birth token of `pid` (this process by default), or None where the platform has none
    or the process does not exist."""
    pid = os.getpid() if pid is None else int(pid)
    stat = _linux_stat(pid)
    return str(stat[1]) if stat is not None else None


def process_matches(pid: int, birth: str) -> bool:
    """Whether a *live* process `pid` still has exactly this birth token. A Linux zombie is not
    live: it can hold nothing, and a parent that never reaps it must not pin a lease forever."""
    stat = _linux_stat(pid)
    if stat is None:
        return _hidden(pid)
    return stat[0] != "Z" and str(stat[1]) == birth


def _hidden(pid: int) -> bool:
    """Whether `pid` exists though `/proc` will not show it (mounted `hidepid`, which hides
    other users' processes from all but root and the mount's `gid=` group). The kernel still
    answers a signal-0 probe, so such a process is judged by its pid alone, as on a platform
    with no birth token: its birth cannot be checked, so a pid reused while hidden keeps the
    lease until that process ends too. `leasehold doctor` warns about such a `/proc`."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's: it exists
    return True


def pid_namespace() -> str | None:
    """This process's pid namespace (`/proc/self/ns/pid`, e.g. `pid:[4026531836]`), or None
    where there is none to read: a pid means something only inside its own namespace."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None
