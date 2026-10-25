"""Containment for a command an agent starts itself, such as a Claude Code Bash command: the
agent's hook takes the lease, but it can neither start the command nor change it, so the
command's shell is put in the lease's scope as it starts.

1. The hook's `pre` offers the lease (`offer`): a note under `pending/` in the hook's notes
   directory (`leasehold.hooknotes`) with the command's text, the agent's pid, and the sizes of
   the lease's scope.
2. The agent sources the lines `env_lines` gives in each command's shell, before the command.
   They test for one directory. While a note is pending they run `python -m leasehold.attach
   $$`, which claims the note for the command this shell runs (`claim`), under that agent, and
   puts the shell in the lease's scope (`leasehold.host.adopt`) before the command starts, so
   that everything the command starts is in it too. It prints nothing, and exits 0. Any agent
   whose shell sources the lines works this way; Claude Code sources them from the session's
   `CLAUDE_ENV_FILE`.
3. When no shell claims a note, the lease's renewer adopts by scan (`Watcher`): it looks among
   the agent's descendants for the shell that runs the command, and adopts it and what it has
   started. That is racy: what the command allocated before the scan found it is not counted,
   so `collect` says it was adopted by `scan`, and in a session whose shells source the lines
   the renewer first gives the shell `SCAN_GRACE_S` to claim the note itself. A renewer that
   stops withdraws an offer no shell claimed.
4. Once the shell is adopted, a holder process (`sleep`) keeps the scope until `post`, so that
   its counters can still be read after the command ends. The renewer measures the scope
   (`leasehold.measure`) and, once stopped, writes what it used beside the note
   (`Watcher.finish`). `post` reads that (`collect`) and stops the holder.

A note whose agent has ended is deleted by the next `claim` that sees it, so an offer left by an
agent that died does not keep every later shell starting Python.

A shell that runs under a hard memory limit someone else set (Claude Code's
`CLAUDE_CODE_TOOL_MEMORY_LIMIT`, say) is never moved from under it: the lease is kept, and the
record says why there is no scope. Where the platform cannot adopt (the host's `support` for
`adopt`, `leasehold.host`), `env_lines` gives no lines, and a lease counts the command without
containing it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leasehold import contain, hooknotes, host, measure
from leasehold.process import process_birth, process_matches

#: The directory, in the notes directory, that holds the leases offered and not yet claimed.
PENDING = "pending"
#: How long the renewer leaves a found shell to claim its note itself before it adopts it.
SCAN_GRACE_S = 1.0
#: How often the renewer looks for the command's shell.
SCAN_PERIOD_S = 0.1
#: How long `collect` waits for the renewer's report once it has been told to stop.
REPORT_WAIT_S = 3.0
#: A file in the notes directory that says a session's shells source `env_lines`.
_SESSION_MARK = "sourced-"


def _write(path: Path, data: Mapping) -> None:
    """Write `data` as JSON to `path` whole: a reader sees the old file or the new one."""
    temp = path.with_name(f".{path.name}.{os.getpid()}")
    temp.write_text(json.dumps(data))
    os.replace(temp, path)


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def env_lines(
    env: Mapping[str, str], session: str | None, python: str = sys.executable
) -> tuple[list[str], str | None]:
    """The shell lines an agent sources before each command, and None; or no lines and why,
    where nothing can be adopted here. A command with no lease offered costs one `[ -d ]`.
    Marks `session` as one whose shells source them (`offer`)."""
    found = host.current().support("adopt")
    if found.how == "none":
        return [], found.reason
    directory = hooknotes.state_dir(env)
    if directory is None:
        return [], "no private directory for the hook's notes"
    if session:
        with contextlib.suppress(OSError):
            (directory / f"{_SESSION_MARK}{session}").touch()
    pending = shlex.quote(str(directory / PENDING))
    run = f'{shlex.quote(python)} -I -m leasehold.attach "$$" </dev/null >/dev/null 2>&1'
    return [
        "# leasehold: put a leased command's shell in its lease's scope before it runs",
        f"[ ! -d {pending} ] || {run} || :",
    ], None


def offer(
    directory: Path,
    lease_id: str,
    *,
    command: str,
    agent: int | None,
    session: str | None,
    program: str,
    weight: int,
    memory: str | None = None,
    cap_bytes: int | None = None,
    hard_cpu: bool = False,
    max_hold_s: float = 660.0,
    rule: str | None = None,
    repo: str | None = None,
) -> bool:
    """Offer `lease_id` to the shell that will run `command` under `agent`: a scope sized to
    `weight` slots, `memory` (soft) and `cap_bytes` (hard), kept at most `max_hold_s`. Whether
    it was offered."""
    note = {
        "command": command,
        "agent": agent,
        "program": program,
        "weight": weight,
        "memory": memory,
        "cap_bytes": cap_bytes,
        "hard_cpu": hard_cpu,
        "max_hold_s": max_hold_s,
        "rule": rule,
        "repo": repo,
        "sourced": bool(session) and (directory / f"{_SESSION_MARK}{session}").exists(),
    }
    pending = directory / PENDING
    for _ in range(3):
        try:
            pending.mkdir(mode=0o700, exist_ok=True)
            _write(pending / lease_id, note)
            return True
        except FileNotFoundError:
            continue  # a claim removed the directory as it emptied
        except OSError:
            return False
    return False


def _args(pid: int, proc: Path) -> list[str]:
    """The arguments process `pid` was started with, its program left out."""
    try:
        raw = (proc / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0")[1:] if a]


def _evaluated(text: str) -> list[str]:
    """The word after each `eval ` in the shell code `text`, its quotes taken off as the shell
    would take them off."""
    words, start = [], 0
    while (at := text.find("eval ", start)) >= 0:
        start = at + 5
        lexer = shlex.shlex(text[start:], posix=True)
        lexer.whitespace_split = True
        with contextlib.suppress(ValueError):
            word = lexer.get_token()
            if word:
                words.append(word)
    return words


def runs(command: str, args: Sequence[str]) -> bool:
    """Whether a shell started with `args` runs `command`: one argument is the command, or is
    shell code that runs it with `eval`, quoted in any way the shell takes (Claude Code runs
    `eval '...'`)."""
    return any(arg == command or command in _evaluated(arg) for arg in args)


def claim(directory: Path, pid: int, proc: Path = Path("/proc")) -> tuple[str, dict] | None:
    """Claim the note offered for the command the shell `pid` runs, under the agent above it:
    (lease id, note), or None when there is none. Of two processes that claim one note, one
    gets it."""
    pending = directory / PENDING
    try:
        names = sorted(n for n in os.listdir(pending) if not n.startswith("."))
    except OSError:
        return None
    args = _args(pid, proc)
    if not args:
        return None
    above = set(hooknotes.ancestors(pid, proc))
    for name in names:
        note = _load(pending / name)
        if note is None or not isinstance(note.get("command"), str):
            continue
        if note.get("agent") and note["agent"] not in above:
            if not _alive(note["agent"]):  # its agent is gone, and its `post` with it
                with contextlib.suppress(OSError):
                    (pending / name).unlink()
            continue
        if not runs(note["command"], args):
            continue
        try:
            os.rename(pending / name, directory / f"{name}.attach")
        except OSError:
            continue  # claimed by another
        with contextlib.suppress(OSError):
            pending.rmdir()
        return name, note
    return None


def _alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


def _holder(max_hold_s: float) -> subprocess.Popen | None:
    """A process that does nothing for `max_hold_s`, to keep a scope until `post` reads it."""
    try:
        return subprocess.Popen(
            ["sleep", f"{max(1.0, max_hold_s):.0f}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return None


def adopt(directory: Path, lease_id: str, note: Mapping, pids: Sequence[int], how: str) -> dict:
    """Put `pids` in the scope of the claimed `lease_id`, sized as `note` says, with a holder;
    record what was done beside the note (`how`: `shell` or `scan`, or None when nothing was
    adopted, with the `reason`). The record."""
    holder = _holder(float(note.get("max_hold_s") or 660.0))
    spec = host.TreeSpec(
        unit=contain.lease_unit(lease_id),
        slice_=contain.lease_slice(),
        weight=int(note.get("weight") or 1),
        memory=note.get("memory"),
        cap_bytes=note.get("cap_bytes"),
        hard_cpu=bool(note.get("hard_cpu")),
    )
    moved = [*pids, holder.pid] if holder is not None else list(pids)
    found = host.current().adopt(moved, spec)
    if found.how == "none" and holder is not None:
        holder.kill()
        holder.wait()
        holder = None
    record = dict(note) | {
        "attached": how if found.how != "none" else None,
        "reason": found.reason,
        "unit": found.unit,
        "tree": None if found.tree is None else found.tree.to_dict(),
        "limit_bytes": found.limit_bytes,
        "capped": found.capped,
        "pids": list(pids),
        "births": [process_birth(p) for p in pids],
        "holder": None if holder is None else [holder.pid, process_birth(holder.pid)],
        "since": time.time(),
    }
    _write(directory / f"{lease_id}.attach", record)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m leasehold.attach PID`: claim the note for the command shell `PID` runs, and
    adopt the shell. Silent, and always 0: the command runs whatever happens here."""
    args = sys.argv[1:] if argv is None else list(argv)
    with contextlib.suppress(Exception):
        pid = int(args[0])
        directory = hooknotes.state_dir(os.environ, create=False)
        claimed = None if directory is None else claim(directory, pid)
        if claimed is not None:
            adopt(directory, *claimed, [pid], "shell")
    return 0


def _children(pid: int, proc: Path) -> list[int]:
    out = []
    try:
        tasks = list((proc / str(pid) / "task").iterdir())
    except OSError:
        return out
    for task in tasks:
        with contextlib.suppress(OSError, ValueError):
            out += [int(w) for w in (task / "children").read_text().split()]
    return out


def _descendants(pid: int, proc: Path, depth: int = 64) -> list[int]:
    """`pid`'s descendants, nearest first."""
    found, level = [], [pid]
    for _ in range(depth):
        level = [c for p in level for c in _children(p, proc)]
        if not level:
            break
        found += level
    return found


class Watcher:
    """Run by the renewer of a lease `offer`ed to an agent's command (`leasehold keep
    --attach`): adopts the command's shell by scan when it does not claim its note itself
    (under `agent`, after `grace_s` when the session's shells source `env_lines`), measures the
    scope once the shell is in it, and on `finish` writes what it used for `collect`."""

    def __init__(
        self,
        lease_id: str,
        env: Mapping[str, str],
        agent: int | None,
        *,
        proc: Path = Path("/proc"),
        grace_s: float = SCAN_GRACE_S,
    ) -> None:
        self._lease_id = lease_id
        self._directory = hooknotes.state_dir(env, create=False)
        self._agent = agent
        self._proc = proc
        self._host = host.current(proc=proc)
        self._grace_s = grace_s
        self._meter: measure.Meter | None = None
        self._holder: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="leasehold-attach", daemon=True)
        if self._directory is not None:
            self._thread.start()

    def _run(self) -> None:
        directory, lid = self._directory, self._lease_id
        seen: dict[int, float] = {}
        while not self._stop.is_set():
            record = _load(directory / f"{lid}.attach")
            if record is not None:
                if "attached" in record:  # claimed and done with
                    if record["attached"]:
                        self._measure(record)
                    return
            else:
                note = _load(directory / PENDING / lid)
                if note is None:
                    return  # withdrawn
                self._scan(note, seen)
            self._stop.wait(SCAN_PERIOD_S)

    def _scan(self, note: dict, seen: dict[int, float]) -> None:
        if not self._agent or not isinstance(note.get("command"), str):
            return
        now = time.monotonic()
        for pid in _descendants(self._agent, self._proc):
            if not runs(note["command"], _args(pid, self._proc)):
                continue
            first = seen.setdefault(pid, now)
            if note.get("sourced") and now - first < self._grace_s:
                return
            pending = self._directory / PENDING / self._lease_id
            try:
                os.rename(pending, self._directory / f"{self._lease_id}.attach")
            except OSError:
                return  # the shell claimed it
            with contextlib.suppress(OSError):
                pending.parent.rmdir()
            tree = [pid, *_descendants(pid, self._proc)]
            record = adopt(self._directory, self._lease_id, note, tree, "scan")
            if record["attached"] and record.get("tree"):
                # What the command started while it was being moved, moved after it.
                later = [p for p in _descendants(pid, self._proc) if p not in tree]
                if later:
                    self._host.join(host.Tree.from_dict(record["tree"]), later)
            return

    def _measure(self, record: dict) -> None:
        holder = record.get("holder")
        self._holder = holder[0] if holder else None
        shell, birth = record["pids"][0], (record.get("births") or [None])[0]
        tree = host.Tree.from_dict(record.get("tree") or {"root": shell})
        self._meter = measure.Meter(
            self._host,
            tree,
            running=lambda: birth is not None and process_matches(shell, birth),
        )

    def finish(self) -> None:
        """Stop: withdraw the offer if no shell has claimed it, and write what the scope used
        beside the note (nothing when no shell was adopted)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._directory is None:
            return
        pending = self._directory / PENDING / self._lease_id
        with contextlib.suppress(OSError):
            pending.unlink()  # unclaimed now, the lease is gone: no shell may take it
            pending.parent.rmdir()
        if self._meter is None:
            record = _load(self._directory / f"{self._lease_id}.attach") or {}
            if not record.get("attached"):
                return
            self._measure(record)  # adopted since the thread last looked: one read, now
        wall_s = self._meter.stop()
        left = [m for m in self._meter.left() if m[0] != self._holder]
        report = {
            "usage": dataclasses.asdict(self._meter.usage),
            "largest": self._meter.largest,
            "wall_s": round(wall_s, 3),
            "left": left[0] if left else None,
        }
        with contextlib.suppress(OSError):
            _write(self._directory / f"{self._lease_id}.used", report)


@dataclass(frozen=True)
class Collected:
    """What `collect` found for a lease offered to a command. `attached` is how its shell was
    adopted (`shell` or `scan`), or None with the `reason`. For an adopted one: its scope
    (`unit`) and its `tree`, the hard memory limit above it (`limit_bytes`), its own cap
    (`cap_bytes` when `capped`), and what it used (`usage`, None when the renewer did not
    report), how long it ran (`wall_s`), its largest process (`largest`) and what it left
    running (`left`), each as (pid, name, bytes)."""

    attached: str | None
    reason: str | None = None
    unit: str | None = None
    tree: host.Tree | None = None
    limit_bytes: int | None = None
    cap_bytes: int | None = None
    capped: bool = False
    usage: measure.Usage | None = None
    wall_s: float | None = None
    largest: tuple[int, str, int] | None = None
    left: tuple[int, str, int] | None = None
    rule: str | None = None
    repo: str | None = None


def _forget(directory: Path, lease_id: str) -> None:
    for path in (
        directory / PENDING / lease_id,
        directory / f"{lease_id}.attach",
        directory / f"{lease_id}.used",
    ):
        with contextlib.suppress(OSError):
            path.unlink()
    with contextlib.suppress(OSError):
        (directory / PENDING).rmdir()


def collect(
    directory: Path | None, lease_id: str, *, wait_s: float = REPORT_WAIT_S
) -> Collected | None:
    """Once the command has ended and its renewer has been told to stop: what was done for
    `lease_id`, after waiting up to `wait_s` for the renewer's report; the holder is stopped
    and the notes deleted. None when no shell claimed the offer, or none was made."""
    if directory is None:
        return None
    record = _load(directory / f"{lease_id}.attach")
    if record is None or "attached" not in record:
        _forget(directory, lease_id)
        return None
    if not record["attached"]:
        _forget(directory, lease_id)
        return Collected(None, reason=record.get("reason"))
    used = directory / f"{lease_id}.used"
    deadline = time.monotonic() + wait_s
    while not used.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    report = _load(used) or {}
    holder = record.get("holder")
    if holder and holder[1] is not None and process_matches(holder[0], holder[1]):
        with contextlib.suppress(OSError):
            os.kill(holder[0], signal.SIGKILL)
    _forget(directory, lease_id)
    usage = report.get("usage")
    fields = {f.name for f in dataclasses.fields(measure.Usage)}
    return Collected(
        record["attached"],
        unit=record.get("unit"),
        tree=host.Tree.from_dict(record["tree"]) if record.get("tree") else None,
        limit_bytes=record.get("limit_bytes"),
        cap_bytes=record.get("cap_bytes"),
        capped=bool(record.get("capped")),
        usage=None
        if not isinstance(usage, dict)
        else measure.Usage(**{k: v for k, v in usage.items() if k in fields}),
        wall_s=report.get("wall_s"),
        largest=tuple(report["largest"]) if report.get("largest") else None,
        left=tuple(report["left"]) if report.get("left") else None,
        rule=record.get("rule"),
        repo=record.get("repo"),
    )


if __name__ == "__main__":
    sys.exit(main())
