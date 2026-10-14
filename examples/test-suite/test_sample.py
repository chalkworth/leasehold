"""Six tests for `pytest -n auto`, each one job of the suite (job.sh). run.sh runs them; job.sh
needs the RUNNING and LOG it sets."""

import subprocess
from pathlib import Path

import pytest

JOB = Path(__file__).with_name("job.sh")


@pytest.mark.parametrize("n", range(1, 7))
def test_job(n):
    subprocess.run([str(JOB), f"pytest-{n}"], check=True)
