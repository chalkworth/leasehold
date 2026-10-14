"""The notes an agent hook keeps on the leases it takes: one file per lease, named by the lease,
in a directory only this user may enter.

A note holds the lease's renewer (pid and birth, `0 -` when none started), the agent the hook
runs under (pid, `0` when unknown) and the program the lease is for: `pre` writes it once the
lease is granted, `post` stops the renewer and deletes it. While a note's renewer lives, its
lease covers that program run under that agent: `covering` finds it for a PATH shim, which then
runs inside that lease rather than taking a second one for the same command.
"""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path

from leasehold.process import process_birth, process_matches


def state_dir(env: Mapping[str, str], *, create: bool = True) -> Path | None:
    """The notes directory: one only this user may enter, or None when one cannot be had (a hook
    then takes no lease, and a shim finds no cover)."""
    base = env.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    directory = Path(base) / f"leasehold-hooks-{os.getuid()}"
    try:
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        st = directory.lstat()
    except OSError:
        return None
    if st.st_uid != os.getuid() or st.st_mode & 0o077 or not directory.is_dir():
        return None  # not ours alone: never trust what is in it
    return directory


def write(
    directory: Path, lease_id: str, renewer: int | None, agent: int | None, program: str = "-"
) -> bool:
    """Note `lease_id`, its renewer, its agent and its program. Whether it was noted."""
    birth = None if renewer is None else process_birth(renewer)
    line = f"{renewer or 0} {birth or '-'} {agent or 0} {program.split()[0] if program else '-'}"
    try:
        (directory / lease_id).write_text(line + "\n")
    except OSError:
        return False
    return True


def _read(note: Path) -> tuple[int, str, int, str] | None:
    """(renewer, its birth, agent, program) from a note."""
    try:
        fields = [*note.read_text().split(), "0", "-"]
        return int(fields[0]), fields[1], int(fields[2]), fields[3]
    except (OSError, ValueError, IndexError):
        return None


def stop(directory: Path | None, lease_id: str) -> None:
    """Stop the lease's renewer (never a process that took over its pid) and delete its note."""
    if directory is None:
        return
    note = directory / lease_id
    found = _read(note)
    if found is not None and found[0] > 0 and process_matches(found[0], found[1]):
        with contextlib.suppress(OSError):
            os.kill(found[0], signal.SIGTERM)
    with contextlib.suppress(OSError):
        note.unlink()


def _parent(pid: int, proc: Path) -> int | None:
    try:
        raw = (proc / str(pid) / "stat").read_text(encoding="ascii")
        return int(raw[raw.rfind(")") + 2 :].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def ancestors(start: int, proc: Path = Path("/proc")) -> list[int]:
    """`start` and the processes above it, nearest first."""
    out = []
    pid: int | None = start
    while pid and pid > 1 and len(out) < 64:
        out.append(pid)
        pid = _parent(pid, proc)
    return out


def covering(
    env: Mapping[str, str], program: str, start: int, proc: Path = Path("/proc")
) -> str | None:
    """The lease a hook holds for `program` run by an agent that `start` runs under, if any: a
    note for that program whose agent is `start` or above it and whose renewer still lives."""
    directory = state_dir(env, create=False)
    if directory is None:
        return None
    try:
        notes = [(p.name, _read(p)) for p in directory.iterdir() if p.is_file()]
    except OSError:
        return None
    mine = [(name, n) for name, n in notes if n is not None and n[2] > 0 and n[3] == program]
    if not mine:
        return None
    above = set(ancestors(start, proc))
    for name, (renewer, birth, agent, _) in mine:
        if agent in above and renewer > 0 and process_matches(renewer, birth):
            return name
    return None
