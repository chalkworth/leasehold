"""From nothing to a lease as an ordinary user: `serve` with no config and no root, then
clients that find it on their own (`leasehold serve`, `hold`, `status`, `init`, `doctor`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from support import CHILD_ENV, until

from leasehold import paths
from leasehold.client import Client
from leasehold.ledger import FILENAME
from leasehold.machine import MachineConfig
from leasehold.template import EXAMPLE, kept_for_you, render

ROOT = Path(__file__).resolve().parent.parent


def run(env, *args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "leasehold", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        **kw,
    )


@pytest.fixture
def user_env(sock_dir, tmp_path):
    """An ordinary user's environment: a home of its own, and a runtime directory short enough
    for a unix socket. No leasehold variables."""
    env = {k: v for k, v in CHILD_ENV.items() if not k.startswith(("LEASEHOLD_", "XDG_"))}
    return env | {"HOME": str(tmp_path / "home"), "XDG_RUNTIME_DIR": str(sock_dir)}


@pytest.fixture
def user_daemon(user_env):
    daemon = subprocess.Popen(
        [sys.executable, "-m", "leasehold", "serve"],
        env=user_env,
        stderr=subprocess.PIPE,
        text=True,
    )
    socket = paths.user_socket(user_env)
    try:
        until(lambda: socket.exists() or daemon.poll() is not None)
        assert daemon.poll() is None, daemon.stderr.read()
        yield daemon, socket
    finally:
        daemon.terminate()
        daemon.wait(timeout=15)


def test_a_user_daemon_needs_no_config_and_no_root(user_env, user_daemon):
    _, socket = user_daemon
    assert oct(socket.stat().st_mode & 0o777) == "0o600"
    assert oct(socket.parent.stat().st_mode & 0o777) == "0o700"
    assert (paths.user_state_dir(user_env) / FILENAME).is_file()
    status = Client(socket).status()
    assert status["ceiling"]["slots"] == (os.cpu_count() or 1)
    assert set(status["pools"]) == {"work", "external", "lend", "observed"}


def test_clients_find_the_user_daemon_without_being_told(user_env, user_daemon):
    held = run(user_env, "hold", "--weight", "1", "--", sys.executable, "-c", "print('in')")
    assert held.returncode == 0, held.stderr
    assert held.stdout.strip() == "in"
    status = run(user_env, "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["leases"] == []


def test_doctor_passes_beside_a_running_user_daemon(user_env, user_daemon):
    result = run(user_env, "doctor", "--json")
    checks = {c["name"]: c for c in json.loads(result.stdout)}
    assert result.returncode == 0, result.stdout
    assert checks["socket"]["level"] == "ok" and checks["daemon"]["level"] == "ok"
    assert "none" in checks["config"]["detail"]  # no config: defaults


def test_without_a_daemon_clients_say_where_they_looked_and_how_to_start_one(user_env):
    result = run(user_env, "status")
    assert result.returncode == 69
    assert "tried:" in result.stderr and "leasehold serve" in result.stderr
    doctor = run(user_env, "doctor")
    assert doctor.returncode == 1
    assert "FAIL  socket" in doctor.stdout and "fix: start one" in doctor.stdout


def test_a_user_daemon_refuses_the_system_daemons_paths(user_env, tmp_path):
    config = tmp_path / "m.toml"
    config.write_text('socket = "/run/leasehold/leasehold.sock"\n[ceiling]\nslots = 1\n')
    result = run(user_env, "--config", str(config), "serve")
    assert result.returncode == 1
    assert "belongs to the system daemon" in result.stderr


def test_init_writes_a_config_the_user_daemon_then_uses(user_env):
    result = run(user_env, "init")
    assert result.returncode == 0, result.stderr
    written = paths.user_config(user_env)
    config = MachineConfig.model_validate(tomllib.loads(written.read_text()))
    assert config.mode == "user" and config.socket is None
    cpus = os.cpu_count() or 1
    assert config.ceiling.slots == max(1, cpus - kept_for_you(cpus))
    assert set(config.pool_set().pools) == {"work", "background"}
    assert config.kinds == {"work": "work", "external": "background"}
    assert config.guarded and config.lend.pools == []
    again = run(user_env, "init")
    assert again.returncode == 1 and "--force" in again.stderr
    assert run(user_env, "init", "--force").returncode == 0


def test_init_never_writes_through_a_symlink(user_env, tmp_path):
    target = paths.user_config(user_env)
    target.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.toml"
    target.symlink_to(elsewhere)  # dangling: exists() would say no file is there
    for args in (["init"], ["init", "--force"]):
        result = run(user_env, *args)
        assert result.returncode == 1 and "symlink" in result.stderr
    assert not elsewhere.exists()


def test_init_print_writes_nothing(user_env):
    result = run(user_env, "init", "--system", "--print")
    assert result.returncode == 0
    config = MachineConfig.model_validate(tomllib.loads(result.stdout))
    assert config.mode == "system" and config.socket == paths.SYSTEM_SOCKET
    assert not paths.user_config(user_env).exists()


def test_the_example_config_is_the_template_rendered():
    """deploy/machine.toml is generated: `python -c 'from leasehold.template import *;
    print(render(**EXAMPLE), end="")' > deploy/machine.toml` after changing the template."""
    assert (ROOT / "deploy" / "machine.toml").read_text() == render(**EXAMPLE)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_an_installed_wheel_carries_its_package_data(tmp_path):
    """`init --print` and the web page from a wheel installed outside the checkout: the template
    and `static/ui.html` are package data."""
    built = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next((tmp_path / "dist").glob("leasehold-*.whl"))
    venv = tmp_path / "venv"
    subprocess.run(["uv", "venv", "-q", str(venv)], check=True, timeout=120)
    subprocess.run(
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(wheel)],
        check=True,
        timeout=300,
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    result = subprocess.run(
        [str(venv / "bin" / "leasehold"), "init", "--print"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    MachineConfig.model_validate(tomllib.loads(result.stdout))
    size = "from leasehold import webui; print(len(webui.page()))"
    page = subprocess.run(
        [str(venv / "bin" / "python"), "-c", size],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert page.returncode == 0 and int(page.stdout) > 10_000, page.stderr


def test_a_user_daemon_ignores_the_machines_config(user_env, monkeypatch, tmp_path):
    """With a system config present, plain `serve` still looks only for the user's."""
    system = tmp_path / "etc-machine.toml"
    system.write_text('mode = "system"\n[ceiling]\nslots = 1\n')
    monkeypatch.setattr(paths, "SYSTEM_CONFIG", system)
    assert paths.find_config(None, user_env, "user") is None


def test_a_system_socket_directory_is_group_traversable_whatever_the_umask(tmp_path):
    from types import SimpleNamespace

    from leasehold.server import Daemon

    socket = tmp_path / "run" / "leasehold" / "leasehold.sock"
    stub = SimpleNamespace(path=socket, config=SimpleNamespace(mode="system", socket_group=None))
    old = os.umask(0o077)  # systemd's UMask=0077
    try:
        Daemon._make_socket_dir(stub)
    finally:
        os.umask(old)
    for made in (socket.parent, socket.parent.parent):
        assert oct(made.stat().st_mode & 0o777) == "0o750"


def test_the_config_and_socket_options_also_follow_the_command(tmp_path):
    from leasehold.cli import _parser

    args = _parser().parse_args(["serve", "--config", "x.toml"])
    assert args.config == "x.toml"
    args = _parser().parse_args(["--socket", "a", "shims", "list", "--socket", "b"])
    assert args.socket == "b"
    assert _parser().parse_args(["--config", "y", "status"]).config == "y"


@pytest.mark.parametrize("how", ["option", "environment"])
def test_serve_listens_where_socket_or_the_environment_says(user_env, sock_dir, tmp_path, how):
    config = tmp_path / "m.toml"
    config.write_text(
        f'mode = "user"\nsocket = "{sock_dir / "config.sock"}"\n[ceiling]\nslots = 2\n'
    )
    wanted = sock_dir / "named.sock"
    named = ["--socket", str(wanted)] if how == "option" else []
    env = user_env | ({"LEASEHOLD_SOCKET": str(wanted)} if how == "environment" else {})
    daemon = subprocess.Popen(
        [sys.executable, "-m", "leasehold", "--config", str(config), "serve", *named],
        env=env, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: wanted.exists() or daemon.poll() is not None)
        assert daemon.poll() is None, daemon.stderr.read()
        assert Client(wanted).status()["ceiling"]["slots"] >= 1
        assert not (sock_dir / "config.sock").exists()
    finally:
        daemon.terminate()
        daemon.wait(timeout=15)
