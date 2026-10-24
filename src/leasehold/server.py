"""`leasehold serve`: the machine agent over HTTP/JSON on a local unix socket.

The socket (mode `socket_mode`, group `socket_group`) is the only listener: whoever may open it
may ask for leases, and the peer's uid (`SO_PEERCRED`) names the user a lease is charged to.
A tick thread runs `MachineAgent.tick` every `tick_s`, and with the memory net on, a thread of
its own runs the net (`leasehold.net`). SIGTERM or SIGINT stops them cleanly: the listener
closes, the socket file goes, the net resumes whatever it parked, and the leases stay in the
ledger until their TTLs, so a restarted daemon picks them up again.

A request body is read only as far as a well-formed `Content-Length` of at most `_MAX_BODY`
says: a missing one is no body, a malformed or negative one 400 `bad_request`, a larger one 413
`too_large`. Each read from a client's socket gives up after `_READ_TIMEOUT_S`, closing that
connection, so a stalled client cannot keep a thread; the wait inside a long-polling `acquire`
reads nothing and is not cut short by it.
"""

from __future__ import annotations

import contextlib
import grp
import json
import os
import pwd
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from leasehold.agent import MachineAgent
from leasehold.api import Peer, handle
from leasehold.machine import MachineConfig
from leasehold.paths import SOCKET_MODES

_MAX_BODY = 64 * 1024
_READ_TIMEOUT_S = 30.0


def peer_of(sock: socket.socket) -> Peer:
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except (OSError, AttributeError):
        return Peer()
    _pid, uid, _gid = struct.unpack("3i", raw)
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = f"uid:{uid}"
    return Peer(uid=uid, user=name)


def _length(value: str | None) -> int | None:
    """A `Content-Length` header's byte count (0 when absent), or None when it is not one."""
    if value is None:
        return 0
    value = value.strip()
    return int(value) if value.isascii() and value.isdigit() else None


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "leasehold"
    timeout = _READ_TIMEOUT_S

    def _answer(self, method: str) -> None:
        length = _length(self.headers.get("Content-Length"))
        if length is None:
            status, payload = 400, {"error": "bad_request", "detail": "malformed Content-Length"}
        elif length > _MAX_BODY:
            status, payload = 413, {"error": "too_large", "detail": f"over {_MAX_BODY} bytes"}
        else:
            try:
                raw = self.rfile.read(length) if length else None
            except TimeoutError:
                self.close_connection = True
                return
            if raw is not None and len(raw) < length:
                status, payload = 400, {"error": "bad_request", "detail": "the body is cut short"}
            else:
                status, payload = handle(
                    self.server.agent, method, self.path, raw, peer_of(self.connection)
                )
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args) -> None:
        if self.server.verbose:
            sys.stderr.write(f"leasehold: {format % args}\n")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, agent: MachineAgent, *, verbose: bool = False) -> None:
        self.agent = agent
        self.verbose = verbose
        super().__init__(str(path), _Handler, bind_and_activate=False)


def _clear_stale(path: Path) -> None:
    """Remove a socket file no daemon answers on; refuse if one does."""
    if not path.exists() and not path.is_symlink():
        return
    if not stat.S_ISSOCK(path.lstat().st_mode):
        raise RuntimeError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink()
        return
    finally:
        probe.close()
    raise RuntimeError(f"a daemon is already listening on {path}")


class Daemon:
    """The listener and the tick thread; `start` and `stop` (also usable in a test thread)."""

    def __init__(self, config: MachineConfig, agent: MachineAgent | None = None, *, verbose=False):
        self.config = config
        self.agent = agent if agent is not None else MachineAgent(config)
        self.path = Path(config.socket)
        self.stopping = threading.Event()
        self._server: _Server | None = None
        self._threads: list[threading.Thread] = []
        self.verbose = verbose

    def start(self) -> None:
        self._make_socket_dir()
        _clear_stale(self.path)
        server = _Server(self.path, self.agent, verbose=self.verbose)
        old = os.umask(0o177)  # nobody else may connect before the mode and group are set
        try:
            server.server_bind()
        finally:
            os.umask(old)
        if self.config.socket_group is not None:
            os.chown(self.path, -1, grp.getgrnam(self.config.socket_group).gr_gid)
        mode = self.config.socket_mode or SOCKET_MODES[self.config.mode or "user"]
        os.chmod(self.path, int(mode, 8))
        server.server_activate()
        self._server = server
        self.agent.tick()
        self._threads = [
            threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
            ),
            threading.Thread(target=self._ticks, daemon=True),
        ]
        if self.agent.net is not None:
            self._threads.append(
                threading.Thread(
                    target=self.agent.net.run, args=(self.stopping,), name="leasehold-net"
                )
            )
        for t in self._threads:
            t.start()

    def _make_socket_dir(self) -> None:
        """The socket's directory, if missing: a user daemon's only its owner may enter; the
        system daemon's its group may too (systemd's RuntimeDirectory usually makes it)."""
        parent = self.path.parent
        if parent.is_dir():
            return
        system = self.config.mode == "system"
        mode = 0o750 if system else 0o700
        missing = [d for d in (parent, *parent.parents) if not d.exists()]
        parent.mkdir(mode=mode, parents=True)
        for made in missing:
            os.chmod(made, mode)  # mkdir's mode is cut by the umask (systemd's UMask=0077)
        if system and self.config.socket_group is not None:
            os.chown(parent, -1, grp.getgrnam(self.config.socket_group).gr_gid)

    def _ticks(self) -> None:
        while not self.stopping.wait(self.config.tick_s):
            try:
                self.agent.tick()
            except Exception as exc:  # noqa: BLE001 -- a bad tick must not end the daemon
                sys.stderr.write(f"leasehold: tick failed: {exc!r}\n")

    def stop(self) -> None:
        self.stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for t in self._threads:
            t.join(timeout=5)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()


def serve(config: MachineConfig, *, verbose: bool = False) -> int:
    """Serve `config` (resolved for its mode, `machine.resolve`) until SIGTERM or SIGINT."""
    daemon = Daemon(config, verbose=verbose)
    daemon.start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: daemon.stopping.set())
    sys.stderr.write(f"leasehold: {config.name} ({config.mode}) serving on {daemon.path}\n")
    while not daemon.stopping.wait(1.0):
        pass
    daemon.stop()
    sys.stderr.write("leasehold: stopped\n")
    return 0
