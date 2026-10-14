"""bench/run.py: the seeded plan, and a seconds-long run of one condition without systemd."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

RUN = Path(__file__).resolve().parent.parent / "bench" / "run.py"


def _bench(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), *args], capture_output=True, text=True, timeout=60, check=True
    )


def _plan(*args: str) -> dict:
    return json.loads(_bench("--dry-run", *args).stdout)


def test_the_plan_is_seeded_and_more_agents_keep_the_others_steps():
    four = _plan("--agents", "4", "--seed", "7")
    assert four == _plan("--agents", "4", "--seed", "7")
    assert four["plan"] != _plan("--agents", "4", "--seed", "8")["plan"]
    eight = _plan("--agents", "8", "--seed", "7")
    assert eight["plan"][:4] == four["plan"]
    assert len(eight["plan"]) == 8
    kinds = {kind for steps in eight["plan"] for _, kind in steps}
    assert kinds == {"cpu", "mem"}


def test_a_tiny_run_writes_jobs_probes_and_a_summary(tmp_path):
    _bench("--tiny", "--no-scope", "--agents", "1", "--conditions", "unmanaged",
           "--out", str(tmp_path))  # fmt: skip
    (outdir,) = tmp_path.iterdir()
    with (outdir / "summary.csv").open() as f:
        (row,) = list(csv.DictReader(f))
    assert row["condition"] == "unmanaged"
    assert int(row["jobs"]) >= 1
    assert row["failed"] == "0"
    assert float(row["wake_same_p50_ms"]) >= 0
    with (outdir / "unmanaged" / "jobs.csv").open() as f:
        jobs = list(csv.DictReader(f))
    assert all(float(j["started"]) >= float(j["issued"]) for j in jobs)
    assert "| unmanaged |" in (outdir / "summary.md").read_text()
