"""`leasehold ui`: the machine in a browser, served on loopback by the user who asks.

A small HTTP server on 127.0.0.1 serves one page (`static/ui.html`, everything inline) and reads
the daemon on the page's behalf. It never changes anything: every route is a GET.

* `/`: the page.
* `/api/status`, `/api/events?since=N`: the daemon's `GET /v1/status` and `/v1/events`.
* `/metrics`: the status as Prometheus metrics (`leasehold.metrics`).

Loopback is reachable by every user on the machine, and some of them should not read another's
leases, so each run makes a random token and the API routes answer 403 without it
(`?token=...`, which the printed URL carries). The page itself holds no data and is served to
anyone. Requests whose `Host` is not this server's loopback address are refused, so a web page
elsewhere cannot reach the server through DNS rebinding.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import shutil
import tempfile
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from leasehold import metrics
from leasehold.client import ApiError, Client, Unavailable

#: What the page may do: run its inline script and styles, fetch from this server, nothing else.
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def page() -> bytes:
    return (files("leasehold") / "static" / "ui.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server: UiServer

    def log_message(self, fmt, *args):
        return

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(421, "text/plain", b"wrong host\n")
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", self.server.page, csp=True)
            return
        if url.path not in ("/api/status", "/api/events", "/metrics"):
            self._send(404, "application/json", b'{"error": "not_found"}')
            return
        if not self._token_ok(query):
            self._json(403, {"error": "forbidden", "detail": "this needs the URL's token"})
            return
        client = self.server.client_factory()
        try:
            if url.path == "/api/status":
                self._json(200, client.status())
            elif url.path == "/api/events":
                since = query.get("since", ["0"])[0]
                self._json(200, client.events(int(since) if since.isdigit() else 0))
            else:
                text = metrics.render(client.status()).encode()
                self._send(200, "text/plain; version=0.0.4; charset=utf-8", text)
        except Unavailable as exc:
            self._json(502, {"error": "unavailable", "detail": str(exc)})
        except ApiError as exc:
            self._json(502, {"error": "daemon_error", "detail": str(exc)})

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in self.server.hosts

    def _token_ok(self, query: dict) -> bool:
        given = query.get("token", [""])[0]
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def _json(self, code: int, body: dict) -> None:
        self._send(code, "application/json", json.dumps(body).encode())

    def _send(self, code: int, kind: str, body: bytes, *, csp: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _HEADERS.items():
            self.send_header(name, value)
        if csp:
            self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(body)


def open_browser(url: str, *, keep_s: float = 60.0) -> None:
    """Open `url` in a browser without putting it in any process's arguments, where every user
    on the machine could read its token (`/proc/<pid>/cmdline`): the browser is handed a file
    only this user may read, which sends it on. The file goes after `keep_s`."""
    directory = tempfile.mkdtemp(prefix="leasehold-ui-")
    page = os.path.join(directory, "open.html")
    target = html.escape(url, quote=True)
    fd = os.open(page, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" '
            f'content="0;url={target}"><title>leasehold</title>'
            f'<p><a href="{target}">Open leasehold</a></p>'
        )
    webbrowser.open(f"file://{page}")
    timer = threading.Timer(
        keep_s, shutil.rmtree, args=(directory,), kwargs={"ignore_errors": True}
    )
    timer.daemon = True
    timer.start()


class UiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        client_factory: Callable[[], Client],
        *,
        port: int = 0,
        token: str | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.client_factory = client_factory
        self.token = token or secrets.token_urlsafe(24)
        self.page = page()
        bound = self.server_address[1]
        self.hosts = {f"127.0.0.1:{bound}", f"localhost:{bound}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?token={self.token}"

    def start(self) -> threading.Thread:
        """Serve on a background thread (for tests and `demo`); `shutdown()` stops it."""
        thread = threading.Thread(target=self.serve_forever, daemon=True)
        thread.start()
        return thread
