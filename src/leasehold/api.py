"""The machine agent's HTTP/JSON API, as a function of (method, path, body, peer): no sockets here,
so any listener answers the same way.

* `GET /v1/status`: the machine, as `leasehold.status` builds it.
* `GET /v1/leases`: `{"leases": [...], "queue": [...]}`.
* `GET /v1/events?since=N&limit=M`: what happened after event `N` (`leasehold.events`).
* `POST /v1/acquire` {`kind`, `pool`, `weight`, `labels`, `lease_id`, `session`, `wait_s`,
  `ttl_s`}: `granted` (with `lease`), `queued` (with `position` and `blocked`) or `refused`.
* `POST /v1/renew` {`lease_id`, `ttl_s`}: `ok`, `revoked`, `queued` or `unknown`, with
  `expires_at`.
* `POST /v1/release` {`lease_id`}: `released`.
* `POST /v1/lend` {`open`: bool}, administrators only: the lend state.
* `POST /v1/pools/<name>` {`max`: int, or null to reset}, administrators only: the pool's `max`.

A well-formed request is answered 200 with a `status` field (the lend and pool routes answer
their state); a malformed one 400, one the peer may not make 403, an unknown path 404, and one
the ledger cannot answer now 503, each with an `error` field (`bad_request`, `forbidden`,
`not_found`, `ledger_unavailable`) and a `detail`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from leasehold.access import Forbidden, may_administer
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.events import CAPACITY
from leasehold.ledger import LedgerUnavailable

_POOLS = "/v1/pools/"


@dataclass(frozen=True)
class Peer:
    """Who is asking: the uid and user name the transport vouches for (None if it cannot)."""

    uid: int | None = None
    user: str | None = None


class _LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=1, max_length=64)
    ttl_s: float | None = Field(None, gt=0, le=3600)


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


def handle(
    agent: MachineAgent, method: str, path: str, raw: bytes | None, peer: Peer
) -> tuple[int, dict]:
    path, _, query = path.partition("?")
    path = path.rstrip("/")
    try:
        if method == "GET" and path == "/v1/status":
            return 200, agent.status()
        if method == "GET" and path == "/v1/events":
            req = _EventsRequest.model_validate(dict(parse_qsl(query)))
            return 200, agent.events.since(req.since, req.limit)
        if method == "GET" and path == "/v1/leases":
            return 200, agent.leases()
        if method == "GET" and path == "/v1/commands":
            return 200, agent.commands()
        if method == "POST" and path == "/v1/acquire":
            req = AcquireRequest.model_validate(_body(raw))
            return 200, agent.acquire_wait(req, user=peer.user)
        if method == "POST" and path == "/v1/renew":
            req = _LeaseRequest.model_validate(_body(raw))
            return 200, agent.renew(req.lease_id, user=peer.user, ttl_s=req.ttl_s)
        if method == "POST" and path == "/v1/release":
            req = _LeaseRequest.model_validate(_body(raw))
            return 200, agent.release(req.lease_id, user=peer.user)
        if method == "POST" and path == "/v1/lend":
            _admin(agent, peer)  # who asks decides first, whatever the body
            req = _LendRequest.model_validate(_body(raw))
            return 200, agent.set_lend(req.open)
        if method == "POST" and path.startswith(_POOLS):
            _admin(agent, peer)
            req = _PoolRequest.model_validate(_body(raw))
            return 200, agent.set_pool_max(path[len(_POOLS) :], req.max)
    except (ValidationError, ValueError) as exc:
        detail = exc.errors(include_url=False) if isinstance(exc, ValidationError) else str(exc)
        return 400, {"error": "bad_request", "detail": json.loads(json.dumps(detail, default=str))}
    except Forbidden as exc:
        return 403, {"error": "forbidden", "detail": str(exc)}
    except LedgerUnavailable as exc:
        return 503, {"error": exc.code, "detail": str(exc)}
    return 404, {"error": "not_found", "detail": f"{method} {path}"}
