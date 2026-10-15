"""`leasehold setup` (`leasehold.setup`) in a home of its own: it writes a sized two-pool config
once and never over one that is there, installs shims only for programs on PATH, names the
agents it finds, says how to start a daemon where there is no user manager, and `--remove`
takes the unit and shims away and leaves the config."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from support import CHILD_ENV

from leasehold import machine, paths, setup


def home(tmp_path: Path) -> dict[str, str]:
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name in ("cargo", "pytest", "claude"):
        tool = bin_ / name
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o755)
    return CHILD_ENV | {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        # No systemctl here: setup must say how to start a daemon by hand.
        "PATH": f"{bin_}:{Path(sys.executable).parent}",
        "LEASEHOLD_CONTAIN": "off",
    }


def setup_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "leasehold", "setup", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_setup_writes_a_sized_config_installs_shims_and_names_the_agents(tmp_path):
    env = home(tmp_path)
    (tmp_path / "run").mkdir()
    done = setup_cli(env)
    out = done.stdout
    config = machine.load(paths.user_config(env))
    assert set(config.pool_set().pools) == {"work", "background"}
    assert "slots for leased work" in out
    assert "no systemd user manager here; start one with `leasehold serve &`" in out
    shim_dir = Path(env["XDG_DATA_HOME"]) / "leasehold" / "shims"
    made = {p.name for p in shim_dir.iterdir()}
    assert {"cargo", "pytest"} <= made and "make" not in made  # make is not on this PATH
    assert "agents: claude: in Claude Code: /plugin marketplace add" in out
    assert "containment: advisory: LEASEHOLD_CONTAIN=off" in out
    assert "undo with `leasehold setup --remove`" in out


def test_setup_never_replaces_a_config_and_is_safe_to_repeat(tmp_path):
    env = home(tmp_path)
    target = paths.user_config(env)
    target.parent.mkdir(parents=True)
    target.write_text("[ceiling]\nslots = 3\n")
    for _ in range(2):
        done = setup_cli(env)
        assert "is there already; left as it is" in done.stdout, done.stderr
    assert target.read_text() == "[ceiling]\nslots = 3\n"


def test_remove_takes_the_unit_and_shims_and_leaves_the_config(tmp_path):
    env = home(tmp_path)
    setup_cli(env)
    unit = setup.unit_path(env)
    unit.parent.mkdir(parents=True)
    unit.write_text(setup.unit_text("/x/leasehold"))
    done = setup_cli(env, "--remove")
    assert done.returncode == 0, done.stderr
    assert not unit.exists()
    assert "shims: removed" in done.stdout
    assert not (Path(env["XDG_DATA_HOME"]) / "leasehold").exists()  # its empty directories too
    assert paths.user_config(env).exists()


def test_the_unit_runs_this_installations_daemon():
    text = setup.unit_text("/home/u/.local/bin/leasehold")
    assert "ExecStart=/home/u/.local/bin/leasehold serve --user\n" in text
    assert "WantedBy=default.target" in text
    assert os.path.isabs(setup.executable())


def test_setup_says_when_the_config_has_0_2s_defaults_and_leaves_it(tmp_path):
    from leasehold import template

    env = home(tmp_path)
    (tmp_path / "run").mkdir()
    path = paths.user_config(env)
    path.parent.mkdir(parents=True)
    old = template.render_user_0_2(name="box", slots=6, cpus=6, memory="11g", about="x")
    path.write_text(old)
    out = setup_cli(env).stdout
    assert "config: it lacks what today's config has: no [memory] table" in out
    assert "`leasehold setup --upgrade-config --dry-run` shows the change" in out
    assert path.read_text() == old
