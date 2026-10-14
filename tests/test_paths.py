"""Where each mode keeps things (leasehold.paths), and which config is used."""

from __future__ import annotations

from pathlib import Path

import pytest

from leasehold import paths
from leasehold.machine import MachineConfig, resolve

ENV = {"HOME": "/home/ann", "XDG_RUNTIME_DIR": "/run/user/1000"}


def test_a_user_daemon_lives_in_the_users_xdg_directories():
    assert paths.default_socket("user", ENV) == Path("/run/user/1000/leasehold/leasehold.sock")
    assert paths.default_ledger_dir("user", ENV) == Path("/home/ann/.local/state/leasehold")
    assert paths.user_config(ENV) == Path("/home/ann/.config/leasehold/machine.toml")
    custom = ENV | {"XDG_STATE_HOME": "/srv/state", "XDG_CONFIG_HOME": "/srv/config"}
    assert paths.default_ledger_dir("user", custom) == Path("/srv/state/leasehold")
    assert paths.user_config(custom) == Path("/srv/config/leasehold/machine.toml")


def test_the_system_daemon_lives_in_run_and_var_lib():
    assert paths.default_socket("system", ENV) == Path("/run/leasehold/leasehold.sock")
    assert paths.default_ledger_dir("system", ENV) == Path("/var/lib/leasehold")


def test_a_user_daemon_without_a_runtime_directory_is_told_how_to_get_one():
    with pytest.raises(paths.PathError, match="XDG_RUNTIME_DIR"):
        paths.default_socket("user", {"HOME": "/home/ann"})


@pytest.mark.parametrize(
    ("mode", "socket", "ledger"),
    [
        ("user", "/run/leasehold/leasehold.sock", "/home/ann/.local/state/leasehold"),
        ("user", "/run/user/1000/leasehold/s.sock", "/var/lib/leasehold/sub"),
        ("system", "/run/user/1000/leasehold/s.sock", "/var/lib/leasehold"),
        ("system", "/run/leasehold/leasehold.sock", "/home/ann/lh"),
    ],
)
def test_paths_that_belong_to_the_other_mode_are_refused(mode, socket, ledger):
    with pytest.raises(paths.PathError):
        paths.check_mode(mode, Path(socket), Path(ledger), ENV)


def test_config_lookup_explicit_then_environment_then_user_then_system(tmp_path):
    env = {"HOME": str(tmp_path)}
    assert paths.config_candidates("x.toml", env | {paths.ENV_CONFIG: "y.toml"}) == [Path("x.toml")]
    assert paths.config_candidates(None, env | {paths.ENV_CONFIG: "y.toml"}) == [Path("y.toml")]
    assert paths.config_candidates(None, env) == [paths.user_config(env), paths.SYSTEM_CONFIG]
    assert paths.config_candidates(None, env, "system") == [paths.SYSTEM_CONFIG]
    assert paths.config_candidates(None, env, "user") == [paths.user_config(env)]


def test_an_explicit_config_must_exist_and_an_implicit_one_may_not(tmp_path):
    env = {"HOME": str(tmp_path)}
    with pytest.raises(FileNotFoundError, match=r"nope\.toml"):
        paths.find_config(str(tmp_path / "nope.toml"), env)
    assert paths.find_config(None, env, "user") is None
    mine = paths.user_config(env)
    mine.parent.mkdir(parents=True)
    mine.write_text("")
    assert paths.find_config(None, env, "user") == mine


def test_resolve_fills_in_the_modes_paths_and_socket_mode():
    config = MachineConfig.defaults(slots=4)
    user = resolve(config, None, ENV)
    assert (user.mode, user.socket_mode) == ("user", "0600")
    assert user.socket == Path("/run/user/1000/leasehold/leasehold.sock")
    system = resolve(config, "system", ENV)
    assert (system.mode, system.socket_mode) == ("system", "0660")
    assert system.ledger_path.parent == Path("/var/lib/leasehold")


def test_resolve_keeps_what_the_config_sets_and_refuses_a_contradiction():
    config = MachineConfig.model_validate(
        {"mode": "system", "ceiling": {"slots": 2}, "socket": "/run/leasehold/x.sock"}
    )
    assert resolve(config, None, ENV).socket == Path("/run/leasehold/x.sock")
    with pytest.raises(ValueError, match="system daemon"):
        resolve(config, "user", ENV)


def test_an_unresolved_config_has_no_ledger_path():
    with pytest.raises(ValueError, match="resolve"):
        _ = MachineConfig.defaults(slots=1).ledger_path
