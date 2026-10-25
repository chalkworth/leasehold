"""The agent sessions a daemon watches, and what each one costs the machine in memory.

A session joins by registering (`POST /v1/sessions`): `leasehold run`, the Claude Code plugin,
the Python API's `session()` and `leasehold session register` all do, and any other harness can
too. A user's own daemon also finds the sessions nobody registered: each of the user's processes
whose program is named in `[watchdog] agents` (`host.named`) is a session of its own,
`auto-<pid>`, unless it already belongs to one.

A session's processes are what `host.session_processes` finds: its slice's, under `leasehold
run`; else the agent's descendants and the processes whose environment names the session
(`LEASEHOLD_SESSION`). The agent process itself is never counted as the session's work;
everything else is, as **trees**: a process whose parent is the agent, or is not of the session
at all (one shell command, one MCP server, a server left running), with what it started. A
session's footprint is the sum of its trees' anonymous memory, resident or in swap
(`host.footprint`). File cache is left out, since the kernel takes it back first.

Watching only reads. Nothing here pauses or stops a process. A session found by name never
asked to join, and is only ever reported.

A registered session ends when it unregisters or when its agent process ends (fenced by its
start time, so a reused pid is not taken for it). A session found by name ends when its agent
does, or when a registered session takes its agent in. Where the host cannot list a session's
processes, each session says so, with the reason, and has no footprint: no footprint does not
mean an idle session.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from leasehold import host
from leasehold.access import Forbidden
from leasehold.host import Host, Proc, SessionRef, Tree

#: How a session joined: `run`, `hook` (an agent's hooks), `sdk` (the Python API), `api`
#: (anyone else, through the HTTP API or `leasehold session register`), or `auto` (found by name).
SOURCES = ("run", "hook", "sdk", "api", "auto")
#: The id a session found by name has: this and its agent's pid.
AUTO_PREFIX = "auto-"


class UnknownSession(LookupError):
    """No session has this id."""


@dataclass(frozen=True)
class Reading:
    """What a session's processes were when last read. `how` is `polled` (each process read in
    turn) or `none`, and then `reason` says why. `footprint` is its trees' anonymous memory,
    resident or in swap, `processes` how many it has besides the agent, `trees` how many trees
    they make, and `largest` the process with the most anonymous memory."""

    how: str
    reason: str | None = None
    footprint: int | None = None
    processes: int | None = None
    trees: int | None = None
    largest: Proc | None = None
    #: Every pid read as the session's, the agent's included.
    pids: frozenset[int] = frozenset()
    #: Whether the agent process was among them (None: it cannot be told here).
    agent_alive: bool | None = None


@dataclass
class Session:
    id: str
    source: str
    since: float
    user: str | None = None
    agent_pid: int | None = None
    agent_fence: str | None = None
    agent: str | None = None
    slice_: str | None = None
    reading: Reading | None = None

    def ref(self) -> SessionRef:
        return SessionRef(self.id, self.agent_pid, self.agent_fence, self.slice_)

    def public(self) -> dict:
        r = self.reading
        largest = None if r is None else r.largest
        return {
            "session": self.id,
            "source": self.source,
            "user": self.user,
            "agent": self.agent,
            "agent_pid": self.agent_pid,
            "slice": self.slice_,
            "since": self.since,
            "how": None if r is None else r.how,
            "reason": None if r is None else r.reason,
            "footprint_bytes": None if r is None else r.footprint,
            "processes": None if r is None else r.processes,
            "trees": None if r is None else r.trees,
            "largest": None
            if largest is None
            else {"pid": largest.pid, "name": largest.name, "bytes": _anon(largest)},
        }


def _anon(proc: Proc) -> int:
    return (proc.anon or 0) + (proc.swap or 0)


def _start(proc: Proc) -> tuple[int, int]:
    """An order oldest first: by the platform's start token where it is a number, then pid."""
    fence = proc.fence or ""
    return (int(fence) if fence.isdigit() else 0, proc.pid)


@dataclass
class Ended:
    """A session that ended at the last `refresh`, and why: `agent ended`, `unregistered` or
    `joined` (a registered session took its agent in)."""

    session: Session
    reason: str


@dataclass
class Changes:
    started: list[Session] = field(default_factory=list)
    ended: list[Ended] = field(default_factory=list)


class Sessions:
    """The sessions watched. Thread-safe: requests register and unregister while the daemon's
    tick refreshes. `agents` are the program names found without registering (empty: none are),
    and `owner` the user a session found so is filed under; `platform` makes the host."""

    def __init__(
        self,
        agents: list[str] | tuple[str, ...] = (),
        *,
        owner: str | None = None,
        platform: Callable[[], Host] = host.current,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.agents = tuple(agents)
        self.owner = owner
        self._platform = platform
        self._host: Host | None = None
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    @property
    def host(self) -> Host:
        if self._host is None:
            self._host = self._platform()
        return self._host

    def _can_list(self) -> host.Support:
        return self.host.support("session_processes")

    # --- joining and leaving -----------------------------------------------------------------

    def register(
        self,
        session_id: str,
        *,
        source: str,
        user: str | None,
        agent_pid: int | None = None,
        agent: str | None = None,
        slice_: str | None = None,
    ) -> dict:
        """Watch `session_id`, whose agent is process `agent_pid` (None: it has none, and the
        session lasts until it unregisters). Registering an id again replaces it. Raises
        `Forbidden` for another user's session and `ValueError` for an agent process
        that is not running."""
        if source not in SOURCES or source == "auto":
            raise ValueError(f"a session cannot register as {source!r}")
        fence = None
        if agent_pid is not None:
            found = self._process(agent_pid)
            if found is None and self.host.support("members").how != "none":
                raise ValueError(f"no process {agent_pid} is running")
            if found is not None:
                fence = found.fence
                agent = agent or found.name
        with self._lock:
            old = self._sessions.get(session_id)
            if old is not None and old.user != user and user != "root":
                raise Forbidden(f"session {session_id} is another user's")
            for key, other in list(self._sessions.items()):
                if other.source == "auto" and other.agent_pid == agent_pid:
                    del self._sessions[key]
            new = Session(
                session_id,
                source,
                self._clock() if old is None else old.since,
                user=user,
                agent_pid=agent_pid,
                agent_fence=fence,
                agent=agent,
                slice_=slice_,
            )
            self._sessions[session_id] = new
        new.reading = self._read(new)
        return new.public()

    def unregister(self, session_id: str, *, user: str | None) -> dict:
        """Stop watching `session_id`. Raises `UnknownSession` when no session has that id, and
        `Forbidden` for another user's."""
        with self._lock:
            found = self._sessions.get(session_id)
            if found is None:
                raise UnknownSession(f"no session {session_id} is watched")
            if found.user != user and user != "root":
                raise Forbidden(f"session {session_id} is another user's")
            del self._sessions[session_id]
        return found.public()

    def _process(self, pid: int) -> Proc | None:
        for proc in self.host.members(Tree(root=pid)):
            if proc.pid == pid:
                return proc
        return None

    # --- watching ----------------------------------------------------------------------------

    def _read(self, session: Session) -> Reading:
        can = self._can_list()
        if can.how == "none":
            alive = None
            if session.agent_pid is not None:
                alive = _running(session.agent_pid)
            return Reading("none", can.reason, agent_alive=alive)
        procs = self.host.session_processes(session.ref())
        pids = frozenset(p.pid for p in procs)
        alive = None if session.agent_pid is None else session.agent_pid in pids
        work = [p for p in procs if p.role != "agent"]
        roots = [p for p in work if p.parent == session.agent_pid or p.parent not in pids]
        total = 0
        for root in roots:
            got = self.host.footprint(Tree(root=root.pid, fence=root.fence))
            total += (got.anon or 0) + (got.swap or 0)
        largest = max(work, key=_anon, default=None)
        return Reading(
            "polled",
            footprint=total,
            processes=len(work),
            trees=len(roots),
            largest=largest,
            pids=pids,
            agent_alive=alive,
        )

    def refresh(self) -> Changes:
        """Read every session's processes again, drop those whose agent has ended, and watch
        each agent process found by name that no session has yet."""
        changes = Changes()
        with self._lock:
            watched = list(self._sessions.values())
        readings = {s.id: self._read(s) for s in watched}
        claimed: set[int] = set()
        for s in watched:
            if s.source != "auto":
                claimed |= readings[s.id].pids
        with self._lock:
            for s in watched:
                if self._sessions.get(s.id) is not s:
                    continue  # it unregistered, or registered again, meanwhile
                reading = readings[s.id]
                if reading.agent_alive is False:
                    del self._sessions[s.id]
                    changes.ended.append(Ended(s, "agent ended"))
                elif s.source == "auto" and s.agent_pid in claimed:
                    del self._sessions[s.id]
                    changes.ended.append(Ended(s, "joined"))
                else:
                    s.reading = reading
                    claimed |= reading.pids
        if self.agents and self._can_list().how != "none":
            changes.started += self._discover(claimed)
        return changes

    def _discover(self, claimed: set[int]) -> list[Session]:
        """Each process named in `agents` that no session has, oldest first, as a session of
        its own; the processes it has are claimed, so an agent another agent started is not."""
        found = []
        me = os.getpid()
        for proc in sorted(self.host.named(self.agents), key=_start):
            if proc.pid in claimed or proc.pid == me:
                continue
            new = Session(
                f"{AUTO_PREFIX}{proc.pid}",
                "auto",
                self._clock(),
                user=self.owner,
                agent_pid=proc.pid,
                agent_fence=proc.fence,
                agent=proc.name,
            )
            new.reading = self._read(new)
            if new.reading.agent_alive is False:
                continue
            claimed |= new.reading.pids
            with self._lock:
                if new.id in self._sessions:
                    continue
                self._sessions[new.id] = new
            found.append(new)
        return found

    def status(self) -> dict:
        """What `watchdog` in the status object holds: whether a session's processes can be
        found here (`processes`: `how`, `reason`), and each session, oldest first."""
        can = self._can_list()
        with self._lock:
            listed = sorted(self._sessions.values(), key=lambda s: (s.since, s.id))
            sessions = [s.public() for s in listed]
        return {
            "processes": {"how": "polled" if can.how != "none" else "none", "reason": can.reason},
            "discover": list(self.agents),
            "sessions": sessions,
        }


def _running(pid: int) -> bool:
    """Whether process `pid` exists, for a host that cannot list processes (one of another
    user's counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
