"""The GitHub runner hooks (`deploy/github-runner/`) as the runner runs them -- `sh` scripts with
the runner's environment -- against a real daemon: job-started takes the job's external lease
(waiting in line when the machine is busy) and returns, the lease outlives it, job-completed gives
it back; a lease job-completed never releases ends with the job's Runner.Worker; and with
leasehold missing or down, both fail open with a loud line."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import FACTS, SRC, FakeLoad, machine_config, until

from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.server import Daemon

HOOKS = Path(__file__).resolve().parent.parent / "deploy" / "github-runner"
JOB = {"RUNNER_NAME": "buildbox", "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
       "GITHUB_JOB": "tests", "GITHUB_REPOSITORY": "o/r"}  # fmt: skip
LEASE_ID = "gh.buildbox.123.2.tests"


@pytest.fixture
def daemon(tmp_path, sock_dir):
    """A 6-slot machine in place of the 2-slot one in conftest: room for a job's default
    weight-4 external lease."""
    config = machine_config(
        tmp_path, socket=str(sock_dir / "s.sock"), ceiling={"slots": 6},
        pools={"work": {"reserved": 1, "max": 6}, "external": {"reserved": 0, "max": 5},
               "lend": {"reserved": 0, "max": 1}},
    )  # fmt: skip
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        yield d
    finally:
        d.stop()


@pytest.fixture
def env(tmp_path, sock_dir):
    """The hooks' environment: a `leasehold` wrapper on the daemon's socket and a fake `logger`
    that records what would reach the journal."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "leasehold"
    wrapper.write_text(
        f'#!/bin/sh\nexec {sys.executable} -m leasehold --socket {sock_dir / "s.sock"} "$@"\n'
    )
    logger = bin_dir / "logger"
    logger.write_text(f'#!/bin/sh\nshift 4\nprintf "%s\\n" "$*" >> {tmp_path / "journal"}\n')
    for path in (wrapper, logger):
        path.chmod(0o755)
    return {
        **os.environ,
        **JOB,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHONPATH": str(SRC),
        "LEASEHOLD_RUNNER_MAX_HOLD": "60",
    }


def hook(name: str, env: dict, shell=("sh",), **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*shell, str(HOOKS / name)], env=env, capture_output=True, text=True, timeout=60,
        check=False, **kw,
    )  # fmt: skip


def leases(client: Client) -> dict[str, dict]:
    return {x["lease_id"]: x for x in client.leases()["leases"]}


def test_started_takes_the_jobs_lease_and_completed_gives_it_back(daemon, env):
    client = Client(daemon.path)
    done = hook("job-started.sh", env)
    assert done.returncode == 0, done.stderr
    held = leases(client)
    assert list(held) == [LEASE_ID]
    assert held[LEASE_ID]["kind"] == "external" and held[LEASE_ID]["weight"] == 4
    assert held[LEASE_ID]["labels"] == {
        "consumer": "github-runner", "runner": "buildbox", "repository": "o/r", "run": "123",
        "job": "tests",
    }  # fmt: skip
    time.sleep(1.0)
    assert list(leases(client)) == [LEASE_ID]  # the hook has exited; its renewer keeps it
    done = hook("job-completed.sh", env)
    assert done.returncode == 0, done.stderr
    assert leases(client) == {}


def test_a_busy_machine_holds_the_job_in_line_until_capacity_frees(daemon, env):
    client = Client(daemon.path)
    busy = client.acquire(kind="external", weight=5, lease_id="busy", ttl_s=600)
    assert busy["status"] == "granted"
    started = subprocess.Popen(
        ["sh", str(HOOKS / "job-started.sh")], env={**env, "LEASEHOLD_RUNNER_WEIGHT": "2"},
        stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: client.leases()["queue"] != [])
        assert started.poll() is None  # the job waits; it is not let through
        client.release("busy")
        _, err = started.communicate(timeout=30)
    finally:
        started.kill()
    assert started.returncode == 0, err
    assert "waiting for 2 slots (external): place 1" in err
    assert leases(client)[LEASE_ID]["weight"] == 2
    assert hook("job-completed.sh", env).returncode == 0
    assert leases(client) == {}


def test_a_lease_never_released_ends_with_the_runner_worker(daemon, env, tmp_path):
    """The renewer watches the job's Runner.Worker (found among the hook's ancestors): when it
    ends, renewals stop and the lease ends at its TTL."""
    client = Client(daemon.path)
    worker = tmp_path / "Runner.Worker"
    shutil.copy("/bin/sh", worker)
    script = f"sh {HOOKS / 'job-started.sh'}; sleep 1.5"
    proc = subprocess.Popen([str(worker), "-c", script], env=env, stderr=subprocess.PIPE)
    until(lambda: LEASE_ID in leases(client))
    proc.wait(timeout=30)
    # Gone within a TTL (the test machine's default, 15 s) once its Runner.Worker has ended.
    until(lambda: LEASE_ID not in leases(client), timeout=20)


@pytest.mark.parametrize("shell", [("sh",), ("bash", "-e")], ids=["sh", "bash-e"])
def test_missing_or_unreachable_leasehold_fails_open_loudly(env, tmp_path, sock_dir, shell):
    """Also under `bash -e`, which is how the GitHub runner runs a `.sh` hook."""
    # The daemon is not running: the socket does not exist.
    done = hook("job-started.sh", env, shell)
    assert done.returncode == 0
    assert "unreachable" in done.stderr and "WITHOUT a lease" in done.stderr
    assert "unreachable" in (tmp_path / "journal").read_text()
    assert hook("job-completed.sh", env, shell).returncode == 0
    # Not installed at all.
    absent = {**env, "LEASEHOLD": str(tmp_path / "no-such-leasehold")}
    done = hook("job-started.sh", absent, shell)
    assert done.returncode == 0 and "not installed" in done.stderr
    assert hook("job-completed.sh", absent, shell).returncode == 0
