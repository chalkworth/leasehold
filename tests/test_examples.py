"""The examples under `examples/`, run as their READMEs say: each script against a test daemon
on a temporary socket, with `leasehold` on PATH, and what it prints checked. A daemon takes the
example's own `machine.toml` where it has one, with only its paths, its tick and (for lending)
its quiet period changed, so the tests stay fast."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from support import FACTS, SRC, FakeLoad, machine_config, until

from leasehold import machine
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.machine import MachineConfig
from leasehold.server import Daemon

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def example_config(name: str) -> MachineConfig:
    return machine.load(EXAMPLES / name / "machine.toml")


@pytest.fixture
def serve(tmp_path, sock_dir):
    """Start a daemon on a config, on this test's socket and ledger; its socket path."""
    daemons: list[Daemon] = []

    def start(config: MachineConfig) -> Path:
        config = config.model_copy(
            update={
                "socket": sock_dir / "s.sock",
                "ledger_dir": tmp_path,
                "tick_s": 0.1,
                "mode": "user",
            }
        )
        daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
        daemon.start()
        daemons.append(daemon)
        return daemon.path

    yield start
    for daemon in daemons:
        daemon.stop()


@pytest.fixture
def bin_dir(tmp_path):
    """`leasehold` for the scripts to call, and a `logger` that keeps the runner hooks' lines
    out of the real journal."""
    path = tmp_path / "bin"
    path.mkdir()
    for name, text in {
        "leasehold": f'#!/bin/sh\nexec {sys.executable} -m leasehold "$@"\n',
        "logger": f'#!/bin/sh\nshift 4\nprintf "%s\\n" "$*" >> {tmp_path / "journal"}\n',
    }.items():
        (path / name).write_text(text)
        (path / name).chmod(0o755)
    return path


def script_env(bin_dir: Path, sock: Path, **extra: str) -> dict[str, str]:
    """The environment a script runs with: the daemon's socket, our `leasehold` and this
    interpreter's `python3` first on PATH, and nothing that would change what it does."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MAKEFLAGS", "MFLAGS", "LEASEHOLD_SESSION", "LEASEHOLD_CONFIG")
    }
    python_dir = Path(sys.executable).parent
    return env | {
        "PATH": f"{bin_dir}:{python_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(SRC),
        "LEASEHOLD_SOCKET": str(sock),
        **extra,
    }


def run(script: Path, env: dict, *, orphan: bool = False, **kw) -> subprocess.CompletedProcess:
    """`script` run to its end. With `orphan`, its parent exits at once (`support.Orphan`), and
    it is done when its output closes; its exit code is then not known."""
    argv = ["sh", str(script)]
    if orphan:
        argv = ["sh", "-c", 'sh "$0" </dev/null & exit 0', str(script)]
    done = subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=60, check=False, **kw
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done


def test_every_machine_toml_is_a_machine_config():
    found = sorted(EXAMPLES.rglob("machine.toml"))
    assert len(found) >= 3
    for path in found:
        assert isinstance(machine.load(path), MachineConfig), path


def test_every_example_has_a_readme_and_its_scripts_are_executable():
    for example in (p for p in EXAMPLES.iterdir() if p.is_dir()):
        assert (example / "README.md").is_file(), example
        for script in example.glob("*.sh"):
            assert os.access(script, os.X_OK), script


# --- library ---------------------------------------------------------------------------------


def test_library_prints_what_its_readme_shows():
    shown = subprocess.run(
        [sys.executable, str(EXAMPLES / "library" / "decide.py")],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    readme = (EXAMPLES / "library" / "README.md").read_text()
    expected = re.search(r"The output:\n\n```\n(.*?)```", readme, re.S)
    assert expected is not None
    assert shown.stdout == expected[1]


# --- test-suite ------------------------------------------------------------------------------


def _make_version() -> tuple[int, int]:
    if shutil.which("make") is None:
        return (0, 0)
    out = subprocess.run(["make", "--version"], capture_output=True, text=True).stdout
    found = re.search(r"GNU Make (\d+)\.(\d+)", out)
    return (int(found[1]), int(found[2])) if found else (0, 0)


def _most(output: str, label: str) -> int:
    found = re.search(rf"^{re.escape(label)}: at most (\d+) ", output, re.M)
    assert found is not None, output
    return int(found[1])


@pytest.mark.skipif(_make_version() < (4, 4), reason="needs GNU make 4.4 (fifo jobservers)")
def test_test_suite_runs_at_most_two_jobs_under_a_weight_2_lease(serve, bin_dir, tmp_path):
    sock = serve(machine_config(tmp_path, ceiling={"slots": 4}))
    out = run(EXAMPLES / "test-suite" / "run.sh", script_env(bin_dir, sock)).stdout
    assert 1 <= _most(out, "make under --jobserver") <= 2
    assert 1 <= _most(out, "make -j$LEASEHOLD_JOBS") <= 2
    try:
        import xdist  # noqa: F401
    except ImportError:
        assert "pytest-xdist is not installed: skipped pytest -n auto" in out
    else:
        assert 1 <= _most(out, "pytest -n auto") <= 2


# --- lend ------------------------------------------------------------------------------------


def test_lend_grants_idle_capacity_and_revokes_it_when_work_arrives(serve, bin_dir):
    config = example_config("lend")
    # The example waits 10 s of quiet before lending; the test does not.
    config = config.model_copy(update={"lend": config.lend.model_copy(update={"quiet_s": 0})})
    sock = serve(config)
    done = run(EXAMPLES / "lend" / "run.sh", script_env(bin_dir, sock))
    out = done.stdout + done.stderr
    assert "lend open: idle for" in out
    assert re.search(r"^  lend +2/2 held", out, re.M)
    assert "lease revoked (non-lend work waiting); stopping the command" in out
    assert "the owner's build runs on all 4 slots" in out
    assert "the batch job's hold exited 76 (76: revoked)" in out
    assert done.stdout.rstrip().endswith("lending off: lending switched off")


@pytest.mark.skipif(shutil.which("stress-ng") is None, reason="needs stress-ng")
def test_the_lent_job_is_stress_ng_when_it_is_installed(tmp_path):
    job = subprocess.Popen(
        ["sh", str(EXAMPLES / "lend" / "batch-job.sh")],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert job.stdout.readline() == "batch job started\n"
        comm = Path(f"/proc/{job.pid}/comm")
        until(lambda: comm.read_text().strip() == "stress-ng")
    finally:
        os.killpg(job.pid, signal.SIGTERM)
        job.wait(timeout=10)


# --- observed --------------------------------------------------------------------------------


def test_observed_charges_the_running_program_and_frees_its_slots_when_it_ends(serve, bin_dir):
    sock = serve(example_config("observed"))
    toml = str(EXAMPLES / "observed" / "machine.toml")
    env = script_env(bin_dir, sock, LEASEHOLD_CONFIG=toml)
    done = run(EXAMPLES / "observed" / "run.sh", env, orphan=True)  # observed: under no `hold`
    out = done.stdout + done.stderr
    assert "data-prep: weight 3 each, 1 running now" in out
    assert re.search(r"^  observed +3/4 held .* 1 running", out, re.M)
    assert re.search(r"^observed\.data-prep\.\d+\.\d+ +observed +observed +3 ", out, re.M)
    assert "hold exited 75 (75: still waiting at --timeout)" in out
    assert "not reached" not in done.stdout
    assert done.stdout.rstrip().endswith("the work runs once the data prep has ended")


# --- agents ----------------------------------------------------------------------------------


def test_agents_sessions_take_turns(serve, bin_dir):
    sock = serve(example_config("agents"))
    out = run(EXAMPLES / "agents" / "run.sh", script_env(bin_dir, sock)).stdout
    places = re.findall(r" (agent-[abc]) .*queued #(\d)", out)  # listed in line order
    assert places == [("agent-a", "1"), ("agent-b", "2"), ("agent-c", "3"), ("agent-a", "4"),
                      ("agent-a", "5")]  # fmt: skip
    assert out.rstrip().endswith("ran in this order: a1 b1 c1 a2 a3")


# --- github-runner ---------------------------------------------------------------------------


def runner_machine(tmp_path) -> MachineConfig:
    """Room for a job's default weight-4 external lease."""
    return machine_config(
        tmp_path, ceiling={"slots": 6},
        pools={"work": {"reserved": 1, "max": 6}, "external": {"reserved": 0, "max": 5},
               "lend": {"reserved": 0, "max": 1}},
    )  # fmt: skip


def test_github_runner_hooks_take_and_give_back_the_jobs_lease(serve, bin_dir, tmp_path):
    sock = serve(runner_machine(tmp_path))
    # The renewer job-started.sh leaves behind stops by itself soon after the daemon has gone.
    env = script_env(bin_dir, sock, LEASEHOLD_RUNNER_MAX_HOLD="10")
    done = run(EXAMPLES / "github-runner" / "try-hooks.sh", env)
    assert "lease gh.buildbox-1.1001.1.test held (external, weight 2)" in done.stderr
    assert "lease gh.buildbox-1.1001.1.test released" in done.stderr
    tables = done.stdout.split("LEASE ")
    assert len(tables) == 3, done.stdout
    assert re.search(r"^gh\.buildbox-1\.1001\.1\.test +external +external +2 ", tables[1], re.M)
    assert tables[2].strip().splitlines()[1:] == []  # released: nothing listed


def test_github_runner_ephemeral_registers_then_runs_under_a_lease(serve, bin_dir, tmp_path):
    sock = serve(runner_machine(tmp_path))
    runner = tmp_path / "runner"
    runner.mkdir()
    fakes = {
        bin_dir / "gh": "#!/bin/sh\necho token-123\n",
        runner / "config.sh": f'#!/bin/sh\necho "$@" > {tmp_path / "config.args"}\n',
        runner / "run.sh": (
            "#!/bin/sh\n"
            'echo "run.sh: pool $LEASEHOLD_POOL weight $LEASEHOLD_WEIGHT lease $LEASEHOLD_LEASE_ID"'
            "\nleasehold leases\n"
        ),
    }
    for path, text in fakes.items():
        path.write_text(text)
        path.chmod(0o755)
    env = script_env(bin_dir, sock, RUNNER_REPO="acme/api")
    done = run(EXAMPLES / "github-runner" / "ephemeral.sh", env, cwd=runner)
    args = (tmp_path / "config.args").read_text().split()
    assert "--ephemeral" in args and "--unattended" in args
    assert args[args.index("--token") + 1] == "token-123"
    assert args[args.index("--url") + 1] == "https://github.com/acme/api"
    assert "run.sh: pool external weight 4 lease " in done.stdout
    assert re.search(r" external +external +4 ", done.stdout)
    until(lambda: _no_leases(sock))  # given back when run.sh ended


def _no_leases(sock: Path) -> bool:
    return Client(sock).leases()["leases"] == []
