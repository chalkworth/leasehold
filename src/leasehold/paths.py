"""Where leasehold keeps things, in each of its two modes.

* **system**: one daemon for the whole machine, run by systemd as the `leasehold` user
  (`serve --system`). Socket `/run/leasehold/leasehold.sock`, ledger under `/var/lib/leasehold`,
  config `/etc/leasehold/machine.toml`.
* **user**: a daemon of your own, no root needed (`serve`, the default). Socket under
  `$XDG_RUNTIME_DIR/leasehold/`, ledger under `$XDG_STATE_HOME/leasehold/` (by default
  `~/.local/state/leasehold/`), config `$XDG_CONFIG_HOME/leasehold/machine.toml` (by default
  `~/.config/leasehold/machine.toml`).

The mode is always chosen, never guessed from the uid: the system daemon is not root either.
Every function takes the environment as a mapping, so tests pass their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

Mode = Literal["system", "user"]

ENV_CONFIG = "LEASEHOLD_CONFIG"
SYSTEM_CONFIG = Path("/etc/leasehold/machine.toml")
SYSTEM_SOCKET = Path("/run/leasehold/leasehold.sock")
SYSTEM_LEDGER_DIR = Path("/var/lib/leasehold")
#: Who may use the socket, by mode: the `leasehold` group's members, or only you.
SOCKET_MODES: dict[str, str] = {"system": "0660", "user": "0600"}


class PathError(ValueError):
    """A path leasehold cannot use, with what to do about it."""


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def user_config(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(_home(env) / ".config")
    return Path(base) / "leasehold" / "machine.toml"


def user_state_dir(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_STATE_HOME") or str(_home(env) / ".local" / "state")
    return Path(base) / "leasehold"


def user_runtime_dir(env: Mapping[str, str]) -> Path | None:
    """`$XDG_RUNTIME_DIR/leasehold`, or None when the session has no runtime directory."""
    base = env.get("XDG_RUNTIME_DIR")
    return Path(base) / "leasehold" if base else None


def user_socket(env: Mapping[str, str]) -> Path | None:
    runtime = user_runtime_dir(env)
    return runtime / "leasehold.sock" if runtime else None


def default_socket(mode: Mode, env: Mapping[str, str]) -> Path:
    if mode == "system":
        return SYSTEM_SOCKET
    socket = user_socket(env)
    if socket is None:
        raise PathError(
            "XDG_RUNTIME_DIR is not set, so a user daemon has nowhere to put its socket: "
            "log in through a normal session (systemd sets it), or pass --socket"
        )
    return socket


def default_ledger_dir(mode: Mode, env: Mapping[str, str]) -> Path:
    return SYSTEM_LEDGER_DIR if mode == "system" else user_state_dir(env)


def check_mode(mode: Mode, socket: Path, ledger_dir: Path, env: Mapping[str, str]) -> None:
    """Refuse paths that belong to the other mode: a user daemon under `/run/leasehold` or
    `/var/lib/leasehold`, or the system daemon inside a user's own directories."""
    system = (SYSTEM_SOCKET.parent, SYSTEM_LEDGER_DIR)
    if mode == "user":
        for path in (socket, ledger_dir):
            if any(path == root or root in path.parents for root in system):
                raise PathError(f"{path} belongs to the system daemon; a user daemon cannot use it")
        return
    runtime = env.get("XDG_RUNTIME_DIR")
    homes = [Path(runtime)] if runtime else []
    homes += [_home(env)] if env.get("HOME") else []
    for path in (socket, ledger_dir):
        if any(path == root or root in path.parents for root in homes):
            raise PathError(f"{path} is in a user's own directory; the system daemon may not")


def config_candidates(
    explicit: str | None, env: Mapping[str, str], mode: Mode | None = None
) -> list[Path]:
    """Where a config is looked for, in order: an explicit one (`--config`, then
    `$LEASEHOLD_CONFIG`) alone; otherwise the `mode`'s own, or with no mode (a client looking for
    a daemon) the user's then the system's."""
    if explicit:
        return [Path(explicit)]
    if env.get(ENV_CONFIG):
        return [Path(env[ENV_CONFIG])]
    if mode == "system":
        return [SYSTEM_CONFIG]
    if mode == "user":
        return [user_config(env)]
    return [user_config(env), SYSTEM_CONFIG]


def find_config(
    explicit: str | None, env: Mapping[str, str], mode: Mode | None = None
) -> Path | None:
    """The config to use, or None when there is none (a daemon then runs on defaults). An
    explicit config that does not exist is an error."""
    candidates = config_candidates(explicit, env, mode)
    if explicit or env.get(ENV_CONFIG):
        if not candidates[0].exists():
            raise FileNotFoundError(f"no config at {candidates[0]}")
        return candidates[0]
    return next((path for path in candidates if path.exists()), None)
