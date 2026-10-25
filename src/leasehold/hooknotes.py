"""The notes an agent hook keeps on the leases it takes: one file per lease, named by the lease,
in a directory only this user may enter.

A note holds the lease's renewer (pid and birth, `0 -` when none started), the agent the hook
runs under (pid, `0` when unknown), the program the lease is for, the job count and
`threads` setting the command is sized to, the weight granted, and the host's counters from
before the command started (`leasehold.host.Mark`): `pre` writes it once the lease is granted,
`post` reads the counters back, stops the renewer and deletes it. While a note's renewer lives, its
lease covers that program run under that agent: `covering` finds it for a PATH shim, which
then runs inside that lease rather than taking a second one for the same command, sized to it
(`sizing`, `weight`).

The daemon leaves notices there too, for an agent session its hooks registered: one file per
session (`notice.<session>`), a line per notice, appended (`notice`). The session's next hook
takes them (`notices`) and passes them on to the agent. The pre-filter tests for the file, so
a hook starts Python for them only while one is pending.
"""

from __future__ import annotations

import contextlib
import os
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path

from leasehold.jobs import count_of, is_share
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
    directory: Path,
    lease_id: str,
    renewer: int | None,
    agent: int | None,
    program: str = "-",
    *,
    jobs: int = 0,
    threads: int | str = "jobs",
    weight: int = 0,
    mark: str = "-",
) -> bool:
    """Note `lease_id`, its renewer, its agent, its program, the job count (0: none) and
    `threads` its command is sized to, the weight it was granted (0: not said), and the host's
    counters as one word (`leasehold.host.Mark.encode`). Whether it was noted."""
    birth = None if renewer is None else process_birth(renewer)
    line = f"{renewer or 0} {birth or '-'} {agent or 0} {program.split()[0] if program else '-'}"
    line += f" {max(0, jobs)} {threads} {max(0, weight)} {mark or '-'}"
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


def sizing(env: Mapping[str, str], lease_id: str) -> tuple[int, int | str] | None:
    """The job count and `threads` setting (a count, "jobs", or a share "jobs/N" or "jobs/N:K")
    the note for `lease_id` gives its command, or None when there is no such note, or it gives
    no job count (a note from before they were kept) or a setting it does not know."""
    directory = state_dir(env, create=False)
    if directory is None:
        return None
    try:
        fields = (directory / lease_id).read_text().split()
        jobs = int(fields[4])
        threads = fields[5]
    except (OSError, ValueError, IndexError):
        return None
    if jobs < 1:
        return None
    if threads == "jobs":
        return jobs, "jobs"
    if is_share(threads):
        return jobs, threads
    count = count_of(threads)
    return None if count is None else (jobs, count)


def weight(env: Mapping[str, str], lease_id: str) -> int | None:
    """The weight the note for `lease_id` says its lease was granted, or None when there is no
    such note or it does not say (a note from before it was kept)."""
    directory = state_dir(env, create=False)
    if directory is None:
        return None
    try:
        granted = int((directory / lease_id).read_text().split()[6])
    except (OSError, ValueError, IndexError):
        return None
    return granted if granted >= 1 else None


def noted(directory: Path | None, lease_id: str) -> tuple[str, str | None] | None:
    """The program the note for `lease_id` is for, and the host's counters it kept as `write`
    was given them (None: a note from before they were kept); None when there is no such
    note."""
    if directory is None:
        return None
    try:
        fields = (directory / lease_id).read_text().split()
    except OSError:
        return None
    if len(fields) < 4:
        return None
    return fields[3], fields[7] if len(fields) > 7 else None


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


def _live(env: Mapping[str, str], start: int, proc: Path) -> list[tuple[str, str]]:
    """(lease, program) for each note whose agent is `start` or above it and whose renewer
    still lives."""
    directory = state_dir(env, create=False)
    if directory is None:
        return []
    try:
        notes = [(p.name, _read(p)) for p in directory.iterdir() if p.is_file()]
    except OSError:
        return []
    above = set(ancestors(start, proc))
    return [
        (name, note[3])
        for name, note in notes
        if note is not None
        and note[2] > 0
        and note[2] in above
        and note[0] > 0
        and process_matches(note[0], note[1])
    ]


def covering(
    env: Mapping[str, str], program: str, start: int, proc: Path = Path("/proc")
) -> str | None:
    """The lease a hook holds for `program` run by an agent that `start` runs under, if any: a
    note for that program whose agent is `start` or above it and whose renewer still lives."""
    return next((name for name, noted in _live(env, start, proc) if noted == program), None)


def under_agent(env: Mapping[str, str], start: int, proc: Path = Path("/proc")) -> bool:
    """Whether `start` runs under an agent whose hook holds a lease now, for any program: a note
    whose agent is `start` or above it and whose renewer still lives."""
    return bool(_live(env, start, proc))


#: A session's notices file in the notes directory, by session.
NOTICE_PREFIX = "notice."


def notice(directory: Path, session: str, text: str) -> bool:
    """Leave `text` (one line) for the agent of `session`. Whether it was left."""
    line = " ".join(text.split())
    try:
        with open(directory / f"{NOTICE_PREFIX}{session}", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        return False
    return True


def notices(directory: Path | None, session: str | None) -> list[str]:
    """Take the notices left for `session`, oldest first: each is passed on once."""
    if directory is None or not session:
        return []
    found = directory / f"{NOTICE_PREFIX}{session}"
    taken = directory / f".taken.{session}.{os.getpid()}"
    try:
        found.rename(taken)
    except OSError:
        return []
    try:
        return [line for line in taken.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        return []
    finally:
        with contextlib.suppress(OSError):
            taken.unlink()
