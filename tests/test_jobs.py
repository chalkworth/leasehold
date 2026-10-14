"""Sizing a command to its lease (leasehold.jobs, `hold --jobserver`): job-count variables, and
a GNU make jobserver that holds a whole build tree, sub-makes included, to the lease."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest
from support import CHILD_ENV, cli

from leasehold import jobs


def test_job_variables_are_set_only_where_unset():
    env = jobs.job_env(3, {"GOMAXPROCS": "8", "CARGO_BUILD_JOBS": ""})
    assert env["CARGO_BUILD_JOBS"] == "3" and env["LEASEHOLD_JOBS"] == "3"
    assert "GOMAXPROCS" not in env  # the user's own choice wins
    assert set(env) == set(jobs.JOB_VARIABLES) - {"GOMAXPROCS"}


def test_makeflags_drop_an_outer_jobserver_and_job_count():
    outer = "w -j8 --jobserver-auth=fifo:/tmp/outer -- FOO=1"
    flags = jobs.makeflags(outer, 2, "/tmp/mine")
    assert flags == "w -j2 --jobserver-auth=fifo:/tmp/mine -- FOO=1"  # options before "--"
    assert jobs.makeflags(None, 1, "/x") == "-j1 --jobserver-auth=fifo:/x"


def test_the_jobserver_holds_one_token_fewer_than_the_jobs_and_is_removed():
    with jobs.jobserver(4) as fifo:
        assert oct(fifo.parent.stat().st_mode & 0o777) == "0o700"
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        try:
            assert os.read(fd, 100) == b"+++"
        finally:
            os.close(fd)
    assert not fifo.parent.exists()


def test_hold_gives_its_command_the_job_count(daemon):
    shown = cli(
        daemon.path, "hold", "--weight", "2", "--",
        sys.executable, "-c", "import os; print(os.environ['LEASEHOLD_JOBS'], "
        "os.environ['CARGO_BUILD_JOBS'], os.environ.get('MAKEFLAGS'))",
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    assert shown.stdout.split() == ["2", "2", "None"]
    quiet = cli(
        daemon.path, "hold", "--no-job-env", "--",
        sys.executable, "-c", "import os; print(os.environ.get('LEASEHOLD_JOBS'))",
    )  # fmt: skip
    assert quiet.stdout.strip() == "None"


def _make_version() -> tuple[int, int]:
    if shutil.which("make") is None:
        return (0, 0)
    out = subprocess.run(["make", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"GNU Make (\d+)\.(\d+)", out)
    return (int(match[1]), int(match[2])) if match else (0, 0)


PROBE = """\
#!/bin/sh
# Note how many jobs run while this one does, then finish.
touch "$RUNNING/$1"
ls "$RUNNING" | wc -l >> "$LOG"
sleep 0.4
rm "$RUNNING/$1"
"""

MAKEFILE = """\
JOBS := a1 a2 a3 a4
all: $(JOBS) sub
$(JOBS):
\t@$(PROBE) $@
sub:
\t@$(MAKE) --no-print-directory -C sub
.PHONY: all sub $(JOBS)
"""

SUB = """\
JOBS := b1 b2 b3 b4
all: $(JOBS)
$(JOBS):
\t@$(PROBE) $@
.PHONY: all $(JOBS)
"""


@pytest.fixture
def build(tmp_path):
    (tmp_path / "running").mkdir()
    probe = tmp_path / "probe"
    probe.write_text(PROBE)
    probe.chmod(0o755)
    (tmp_path / "Makefile").write_text(MAKEFILE)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Makefile").write_text(SUB)
    env = CHILD_ENV | {
        "RUNNING": str(tmp_path / "running"),
        "LOG": str(tmp_path / "log"),
        "PROBE": str(probe),
    }
    env.pop("MAKEFLAGS", None)
    return tmp_path, env


def _most_at_once(path) -> int:
    return max(int(line) for line in (path / "log").read_text().split())


@pytest.mark.skipif(_make_version() < (4, 4), reason="needs GNU make 4.4 (fifo jobservers)")
def test_a_make_tree_under_a_weight_2_jobserver_runs_at_most_2_jobs(daemon, build):
    root, env = build
    # The control: unbounded, the same build runs many jobs at once, so the probe can see it.
    subprocess.run(["make", "-s", "-j8", "-C", str(root)], env=env, check=True, timeout=60)
    assert _most_at_once(root) > 2
    (root / "log").unlink()
    held = cli(
        daemon.path, "hold", "--weight", "2", "--jobserver", "--",
        "make", "-s", "-C", str(root), env=env,
    )  # fmt: skip
    assert held.returncode == 0, held.stderr
    assert len((root / "log").read_text().split()) == 8  # every job ran, sub-make's included
    assert _most_at_once(root) <= 2


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("-j 8 --warn-undefined-variables", "--warn-undefined-variables"),
        ("--jobs 8 -k", "-k"),
        ("--jobs=8 -k", "-k"),
        ("-j8 -k", "-k"),
        ("-j -k", "-k"),
        ("ks -j4 --jobserver-auth=fifo:/tmp/other", "ks"),
        ("--jobserver-fds=3,4 -j", ""),
    ],
)
def test_makeflags_drops_every_form_of_an_outer_job_count(given, kept, tmp_path):
    fifo = tmp_path / "fifo"
    ours = f"-j2 --jobserver-auth=fifo:{fifo}"
    assert jobs.makeflags(given, 2, fifo) == f"{kept} {ours}".strip()


def test_makeflags_keeps_assignments_and_escaped_spaces(tmp_path):
    fifo = tmp_path / "fifo"
    given = r"-k -j8 -- CFLAGS=-O2\ -g NAME=a\ b"
    assert jobs.makeflags(given, 3, fifo) == (
        rf"-k -j3 --jobserver-auth=fifo:{fifo} -- CFLAGS=-O2\ -g NAME=a\ b"
    )
