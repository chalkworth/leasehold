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
    assert set(env) == set(jobs.JOB_VARIABLES) - {"GOMAXPROCS"} | {"LEASEHOLD_SET"}


def test_worker_counts_get_the_jobs_and_everything_inside_a_process_the_thread_count():
    assert set(jobs.WORKER_VARIABLES) == {
        "LEASEHOLD_JOBS", "CARGO_BUILD_JOBS", "CMAKE_BUILD_PARALLEL_LEVEL",
        "PYTEST_XDIST_AUTO_NUM_WORKERS",
    }  # fmt: skip
    assert {"RUST_TEST_THREADS", "GOMAXPROCS", "DASK_NUM_WORKERS", "LOKY_MAX_CPU_COUNT",
            "PYTHON_CPU_COUNT", "OMP_NUM_THREADS", "NUMBA_NUM_THREADS"} <= set(
        jobs.THREAD_VARIABLES
    )  # fmt: skip
    assert jobs.JOB_VARIABLES == jobs.WORKER_VARIABLES | jobs.THREAD_VARIABLES
    env = jobs.job_env(4, {}, threads=1)
    assert env["PYTEST_XDIST_AUTO_NUM_WORKERS"] == env["CARGO_BUILD_JOBS"] == "4"
    assert {n for n, v in env.items() if v == "1"} == set(jobs.THREAD_VARIABLES)
    every = set(jobs.JOB_VARIABLES)
    assert {jobs.job_env(4, {})[n] for n in every} == {"4"}  # "jobs", the default


#: Inside a lease, as `hold` leaves its command.
LEASED = {"LEASEHOLD_LEASE_ID": "abc"}


def test_the_users_blas_value_is_given_to_the_siblings_they_left_unset():
    """OpenBLAS reads OPENBLAS_NUM_THREADS before OMP_NUM_THREADS, and MKL_NUM_THREADS
    overrides OMP_NUM_THREADS in PyTorch: siblings at another value would undo the user's."""
    env = jobs.job_env(4, {"OMP_NUM_THREADS": "6"}, threads=1)
    assert "OMP_NUM_THREADS" not in env
    assert env["OPENBLAS_NUM_THREADS"] == env["MKL_NUM_THREADS"] == env["LEASEHOLD_THREADS"] == "6"
    assert "OPENBLAS_NUM_THREADS=6" in env["LEASEHOLD_SET"].split(",")
    assert env["POLARS_MAX_THREADS"] == env["GOMAXPROCS"] == "1"
    env = jobs.job_env(4, {"MKL_NUM_THREADS": "3", "OPENBLAS_NUM_THREADS": "2"})
    assert env["OMP_NUM_THREADS"] == env["LEASEHOLD_THREADS"] == "2"
    assert "MKL_NUM_THREADS" not in env  # the user's own


def test_a_user_blas_value_that_is_not_a_count_is_not_copied_nor_reported():
    env = jobs.job_env(4, {"OMP_NUM_THREADS": "4,2"}, threads=2)
    assert not {"OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"} & set(env)
    assert env["LEASEHOLD_THREADS"] == "2"  # always a count: the lease's


def test_a_nested_lease_overwrites_the_enclosing_leases_values_and_never_the_users():
    outer = jobs.sized_env({"PATH": "/bin", "GOMAXPROCS": "8"} | LEASED, 4)
    assert jobs.recorded(outer)["OMP_NUM_THREADS"] == "4"
    assert "GOMAXPROCS" not in jobs.recorded(outer)
    inner = jobs.sized_env(outer, 2, threads=1)
    assert inner["OMP_NUM_THREADS"] == inner["LEASEHOLD_THREADS"] == "1"
    assert inner["CARGO_BUILD_JOBS"] == inner["LEASEHOLD_JOBS"] == "2"
    assert inner["GOMAXPROCS"] == "8"  # the user's, through both


def test_a_value_the_user_set_inside_a_lease_is_theirs():
    """`export OMP_NUM_THREADS=3` (or `OMP_NUM_THREADS=3 cmd`) inside a lease that set 4."""
    outer = jobs.sized_env(LEASED, 4)
    inner = jobs.sized_env(outer | {"OMP_NUM_THREADS": "3"}, 2, threads=1)
    assert inner["OMP_NUM_THREADS"] == "3"
    assert inner["OPENBLAS_NUM_THREADS"] == inner["MKL_NUM_THREADS"] == "3"
    assert inner["LEASEHOLD_THREADS"] == "3" and inner["RAYON_NUM_THREADS"] == "1"
    # A lease's value the user unset is set again, as any unset variable is.
    gone = {k: v for k, v in outer.items() if k != "GOMAXPROCS"}
    assert jobs.sized_env(gone, 2)["GOMAXPROCS"] == "2"


@pytest.mark.parametrize(
    "recorded",
    ["OMP_NUM_THREADS", "OMP_NUM_THREADS=4,junk", "OMP_NUM_THREADS=4;GOMAXPROCS=4",
     "OMP_NUM_THREADS=four", "omp_num_threads=4"],
)  # fmt: skip
def test_a_malformed_leasehold_set_records_nothing(recorded):
    env = LEASED | {"OMP_NUM_THREADS": "4", "LEASEHOLD_SET": recorded}
    assert jobs.recorded(env) == {}
    assert "OMP_NUM_THREADS" not in jobs.job_env(2, env)  # the user's, then


def test_a_leasehold_set_without_a_lease_or_stale_is_not_believed():
    spoofed = {"OMP_NUM_THREADS": "4", "LEASEHOLD_SET": "OMP_NUM_THREADS=4"}
    assert "OMP_NUM_THREADS" not in jobs.job_env(2, spoofed)  # no LEASEHOLD_LEASE_ID
    stale = LEASED | spoofed | {"OMP_NUM_THREADS": "5"}
    assert "OMP_NUM_THREADS" not in jobs.job_env(2, stale)  # changed since: the user's


def test_without_job_variables_no_sizing_is_passed_on():
    outer = jobs.sized_env(LEASED | {"PATH": "/bin"}, 4)
    bare = jobs.unsized_env(outer)
    assert not {"LEASEHOLD_SET", "LEASEHOLD_JOBS", "LEASEHOLD_THREADS"} & set(bare)
    assert bare["PATH"] == "/bin"


def test_resized_gives_the_leases_variables_a_thread_count_and_keeps_its_jobs():
    outer = jobs.sized_env(LEASED | {"OPENBLAS_NUM_THREADS": "3"}, 4)
    inside = jobs.resized(outer, 1)
    assert inside["RAYON_NUM_THREADS"] == inside["PYTHON_CPU_COUNT"] == "1"
    assert inside["PYTEST_XDIST_AUTO_NUM_WORKERS"] == inside["LEASEHOLD_JOBS"] == "4"
    assert inside["OPENBLAS_NUM_THREADS"] == inside["LEASEHOLD_THREADS"] == "3"
    assert inside["OMP_NUM_THREADS"] == "3"
    assert jobs.resized({"OMP_NUM_THREADS": "2"}, 1) is None  # no lease set anything


def test_the_thread_count_is_the_jobs_or_fewer():
    assert jobs.thread_count("jobs", 6) == 6
    assert jobs.thread_count(2, 6) == 2
    assert jobs.thread_count(8, 6) == 6


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
