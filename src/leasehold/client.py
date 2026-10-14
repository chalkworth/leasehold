"""A small client for the machine agent's unix-socket API (`leasehold.api`)."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from urllib.parse import quote


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
        body = {k: v for k, v in body.items() if v is not None}
        return self.request("POST", "/v1/acquire", body, extra_timeout=body.get("wait_s", 0))

    def renew(self, lease_id: str, ttl_s: float | None = None) -> dict:
        body = {"lease_id": lease_id} | ({} if ttl_s is None else {"ttl_s": ttl_s})
        return self.request("POST", "/v1/renew", body)

    def release(self, lease_id: str) -> dict:
        return self.request("POST", "/v1/release", {"lease_id": lease_id})

    def lend(self, open: bool) -> dict:
        return self.request("POST", "/v1/lend", {"open": open})

    def set_pool_max(self, pool: str, maximum: int | None) -> dict:
        """Override `pool`'s `max` until the daemon restarts; None resets it."""
        return self.request("POST", f"/v1/pools/{quote(pool, safe='')}", {"max": maximum})
