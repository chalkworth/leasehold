"""How a client finds the daemon to talk to.

Named sockets are used as given and fail fast: `--socket`, then the socket of a config named
by `--config`, then `$LEASEHOLD_SOCKET`, then the socket of a config named by
`$LEASEHOLD_CONFIG`. A flag outranks the environment, so a command run inside `hold` (which
exports `LEASEHOLD_SOCKET`) can still name another daemon. A config's socket is its `socket`,
or its mode's default socket; a named config that cannot be read is an error, never a reason
to look elsewhere. Otherwise the client tries the
system daemon's socket, then the user's own, and takes the first that answers: a socket file
that exists is not enough (it may be stale, or another user's). `leasehold doctor` prints every
candidate and why it was taken or passed over.
"""

from __future__ import annotations

import errno
import os
import socket as _socket
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from leasehold import paths

ENV_SOCKET = "LEASEHOLD_SOCKET"


@dataclass(frozen=True)
class Candidate:
    path: Path
    #: Where the path came from: `--socket`, `$LEASEHOLD_SOCKET`, a config, `system`, `user`.
    source: str
    #: Whether something answered on it (None: not probed, because it was named).
    answers: bool | None
    #: Why it was or was not taken, in words.
    reason: str


def probe(path: Path, timeout: float = 1.0) -> tuple[bool, str]:
    """Whether a daemon accepts connections on `path`, and why not."""
    try:
        os.stat(path)
    except FileNotFoundError:
        return False, "no socket there"
    except PermissionError:
        return False, "permission denied (its directory is closed to you; not in its group?)"
    except OSError as exc:
        return False, exc.strerror or str(exc)
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except PermissionError:
        return False, "permission denied (not in the socket's group?)"
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return False, "nothing listening (a stale socket file)"
        return False, exc.strerror or str(exc)
    finally:
        sock.close()
    return True, "a daemon answers"


class DiscoveryError(ValueError):
    """A named config the client cannot take its socket from."""


def named_socket(path: Path, env: Mapping[str, str]) -> Path:
    """The socket a named config's daemon serves on: its `socket`, else its `mode`'s default
    (a config with no mode is a user daemon's)."""
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DiscoveryError(f"cannot read the config {path}: {exc}") from exc
    value = raw.get("socket")
    if isinstance(value, str) and value:
        return Path(value)
    mode = raw.get("mode", "user")
    if mode not in ("system", "user"):
        raise DiscoveryError(f"{path}: mode {mode!r} is not system or user")
    try:
        return paths.default_socket(mode, env)
    except paths.PathError as exc:
        raise DiscoveryError(str(exc)) from exc


def _configured_socket(path: Path) -> Path | None:
    """The `socket` a config file names, if it can be read and names one."""
    try:
        value = tomllib.loads(path.read_text()).get("socket")
    except (OSError, ValueError):
        return None
    return Path(value) if isinstance(value, str) and value else None


def candidates(
    socket: str | None, config: str | None, env: Mapping[str, str], *, probe=probe
) -> list[Candidate]:
    """Every socket considered, in order. The first that is named, or else the first that
    answers, is the one to use (`choose`)."""
    if socket:
        return [Candidate(Path(socket), "--socket", None, "named on the command line")]
    if config:
        return [
            Candidate(named_socket(Path(config), env), f"config {config}", None, "its daemon's")
        ]
    if env.get(ENV_SOCKET):
        return [
            Candidate(Path(env[ENV_SOCKET]), f"${ENV_SOCKET}", None, "named in the environment")
        ]
    named = env.get(paths.ENV_CONFIG)
    if named:
        return [Candidate(named_socket(Path(named), env), f"config {named}", None, "its daemon's")]
    found = []
    system = _configured_socket(paths.SYSTEM_CONFIG) or paths.SYSTEM_SOCKET
    user = _configured_socket(paths.user_config(env)) or paths.user_socket(env)
    for path, source in ((system, "system"), (user, "user")):
        if path is None:
            found.append(Candidate(Path("-"), source, False, "XDG_RUNTIME_DIR is not set"))
            continue
        answers, reason = probe(path)
        found.append(Candidate(path, source, answers, reason))
    return found


def choose(found: list[Candidate]) -> Path | None:
    """The socket to use: a named one, else the first that answers, else None."""
    for candidate in found:
        if candidate.answers is None or candidate.answers:
            return candidate.path
    return None


def find(socket: str | None, config: str | None, env: Mapping[str, str]) -> Path:
    """The socket a client should use. With no daemon found, the system socket, so the error a
    client then reports names the usual place."""
    return choose(candidates(socket, config, env)) or paths.SYSTEM_SOCKET
