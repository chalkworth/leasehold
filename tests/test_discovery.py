"""How a client finds its daemon (leasehold.discovery): named sockets are used as given; the
system and user sockets are probed, and only one that answers is taken."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from leasehold import discovery, paths


def listening(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(1)
    return sock


def test_a_named_socket_is_used_without_probing(sock_dir):
    env = {discovery.ENV_SOCKET: "/from/env.sock"}
    assert discovery.find("/named.sock", None, env) == Path("/named.sock")
    assert discovery.find(None, None, env) == Path("/from/env.sock")


def test_a_named_config_without_a_socket_means_its_modes_socket(tmp_path):
    """Not the first daemon that answers: the one that config's daemon serves on."""
    config = tmp_path / "m.toml"
    config.write_text("[ceiling]\nslots = 1\n")
    env = {"XDG_RUNTIME_DIR": "/run/user/7"}
    assert discovery.find(None, str(config), env) == Path("/run/user/7/leasehold/leasehold.sock")
    config.write_text('mode = "system"\n[ceiling]\nslots = 1\n')
    assert discovery.find(None, str(config), env) == paths.SYSTEM_SOCKET


def test_a_named_config_that_cannot_be_read_is_an_error(tmp_path):
    with pytest.raises(discovery.DiscoveryError, match="cannot read"):
        discovery.find(None, str(tmp_path / "missing.toml"), {})


def test_a_named_configs_socket_is_used(tmp_path):
    config = tmp_path / "m.toml"
    config.write_text('socket = "/srv/lh.sock"\n[ceiling]\nslots = 1\n')
    assert discovery.find(None, str(config), {}) == Path("/srv/lh.sock")
    assert discovery.find(None, None, {paths.ENV_CONFIG: str(config)}) == Path("/srv/lh.sock")


def test_a_flag_outranks_the_environment(tmp_path):
    """Inside `hold`, which exports `LEASEHOLD_SOCKET`, `--config` still names the daemon."""
    config = tmp_path / "m.toml"
    config.write_text('socket = "/srv/lh.sock"\n[ceiling]\nslots = 1\n')
    env = {discovery.ENV_SOCKET: "/from/env.sock", paths.ENV_CONFIG: "/from/env.toml"}
    assert discovery.find(None, str(config), env) == Path("/srv/lh.sock")
    assert discovery.find("/named.sock", str(config), env) == Path("/named.sock")
    assert discovery.find(None, None, env) == Path("/from/env.sock")
    only_config = {paths.ENV_CONFIG: str(config)}
    assert discovery.find(None, None, only_config) == Path("/srv/lh.sock")


def test_the_first_socket_that_answers_is_taken(sock_dir):
    env = {"HOME": str(sock_dir), "XDG_RUNTIME_DIR": str(sock_dir)}
    user = paths.user_socket(env)
    user.parent.mkdir()
    server = listening(user)
    try:
        found = discovery.candidates(None, None, env)
        assert [c.source for c in found] == ["system", "user"]
        assert found[1].answers and found[1].reason == "a daemon answers"
        assert discovery.choose(found) == user
    finally:
        server.close()


def test_a_stale_socket_file_is_passed_over(sock_dir):
    env = {"HOME": str(sock_dir), "XDG_RUNTIME_DIR": str(sock_dir)}
    user = paths.user_socket(env)
    user.parent.mkdir()
    listening(user).close()  # the file stays; nothing listens
    found = discovery.candidates(None, None, env)
    assert found[1].answers is False and "stale" in found[1].reason
    assert discovery.choose(found) is None
    assert discovery.find(None, None, env) == paths.SYSTEM_SOCKET  # so the error names it


def test_a_socket_this_user_may_not_open_is_passed_over_with_the_reason(sock_dir):
    if os.getuid() == 0:
        return  # root opens anything
    path = sock_dir / "locked.sock"
    server = listening(path)
    try:
        os.chmod(path, 0o000)
        answers, reason = discovery.probe(path)
        assert not answers and "permission denied" in reason
    finally:
        server.close()


def test_a_socket_in_a_directory_this_user_may_not_enter_is_passed_over(sock_dir):
    """A system daemon's directory closed to other users (0750) is passed over, not crashed on."""
    if os.getuid() == 0:
        return  # root enters anything
    closed = sock_dir / "closed"
    closed.mkdir()
    server = listening(closed / "leasehold.sock")
    try:
        os.chmod(closed, 0o000)
        answers, reason = discovery.probe(closed / "leasehold.sock")
        assert not answers and "permission denied" in reason
    finally:
        os.chmod(closed, 0o700)
        server.close()


def test_no_runtime_directory_is_reported_not_crashed_on():
    found = discovery.candidates(None, None, {"HOME": "/nonexistent"}, probe=lambda p: (False, "-"))
    assert found[1].source == "user" and "XDG_RUNTIME_DIR" in found[1].reason
