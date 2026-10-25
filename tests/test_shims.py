"""A PATH shim's contract with the agent that runs it (`leasehold.shims`), against a real daemon:
under an agent's tool timeout it gives up in time, and says who holds the machine, how long that
usually takes, and when to retry."""

from __future__ import annotations

import subprocess
import sys
import time

from support import CHILD_ENV

from leasehold.client import Client

#: What the holder's past runs took: the median of the clean ones is 10 minutes.
PAST_RUNS = [(600.0, 0), (590.0, 0), (610.0, 0), (5.0, 1)]


def test_a_shim_under_a_tool_timeout_refuses_in_time_with_when_to_retry(daemon, tmp_path):
    client = Client(daemon.path)
    past = {"rule": "cargo-test", "repo": "/src/app"}
    for wall_s, status in PAST_RUNS:
        done = client.acquire(kind="work", weight=1, ttl_s=30, wait_s=0)["lease"]["lease_id"]
        client.release(done, outcome=past | {"wall_s": wall_s, "exit_status": status})
    labels = past | {"cmd": "cargo test"}
    client.acquire(kind="work", weight=2, ttl_s=30, wait_s=0, labels=labels, session="other")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pytest").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "pytest").chmod(0o755)
    env = CHILD_ENV | {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "LEASEHOLD_SOCKET": str(daemon.path),
        "LEASEHOLD_TOOL_TIMEOUT_S": "4",
    }
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "leasehold.shims", str(tmp_path / "shims"), "pytest", "-q"],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 75
    assert time.monotonic() - started < 4  # within the agent's timeout
    said = done.stderr.splitlines()[-1]
    assert "cargo test (session other) holds 2 slots" in said
    assert "it usually takes ~10m)" in said
    assert "Retry in about 10 minutes" in said
