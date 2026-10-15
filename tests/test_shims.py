"""PATH shims (leasehold.shims, `leasehold shims`): a heavy program run through PATH runs under
a lease, and passes straight through whenever it should."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_ENV, FACTS, FakeLoad, cli, machine_config

from leasehold import shims
from leasehold.agent import MachineAgent
from leasehold.server import Daemon

ROOT = Path(__file__).resolve().parents[1]
#: A stand-in heavy program: prints what lease it runs under.
FAKE = """#!/bin/sh
echo "lease=${LEASEHOLD_LEASE_ID:-none} weight=${LEASEHOLD_WEIGHT:-none} \\
jobs=${CARGO_BUILD_JOBS:-none} omp=${OMP_NUM_THREADS:-none} args=$*"
"""


@pytest.fixture
def setup(tmp_path, sock_dir, request):
    """A daemon whose one rule leases `fakebuild build`, the real program in `bin/`, and a shim
    for it in `shims/`; `when_busy` is the test's parameter, else `run`."""
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "s.sock"),
        commands={
            "defaults": False,
            "rule": [{"programs": ["fakebuild"], "args": "^build", "weight": 2, "threads": 1}],
            "wait_s": 1,
            "when_busy": getattr(request, "param", "run"),
        },
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    real = tmp_path / "bin"
    real.mkdir()
    (real / "fakebuild").write_text(FAKE)
    (real / "fakebuild").chmod(0o755)
    shim_dir = tmp_path / "shims"
    assert shims.install(shim_dir, ["fakebuild"], python=sys.executable) == [
        ("fakebuild", "installed")
    ]
    env = {
        "PATH": f"{shim_dir}:{real}:/usr/bin:/bin",
        "LEASEHOLD_SOCKET": str(daemon.path),
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(tmp_path),
    }
    try:
        yield daemon, shim_dir, real, env
    finally:
        daemon.stop()


def run(env, *args, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["fakebuild", *args], env={**env, **extra}, capture_output=True, text=True, timeout=30
    )


def test_a_matching_run_gets_a_lease_sized_to_its_rule(setup):
    *_, env = setup
    done = run(env, "build", "--release")
    assert done.returncode == 0, done.stderr
    assert "weight=2 jobs=2 omp=1 args=build --release" in done.stdout  # the rule's threads
    assert "lease=none" not in done.stdout


def test_it_passes_through_when_it_should(setup):
    daemon, _, _, env = setup
    assert "lease=none" in run(env, "--version").stdout  # no rule matches
    inner = run(env, "build", LEASEHOLD_LEASE_ID="outer").stdout
    assert "lease=outer weight=none" in inner  # already under a lease: that one covers it
    assert "lease=none" in run(env, "build", LEASEHOLD_SHIMS="off").stdout
    daemon.stop()
    gone = run(env, "build")
    assert gone.returncode == 0 and "lease=none" in gone.stdout  # no daemon: as without shims


def test_a_busy_machine_runs_it_unleased_after_the_wait(setup):
    from leasehold.client import Client

    daemon, _, _, env = setup
    Client(daemon.path).acquire(kind="work", weight=2, lease_id="someone-else")
    done = run(env, "build")
    assert done.returncode == 0 and "lease=none" in done.stdout
    assert "running fakebuild without a lease" in done.stderr


@pytest.mark.parametrize("setup", ["deny"], indirect=True)
def test_a_busy_machine_can_refuse_it_saying_who_holds_the_machine(setup):
    from leasehold.client import Client

    daemon, _, _, env = setup
    Client(daemon.path).acquire(kind="work", weight=2, lease_id="x", labels={"cmd": "make all"})
    done = run(env, "build")
    assert done.returncode == 75 and done.stdout == ""
    assert "not running fakebuild: machine busy for 1s: make all holds 2 slots;" in done.stderr
    assert "queued #1. The command did not run. Do lighter work first" in done.stderr


def test_the_exit_code_is_the_programs(setup):
    _, _, real, env = setup
    (real / "fakebuild").write_text("#!/bin/sh\nexit 75\n")  # what leasehold calls busy
    assert run(env, "build").returncode == 75
    assert run(env, "--version").returncode == 75


def test_the_real_program_is_found_past_the_shims(tmp_path):
    shim_dir, real = tmp_path / "shims", tmp_path / "bin"
    real.mkdir()
    shims.install(shim_dir, ["tool"], python=sys.executable)
    assert shims.real("tool", f"{shim_dir}:{shim_dir}", shim_dir) is None  # never itself
    (real / "tool").write_text("#!/bin/sh\n")
    (real / "tool").chmod(0o755)
    assert shims.real("tool", f"{shim_dir}:{real}", shim_dir) == str(real / "tool")
    other = tmp_path / "other-shims"
    shims.install(other, ["tool"], python=sys.executable)
    assert shims.real("tool", f"{other}:{shim_dir}:{real}", shim_dir) == str(real / "tool")


def test_install_and_remove_touch_only_shims(tmp_path):
    d = tmp_path / "shims"
    d.mkdir()
    (d / "make").write_text("#!/bin/sh\necho mine\n")
    done = dict(shims.install(d, ["make", "cargo", "../x"], python=sys.executable))
    assert done["make"].startswith("skipped") and done["cargo"] == "installed"
    assert done["../x"].startswith("skipped")
    assert oct((d / "cargo").stat().st_mode & 0o777) == "0o755"
    assert shims.installed(d) == ["cargo"]
    assert shims.remove(d) == ["cargo"] and (d / "make").read_text() == "#!/bin/sh\necho mine\n"


def test_a_shim_quotes_its_paths(tmp_path):
    text = shims.script("it's", "/opt/py thon/bin/python", tmp_path / "a b")
    assert "'/opt/py thon/bin/python' '-m' 'leasehold.shims'" in text
    assert "'it'\\''s' \"$@\"" in text


def test_the_command_installs_shims_for_programs_on_path(setup, tmp_path):
    daemon, _, _, env = setup
    target = tmp_path / "mine"
    done = cli(daemon.path, "shims", "install", "--dir", str(target), env={**CHILD_ENV, **env})
    assert done.returncode == 0, done.stderr
    assert shims.installed(target) == ["fakebuild"]
    assert "export PATH=" in done.stdout
    listed = cli(daemon.path, "shims", "list", "--dir", str(target))
    assert listed.stdout.split() == ["fakebuild"]
    assert cli(daemon.path, "shims", "remove", "--dir", str(target)).returncode == 0
    assert shims.installed(target) == []


def test_an_empty_path_entry_is_the_current_directory(tmp_path, monkeypatch):
    shim_dir = tmp_path / "shims"
    shims.install(shim_dir, ["tool"], python=sys.executable)
    (tmp_path / "tool").write_text("#!/bin/sh\n")
    (tmp_path / "tool").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    assert shims.real("tool", f"{shim_dir}::/usr/bin", shim_dir) == "./tool"


def test_a_script_without_a_shebang_runs_through_sh(setup):
    _, _, real, env = setup
    (real / "fakebuild").write_text('echo "plain script, lease=${LEASEHOLD_LEASE_ID:-none}"\n')
    leased = run(env, "build")
    assert leased.returncode == 0, leased.stderr
    assert "plain script" in leased.stdout and "lease=none" not in leased.stdout
    unleased = run(env, "--version")
    assert unleased.returncode == 0 and "lease=none" in unleased.stdout


def test_an_agent_hooks_lease_for_this_program_covers_it_and_no_other(setup, tmp_path):
    """Under `leasehold run` with the Claude Code plugin, the hook has taken the command's lease
    before the shim runs: the shim runs inside it rather than asking for a second one."""
    from leasehold import hooknotes
    from leasehold.client import Client

    daemon, _, _, env = setup
    env = {**env, "XDG_RUNTIME_DIR": str(tmp_path)}
    notes = hooknotes.state_dir(env)
    renewer = subprocess.Popen(["sleep", "60"])  # stands in for the hook's renewer
    try:
        agent = os.getpid()  # this test is the agent: the shim runs under it
        hooknotes.write(notes, "cc.toolu_1", renewer.pid, agent, "fakebuild")
        done = run(env, "build")
        assert "lease=cc.toolu_1 weight=none" in done.stdout, done.stderr
        assert Client(daemon.path).leases()["leases"] == []  # no second lease was taken
        hooknotes.write(notes, "cc.toolu_1", renewer.pid, agent, "otherprog")
        assert "lease=cc.toolu_1" not in run(env, "build").stdout
        hooknotes.write(notes, "cc.toolu_1", renewer.pid, 1, "fakebuild")  # not our agent
        assert "lease=cc.toolu_1" not in run(env, "build").stdout
    finally:
        renewer.kill()
        renewer.wait()
    hooknotes.write(notes, "cc.toolu_1", renewer.pid, os.getpid(), "fakebuild")
    assert "lease=cc.toolu_1" not in run(env, "build").stdout  # its renewer is gone


def test_a_refused_run_exits_77_without_running_the_program(tmp_path, sock_dir):
    """A refusal (here: the user's submit rate in the pool) is a working daemon saying no: the
    shim does not run the program unleased, even with `when_busy = "run"`."""
    from leasehold.client import Client

    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "r.sock"),
        pools={"work": {"reserved": 2, "max": 2, "create_rate_per_min": 1}},
        kinds={"work": "work"},
        lend={"pools": []},
        commands={
            "defaults": False,
            "rule": [{"programs": ["fakebuild"], "args": "^build", "weight": 1}],
            "when_busy": "run",
        },
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        real = tmp_path / "bin"
        real.mkdir()
        (real / "fakebuild").write_text(FAKE)
        (real / "fakebuild").chmod(0o755)
        shim_dir = tmp_path / "shims"
        shims.install(shim_dir, ["fakebuild"], python=sys.executable)
        env = {
            "PATH": f"{shim_dir}:{real}:/usr/bin:/bin",
            "LEASEHOLD_SOCKET": str(daemon.path),
            "PYTHONPATH": str(ROOT / "src"),
            "HOME": str(tmp_path),
        }
        Client(daemon.path).acquire(kind="work", weight=1, lease_id="first")
        done = run(env, "build")
        assert done.returncode == 77
        assert done.stdout == ""  # the program never ran
        assert "not running fakebuild: refused:" in done.stderr
        assert "per-user limit" in done.stderr
    finally:
        daemon.stop()
