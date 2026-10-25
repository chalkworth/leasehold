"""The GitHub Actions runner hooks (`deploy/github-runner/`) against a real daemon: with their
default weight a job is leased even in a small `external` pool, and the completed hook says
whether it gave back a lease."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from support import CHILD_ENV

from leasehold.client import Client

HOOKS = Path(__file__).resolve().parent.parent / "deploy" / "github-runner"


def run_hook(tmp_path, sock, name: str) -> str:
    """`name` the way the runner runs a hook (`bash -e`), with `leasehold` as this checkout's;
    what it says."""
    wrapper = tmp_path / "leasehold"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} -m leasehold "$@"\n')
    wrapper.chmod(0o755)
    env = CHILD_ENV | {
        "LEASEHOLD": str(wrapper),
        "LEASEHOLD_SOCKET": str(sock),
        "RUNNER_NAME": "r",
        "GITHUB_JOB": "j",
        "GITHUB_RUN_ID": "0",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    done = subprocess.run(
        ["bash", "-e", str(HOOKS / name)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0
    return done.stderr


def test_a_job_is_leased_by_default_and_the_completed_hook_says_what_it_released(daemon, tmp_path):
    client = Client(daemon.path)
    assert "held (external" in run_hook(tmp_path, daemon.path, "job-started.sh")
    assert [x["lease_id"] for x in client.leases()["leases"]] == ["gh.r.0.1.j"]
    assert "lease gh.r.0.1.j released" in run_hook(tmp_path, daemon.path, "job-completed.sh")
    assert client.leases()["leases"] == []
    assert "held no lease" in run_hook(tmp_path, daemon.path, "job-completed.sh")
