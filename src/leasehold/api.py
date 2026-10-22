"""The machine agent's HTTP/JSON API, as a function (`handle`) of (method, path, body, peer): no
sockets here, so any listener answers the same way.

`ROUTES` is every route: the model its body (or, for `GET /v1/events`, its query) is read into,
whether only an administrator may call it, and what answers it. `docs/reference.md` describes
each one. An administrator-only route checks who asks before it reads the body.

A well-formed request is answered 200 with a `status` field (the lend and pool routes answer
their state); a malformed one 400, one the peer may not make 403, an unknown path 404, one the
ledger cannot answer now 503, one that cannot be done now but may be later 409, and one the
daemon fails on 500, each with an `error` field (`bad_request`, `forbidden`, `not_found`,
`conflict`, `ledger_unavailable`, `internal`) and a `detail`. The transport adds 400
`bad_request` for a malformed `Content-Length` and 413 `too_large` for a body over its limit.
Every request is answered: no exception escapes `handle`, so a fault in the daemon never reads
to a client as no daemon at all.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leasehold.access import Forbidden, may_administer
from leasehold.agent import AcquireRequest, Conflict, MachineAgent
from leasehold.events import CAPACITY
from leasehold.ledger import LedgerUnavailable
from leasehold.outcomes import Outcome


@dataclass(frozen=True)
class Peer:
    """Who is asking: the uid and user name the transport vouches for (None if it cannot)."""

    uid: int | None = None
    user: str | None = None


class _LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=64)
    ttl_s: float | None = Field(None, gt=0, le=3600)


class _ReleaseRequest(_LeaseRequest):
    #: What the lease's command used (`leasehold.outcomes`), kept with the release.
    outcome: Outcome | None = None


class _ResizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lease_id: str = Field(min_length=1, max_length=64)
    weight: int = Field(ge=1)


class _ElasticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lease_id: str = Field(min_length=1, max_length=64)


class _EventsRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    since: int = Field(0, ge=0)
    limit: int = Field(CAPACITY, ge=1, le=CAPACITY)


class _LendRequest(BaseModel):
    # Strict: `"no"` or `0` is not a boolean, `true` or `"3"` not a number.
    model_config = ConfigDict(extra="forbid", strict=True)

    open: bool


class _PoolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    max: int | None


def _body(raw: bytes | None) -> dict:
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("the body is not a JSON object")
    return value


def _admin(agent: MachineAgent, peer: Peer) -> None:
    if not may_administer(peer.uid, peer.user, admin_group=agent.config.admin_group):
        group = agent.config.admin_group
        raise Forbidden(
            "only root, the daemon's user"
            + (f" or a member of group {group!r}" if group else "")
            + " may change this"
        )


#: What answers a route: (agent, the validated request or None, peer, the rest of the path after
#: a prefix route's stem, `""` for any other route) to the answer.
Handler = Callable[[MachineAgent, Any, Peer, str], dict]


@dataclass(frozen=True)
class Route:
    """One route. `request` is the model the body (with `query`, the query string) is
    validated into, or None to read neither. With `admin`, only an administrator may call it.
    With `prefix`, its path ends in a `<name>` part: it answers every path that starts with
    the part before that, and the handler is given the rest."""

    request: type[BaseModel] | None
    handler: Handler
    admin: bool = False
    query: bool = False
    prefix: bool = False


def _events(agent: MachineAgent, req: _EventsRequest, peer: Peer, rest: str) -> dict:
    return agent.events.since(req.since, req.limit)


def _acquire(agent: MachineAgent, req: AcquireRequest, peer: Peer, rest: str) -> dict:
    return agent.acquire_wait(req, user=peer.user)


def _renew(agent: MachineAgent, req: _LeaseRequest, peer: Peer, rest: str) -> dict:
    return agent.renew(req.lease_id, user=peer.user, ttl_s=req.ttl_s)


def _release(agent: MachineAgent, req: _ReleaseRequest, peer: Peer, rest: str) -> dict:
    return agent.release(req.lease_id, user=peer.user, outcome=req.outcome)


def _outcomes(agent: MachineAgent, req: None, peer: Peer, rest: str) -> dict:
    everyone = may_administer(peer.uid, peer.user, admin_group=agent.config.admin_group)
    return agent.outcomes(user=peer.user, everyone=everyone)


def _resize(agent: MachineAgent, req: _ResizeRequest, peer: Peer, rest: str) -> dict:
    return agent.resize(req.lease_id, req.weight, user=peer.user)


def _elastic(agent: MachineAgent, req: _ElasticRequest, peer: Peer, rest: str) -> dict:
    return agent.make_elastic(req.lease_id, user=peer.user)


def _lend(agent: MachineAgent, req: _LendRequest, peer: Peer, rest: str) -> dict:
    return agent.set_lend(req.open)


def _pool(agent: MachineAgent, req: _PoolRequest, peer: Peer, rest: str) -> dict:
    return agent.set_pool_max(rest, req.max)


#: Every route, by (method, path).
ROUTES: dict[tuple[str, str], Route] = {
    ("GET", "/v1/status"): Route(None, lambda agent, req, peer, rest: agent.status()),
    ("GET", "/v1/events"): Route(_EventsRequest, _events, query=True),
    ("GET", "/v1/leases"): Route(None, lambda agent, req, peer, rest: agent.leases()),
    ("GET", "/v1/commands"): Route(None, lambda agent, req, peer, rest: agent.commands()),
    ("POST", "/v1/acquire"): Route(AcquireRequest, _acquire),
    ("POST", "/v1/renew"): Route(_LeaseRequest, _renew),
    ("GET", "/v1/outcomes"): Route(None, _outcomes),
    ("POST", "/v1/release"): Route(_ReleaseRequest, _release),
    ("POST", "/v1/resize"): Route(_ResizeRequest, _resize),
    ("POST", "/v1/elastic"): Route(_ElasticRequest, _elastic),
    ("POST", "/v1/lend"): Route(_LendRequest, _lend, admin=True),
    ("POST", "/v1/pools/<name>"): Route(_PoolRequest, _pool, admin=True, prefix=True),
}


def _route(method: str, path: str) -> tuple[Route, str] | None:
    """The route for `method` and `path`, with the rest of the path after a prefix route's
    stem; None when there is none. A path matches a prefix route only through its stem."""
    route = ROUTES.get((method, path))
    if route is not None and not route.prefix:
        return route, ""
    for (m, pattern), route in ROUTES.items():
        stem = pattern.partition("<")[0]
        if route.prefix and m == method and path.startswith(stem):
            return route, path[len(stem) :]
    return None


def handle(
    agent: MachineAgent, method: str, path: str, raw: bytes | None, peer: Peer
) -> tuple[int, dict]:
    path, _, query = path.partition("?")
    path = path.rstrip("/")
    try:
        found = _route(method, path)
        if found is None:
            return 404, {"error": "not_found", "detail": f"{method} {path}"}
        route, rest = found
        if route.admin:
            _admin(agent, peer)  # who asks decides first, whatever the body
        req = None
        if route.request is not None:
            fields = dict(parse_qsl(query)) if route.query else _body(raw)
            req = route.request.model_validate(fields)
        return 200, route.handler(agent, req, peer, rest)
    except (ValidationError, ValueError) as exc:
        detail = exc.errors(include_url=False) if isinstance(exc, ValidationError) else str(exc)
        return 400, {"error": "bad_request", "detail": json.loads(json.dumps(detail, default=str))}
    except Forbidden as exc:
        return 403, {"error": "forbidden", "detail": str(exc)}
    except Conflict as exc:
        return 409, {"error": "conflict", "detail": str(exc)}
    except LedgerUnavailable as exc:
        return 503, {"error": exc.code, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 -- answered as a 500, its traceback on stderr
        sys.stderr.write(f"leasehold: {method} {path} failed:\n{traceback.format_exc()}")
        return 500, {"error": "internal", "detail": f"{type(exc).__name__}: {exc}"}
