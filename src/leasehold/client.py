"""A small client for the machine agent's unix-socket API (`leasehold.api`)."""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
from pathlib import Path
from urllib.parse import quote

from leasehold.units import memory_to_bytes


class Unavailable(ConnectionError):
    """The daemon could not be reached."""


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(f"{status}: {payload.get('error')}: {payload.get('detail')}")
        self.status = status
        self.payload = payload


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Client:
    def __init__(self, path: str | Path, *, timeout: float = 10.0) -> None:
        self.path = str(path)
        self.timeout = timeout

    def request(self, method: str, url: str, body: dict | None = None, *, extra_timeout=0.0):
        conn = _UnixConnection(self.path, self.timeout + extra_timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            headers = {"Content-Type": "application/json"} if data is not None else {}
            conn.request(method, url, body=data, headers=headers)
            resp = conn.getresponse()
            payload = json.loads(resp.read() or b"{}")
        except (OSError, http.client.HTTPException) as exc:
            raise Unavailable(f"cannot reach leasehold at {self.path}: {exc}") from exc
        finally:
            conn.close()
        if resp.status >= 400:
            raise ApiError(resp.status, payload)
        return payload

    def status(self) -> dict:
        return self.request("GET", "/v1/status")

    def events(self, since: int = 0) -> dict:
        """Events after `since` (`leasehold.events`): {`events`, `next`, `dropped`}."""
        return self.request("GET", f"/v1/events?since={int(since)}")

    def leases(self) -> dict:
        return self.request("GET", "/v1/leases")

    def commands(self) -> dict:
        """The `[commands]` rules (`leasehold.commands`): {`rules` (each with its `source`),
        `wait_s`, `when_busy`}."""
        return self.request("GET", "/v1/commands")

    def acquire(self, **body) -> dict:
        """Ask for a lease (`POST /v1/acquire`); fields that are None are left out. A daemon
        that does not take a newer field (`OPTIONAL`) is asked again without it: one older than
        0.3 takes no `memory`, one from before ranges no `max_weight` (it grants the `weight`) and
        no `memory_per_slot` (declared instead as `memory`, the `weight`'s worth, which is what
        such a daemon grants), and one from before elastic leases no `elastic` (its lease then
        keeps the size it is granted)."""
        body = {k: v for k, v in body.items() if v is not None}
        while True:
            try:
                return self.request(
                    "POST", "/v1/acquire", body, extra_timeout=body.get("wait_s", 0)
                )
            except ApiError as exc:
                unknown = _unknown_fields(exc)
                if not unknown or not unknown <= OPTIONAL or not unknown & body.keys():
                    raise
            body = _without(body, unknown)

    def renew(self, lease_id: str, ttl_s: float | None = None) -> dict:
        body = {"lease_id": lease_id} | ({} if ttl_s is None else {"ttl_s": ttl_s})
        return self.request("POST", "/v1/renew", body)

    def release(self, lease_id: str, outcome: dict | None = None) -> dict:
        """Release `lease_id` (`POST /v1/release`), sending `outcome`, what its command used
        (`leasehold.measure`), when given. A release with an outcome that the daemon answers
        with an error (one from before outcomes refuses the field) is sent again without it, so
        an outcome never keeps a lease from being released."""
        body = {"lease_id": lease_id}
        if outcome is not None:
            with contextlib.suppress(ApiError):
                return self.request("POST", "/v1/release", body | {"outcome": outcome})
        return self.request("POST", "/v1/release", body)

    def outcomes(self) -> dict:
        """What contained commands used (`GET /v1/outcomes`): {`groups`, one per rule and
        repository, and `outcomes`, newest first}; only the asking user's, unless an
        administrator asks."""
        return self.request("GET", "/v1/outcomes")

    def resize(self, lease_id: str, weight: int) -> dict:
        """Move an elastic lease to `weight` (`POST /v1/resize`): give slots back, or take the
        ones its target offers. An `ApiError` with a status under 500 means not now: the
        growth no longer fits, the lease is gone, or the daemon (one from before elastic
        leases) has no such route."""
        return self.request("POST", "/v1/resize", {"lease_id": lease_id, "weight": weight})

    def make_elastic(self, lease_id: str) -> dict:
        """Make a held range elastic from now on (`POST /v1/elastic`): its weight and target.
        An `ApiError` with a status under 500 means it cannot be: the lease has no range or is
        gone, or the daemon (one from before elastic leases) has no such route."""
        return self.request("POST", "/v1/elastic", {"lease_id": lease_id})

    def lend(self, open: bool) -> dict:
        return self.request("POST", "/v1/lend", {"open": open})

    def set_pool_max(self, pool: str, maximum: int | None) -> dict:
        """Override `pool`'s `max` until the daemon restarts; None resets it."""
        return self.request("POST", f"/v1/pools/{quote(pool, safe='')}", {"max": maximum})


#: The acquire fields a daemon older than this client may not take, and so may be left out.
OPTIONAL = frozenset({"memory", "max_weight", "memory_per_slot", "elastic"})


def _without(body: dict, unknown: set[str]) -> dict:
    """`body` without the fields in `unknown`; a `memory_per_slot` left out becomes the
    `memory` of the `weight` it is granted, where `memory` is a field still taken."""
    out = {k: v for k, v in body.items() if k not in unknown}
    per_slot = body.get("memory_per_slot")
    if "memory_per_slot" in unknown and per_slot is not None and "memory" not in unknown:
        out["memory"] = f"{int(body.get('weight', 1)) * memory_to_bytes(per_slot)}b"
    return out


def _unknown_fields(exc: ApiError) -> set[str]:
    """The fields the daemon refused a request for not knowing, when that is its only reason;
    an empty set otherwise."""
    detail = exc.payload.get("detail")
    if exc.status != 400 or not isinstance(detail, list) or not detail:
        return set()
    names = set()
    for e in detail:
        if not isinstance(e, dict) or e.get("type") != "extra_forbidden":
            return set()
        loc = e.get("loc")
        if not isinstance(loc, list) or len(loc) != 1:
            return set()
        names.add(loc[0])
    return names
