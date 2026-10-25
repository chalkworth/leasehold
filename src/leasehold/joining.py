"""Joining an agent session to the daemon's watch (`leasehold.sessions`), for any harness.

`session()` is the Python API's: an orchestrator that runs agents, or any program, registers
itself as one session's agent for the length of a `with` block, and the processes it starts
carry the session's id in `LEASEHOLD_SESSION`, so that the daemon counts them, including any
left running on their own. `join` and `leave` are what `leasehold run`, the Claude Code hooks
and `leasehold session` use.

Watching is never in the way of the work: without a daemon, or with one from before sessions,
the block runs all the same, the variable is still set, and `Session.registered` is false with
the reason in `Session.reason`.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from leasehold import client as _client
from leasehold import discovery

ENV_SESSION = "LEASEHOLD_SESSION"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


@dataclass
class Session:
    """A session `session()` joined: its `id` (what `LEASEHOLD_SESSION` holds), whether the
    daemon watches it (`registered`), and why not (`reason`, None when it does)."""

    id: str
    registered: bool
    reason: str | None = None


def new_id(name: str | None = None) -> str:
    """A session id from `name` (default: this program's name) and a tag of its own."""
    base = _UNSAFE.sub("_", name or Path(sys.argv[0] if sys.argv else "").name or "python")
    return f"{base[:40]}_{uuid.uuid4().hex[:6]}"


def join(
    client: _client.Client,
    session: str,
    *,
    pid: int | None,
    source: str,
    agent: str | None = None,
    slice_: str | None = None,
) -> str | None:
    """Register `session`, whose agent is process `pid`, with the daemon `client` reaches. None
    when it is watched; else why not, in a few words."""
    try:
        client.register_session(session, pid=pid, agent=agent, slice_=slice_, source=source)
    except _client.Unavailable as exc:
        return f"no daemon: {exc}"
    except _client.ApiError as exc:
        if exc.status == 404 and exc.payload.get("error") == "not_found":
            return "the daemon is from before sessions"
        return f"the daemon said {exc.payload.get('error')}: {exc.payload.get('detail')}"
    return None


def leave(client: _client.Client, session: str) -> str | None:
    """Unregister `session`. None when it was watched until now; else why not."""
    try:
        client.unregister_session(session)
    except _client.Unavailable as exc:
        return f"no daemon: {exc}"
    except _client.ApiError as exc:
        return f"the daemon said {exc.payload.get('error')}: {exc.payload.get('detail')}"
    return None


def find_client(socket: str | os.PathLike | None = None, env=None) -> _client.Client | None:
    """A client for the daemon the `leasehold` command would use; None when none answers."""
    env = os.environ if env is None else env
    found = discovery.choose(
        discovery.candidates(None if socket is None else str(socket), None, env)
    )
    return None if found is None else _client.Client(found)


@contextmanager
def session(
    name: str | None = None,
    *,
    agent: str | None = None,
    socket: str | os.PathLike | None = None,
    client: _client.Client | None = None,
    env: MutableMapping[str, str] | None = None,
) -> Iterator[Session]:
    """Be one agent session for the `with` block: this process is the session's agent, and
    `LEASEHOLD_SESSION` in `env` (default `os.environ`, which the processes it starts inherit)
    names it. `name` is the session's id when it is a valid one, else the start of one;
    `agent` what to call the agent (default: this program). On leaving the block, the
    variable is put back as it was and the session is unregistered."""
    env = os.environ if env is None else env
    sid = name if name and _UNSAFE.search(name) is None and len(name) <= 64 else new_id(name)
    reach = client if client is not None else find_client(socket, env)
    why = "no daemon answers" if reach is None else None
    if reach is not None:
        why = join(reach, sid, pid=os.getpid(), source="sdk", agent=agent)
    before = env.get(ENV_SESSION)
    env[ENV_SESSION] = sid
    try:
        yield Session(sid, why is None, why)
    finally:
        if before is None:
            env.pop(ENV_SESSION, None)
        else:
            env[ENV_SESSION] = before
        if reach is not None and why is None:
            leave(reach, sid)
