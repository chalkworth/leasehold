"""Push this machine's status to a URL: a webhook for a dashboard, an alerting system or a
fleet view.

`leasehold report` reads the daemon's own `GET /v1/status` and POSTs that JSON object as it is
(`Content-Type: application/json`, the status schema in `docs/reference.md`) every `--interval`
seconds. With `--token-file` it sends `Authorization: Bearer <token>`; `--header` adds others.
The token file must not be world-readable (mode bit 0o004): `root:leasehold` 0640 is the mode
the unit expects. The URL must be https, unless its host is loopback. A failed POST is logged
once per status-code change and never ends the process.
"""

from __future__ import annotations

import json
import logging
import re
import stat
import time
import urllib.error
import urllib.request
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from leasehold import __version__
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import EXIT_UNAVAILABLE

__all__ = [
    "EXIT_UNAVAILABLE",
    "ReportError",
    "observe",
    "parse_headers",
    "read_token",
    "require_url",
    "run",
]

log = logging.getLogger("leasehold.report")
_TIMEOUT_S = 10.0
#: Some edges refuse Python's default User-Agent, so the report names itself.
USER_AGENT = f"leasehold/{__version__}"


class ReportError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def read_token(path: Path) -> str:
    """The token in `path`. Refuses a file whose mode has the other-read bit
    (0o004): group read is the leasehold group's, world read is not."""
    try:
        info = path.stat()
    except OSError as exc:
        raise ReportError(f"cannot read token file {path}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ReportError(f"token file {path} is not a regular file")
    if stat.S_IMODE(info.st_mode) & 0o004:
        raise ReportError(
            f"token file {path} is world-readable (mode bit 0o004); use root:leasehold mode 0640"
        )
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ReportError(f"cannot read token file {path}: {exc}") from exc
    if not token or any(ch.isspace() for ch in token):
        raise ReportError(f"token file {path} must hold only the token")
    return token


def parse_headers(values: list[str]) -> dict[str, str]:
    """`Name: value` strings as a mapping; refuses a malformed one, or one that would replace
    the headers the report sets itself."""
    out = {}
    for value in values:
        name, sep, rest = value.partition(":")
        name, rest = name.strip(), rest.strip()
        if not sep or not _TOKEN.match(name):
            raise ReportError(f"--header {value!r} is not 'Name: value'")
        if name.lower() in ("content-type", "content-length"):
            raise ReportError(f"--header {name} is set by the report itself")
        if "\r" in rest or "\n" in rest or not rest.isprintable() or not rest.isascii():
            raise ReportError(f"--header {name}: the value must be printable ASCII")
        out[name] = rest
    return out


#: A header name: RFC 9110's token.
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def require_url(url: str) -> None:
    """https, or http when the host is loopback. Anything else is refused."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise ReportError("the report URL has no host")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and _loopback(host):
        return
    raise ReportError("the report URL must be https, or http on a loopback host")


def observe(previous: str | None, outcome: str) -> tuple[str, bool]:
    """`(outcome, should_log)`. A non-2xx outcome is logged when it differs from
    `previous`, so a repeated failure is one line until the code changes."""
    failed = not outcome.startswith("2")
    return outcome, failed and outcome != previous


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is answered as its status, never followed: following one could carry the
    token, and the status, to a URL `require_url` never saw."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _post(url: str, headers: dict[str, str], payload: dict) -> str:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"User-Agent": USER_AGENT, **headers, "Content-Type": "application/json"},
    )
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_S) as response:
            response.read()
            return str(response.status)
    except urllib.error.HTTPError as exc:
        return str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return "unreachable"


def run(
    client: Client,
    *,
    url: str,
    token_file: Path | None,
    interval: float,
    once: bool,
    headers: dict[str, str] | None = None,
) -> int:
    """Report until stopped. `--once` still returns 0 when the POST failed; it
    returns `EXIT_UNAVAILABLE` only when the daemon itself did not answer."""
    sent = dict(headers or {})
    if token_file is not None:
        if any(name.lower() == "authorization" for name in sent):
            raise ReportError("--header Authorization and --token-file both set it; use one")
        sent["Authorization"] = f"Bearer {read_token(token_file)}"
    require_url(url)
    if interval <= 0:
        raise ReportError("--interval must be a positive number of seconds")
    remembered: str | None = None
    while True:
        try:
            payload = client.status()
        except (Unavailable, ApiError) as exc:
            remembered, do_log = observe(remembered, "unavailable")
            if do_log:
                log.warning("leasehold report: the daemon did not answer (%s)", exc)
            if once:
                return EXIT_UNAVAILABLE
            time.sleep(interval)
            continue
        outcome = _post(url, sent, payload)
        remembered, do_log = observe(remembered, outcome)
        if do_log:
            log.warning("leasehold report: %s answered %s", url, outcome)
        if once:
            return 0
        time.sleep(interval)
