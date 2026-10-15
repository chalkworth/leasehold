"""Sizing a command to its lease: job-count and thread-count variables, and a GNU make
jobserver.

A lease of weight W runs `jobs` parallel jobs (the daemon's `[jobs] per_slot`, by default one
per slot). `hold` tells its command in two ways:

* **Variables** many tools read (`JOB_VARIABLES`; `sized_env`). On by default; `hold
  --no-job-env` turns them off. They come in two groups. `WORKER_VARIABLES` say how many jobs or
  worker processes a tool fans out to, and are set to the job count. `THREAD_VARIABLES` limit
  the parallelism inside each process (OpenMP and BLAS pools, Go, Polars, rayon, process
  pools sized from the CPU count), and are set to the thread count: the job count, unless the
  command's `threads` setting (a `[commands]` rule's, or `hold --threads`) gives fewer. A
  command whose jobs are worker processes, as `pytest -n` runs, takes `threads = 1`, so its N
  workers run N threads between them rather than N each.

  A value the user chose wins: a variable already set, unless an enclosing lease set it,
  which `LEASEHOLD_SET` records. A nested lease sets the names listed there to its own
  values. `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` are one family
  (OpenBLAS reads its own before OpenMP's, and MKL's overrides OpenMP's in PyTorch): when the
  user set any of them, none of them is set. `LEASEHOLD_JOBS` and `LEASEHOLD_THREADS` are
  always this lease's: the job count, and the thread count in effect (the user's value of the
  family when they set one: `OMP_NUM_THREADS`, else `OPENBLAS_NUM_THREADS`, else
  `MKL_NUM_THREADS`).
* **A jobserver** (`hold --jobserver`): a named pipe holding `jobs - 1` tokens, announced in
  `MAKEFLAGS` as `--jobserver-auth=fifo:PATH`, the protocol of GNU make 4.4. make, ninja 1.13
  and later, cargo and rustc, and GCC's LTO take a token before each extra job, so the whole tree
  of builds under the command, sub-makes included, runs at most `jobs` at once. A tool that does
  not speak the protocol (or make before 4.4, which warns and runs serially) still has the
  variables. The command must not pass its own `-jN`: make then runs its own jobserver.

Prior art: steve and guildmaster run one jobserver for a whole machine; here the jobserver is
per lease, so it follows what the machine's daemon granted.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Literal

#: Variables set to the job count, and who reads each: how many jobs or worker processes a
#: tool fans out to.
WORKER_VARIABLES: dict[str, str] = {
    "LEASEHOLD_JOBS": "anything: the job count itself",
    "CARGO_BUILD_JOBS": "cargo build, test and check",
    "CMAKE_BUILD_PARALLEL_LEVEL": "cmake --build",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "pytest -n auto (pytest-xdist)",
}

#: Variables set to the thread count (`thread_count`), and who reads each: the parallelism
#: inside each process.
THREAD_VARIABLES: dict[str, str] = {
    "LEASEHOLD_THREADS": "anything: the thread count in effect",
    "OMP_NUM_THREADS": "OpenMP: numpy and other BLAS users, PyTorch, compilers with -fopenmp",
    "OPENBLAS_NUM_THREADS": "OpenBLAS (numpy's and scipy's wheels), before OMP_NUM_THREADS",
    "MKL_NUM_THREADS": "Intel MKL; PyTorch reads OMP_NUM_THREADS, then this overrides it",
    "RUST_TEST_THREADS": "test binaries built by cargo (cargo test runs them one at a time)",
    "GOMAXPROCS": "Go programs, go build and go test included",
    "POLARS_MAX_THREADS": "Polars",
    "RAYON_NUM_THREADS": "Rust programs using rayon",
    "LOKY_MAX_CPU_COUNT": "joblib and loky process pools: scikit-learn's n_jobs=-1",
    "DASK_NUM_WORKERS": "Dask's threaded and multiprocessing schedulers",
    "PYTHON_CPU_COUNT": (
        "Python 3.13 and later: os.cpu_count() and os.process_cpu_count(), and so "
        "multiprocessing, ProcessPoolExecutor and Dask's default worker count"
    ),
}

#: Every variable `job_env` may set.
JOB_VARIABLES: dict[str, str] = WORKER_VARIABLES | THREAD_VARIABLES

#: Names a lease set for its command, comma-separated: an enclosing lease's values, not the
#: user's.
LEASEHOLD_SET = "LEASEHOLD_SET"

#: Variables the user's value of any one of decides all of: none is set beside it.
BLAS_FAMILY = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")

#: Always this lease's own, and never listed in `LEASEHOLD_SET`.
_OWN = ("LEASEHOLD_JOBS", "LEASEHOLD_THREADS")

#: A command's `threads` setting: "jobs" (the job count) or a count of at least 1.
Threads = int | Literal["jobs"]

_JOBSERVER_FLAGS = ("--jobserver-auth=", "--jobserver-fds=")


def thread_count(threads: Threads, jobs: int) -> int:
    """The thread count `threads` gives a lease of `jobs` jobs: `jobs` for "jobs", else
    `threads`, never more than `jobs`."""
    if threads == "jobs":
        return jobs
    return max(1, min(int(threads), jobs))


def leasehold_set(env: Mapping[str, str]) -> frozenset[str]:
    """The names `env`'s `LEASEHOLD_SET` lists: values an enclosing lease set."""
    return frozenset(name for name in env.get(LEASEHOLD_SET, "").split(",") if name)


def _plan(jobs: int, env: Mapping[str, str], threads: Threads) -> tuple[dict[str, str], list[str]]:
    """What a lease of `jobs` jobs sets in `env`, and the enclosing lease's names it removes
    (the BLAS family's, when the user set one of it)."""
    count = thread_count(threads, jobs)
    inherited = leasehold_set(env)

    def users(name: str) -> bool:
        return bool(env.get(name)) and name not in inherited

    family = next((env[name] for name in BLAS_FAMILY if users(name)), None)
    out: dict[str, str] = {}
    removed: list[str] = []
    for name in JOB_VARIABLES:
        if name in _OWN:
            continue
        if users(name):
            continue
        if family is not None and name in BLAS_FAMILY:
            if name in env:
                removed.append(name)
            continue
        out[name] = str(jobs if name in WORKER_VARIABLES else count)
    out[LEASEHOLD_SET] = ",".join(out)
    out["LEASEHOLD_JOBS"] = str(jobs)
    out["LEASEHOLD_THREADS"] = family if family is not None else str(count)
    return out, removed


def job_env(jobs: int, env: Mapping[str, str], threads: Threads = "jobs") -> dict[str, str]:
    """The variables a lease of `jobs` jobs sets in `env`: each of `WORKER_VARIABLES` to `jobs`
    and each of `THREAD_VARIABLES` to `thread_count(threads, jobs)`, but not one the user set
    (present and not in `LEASEHOLD_SET`), nor the BLAS family when the user set one of it;
    `LEASEHOLD_JOBS`, `LEASEHOLD_THREADS` and `LEASEHOLD_SET` always."""
    return _plan(jobs, env, threads)[0]


def sized_env(env: Mapping[str, str], jobs: int, threads: Threads = "jobs") -> dict[str, str]:
    """`env` with `job_env` applied, and without an enclosing lease's BLAS family values when
    the user set one of the family."""
    out, removed = _plan(jobs, env, threads)
    sized = {name: value for name, value in env.items() if name not in removed}
    return sized | out


def resized(env: Mapping[str, str], threads: Threads) -> dict[str, str] | None:
    """`env`, inside a lease that set its variables (`LEASEHOLD_SET` and `LEASEHOLD_JOBS`), with
    the thread count `threads` gives that lease's job count in the variables the lease set; None
    when `env` is not such a lease's."""
    if LEASEHOLD_SET not in env:
        return None
    try:
        jobs = int(env.get("LEASEHOLD_JOBS", ""))
    except ValueError:
        return None
    if jobs < 1:
        return None
    return sized_env(env, jobs, threads)


def _words(flags: str) -> list[str]:
    """`MAKEFLAGS` split as make splits it: at whitespace a backslash does not escape. Each word
    keeps its backslashes, so joining the words with spaces gives the same flags back."""
    words, word, escaped = [], "", False
    for char in flags:
        if escaped:
            word, escaped = word + char, False
        elif char == "\\":
            word, escaped = word + char, True
        elif char.isspace():
            if word:
                words.append(word)
            word = ""
        else:
            word += char
    if word:
        words.append(word)
    return words


def makeflags(existing: str | None, jobs: int, fifo: Path) -> str:
    """`MAKEFLAGS` that hand make this jobserver: `existing` without its own job count or
    jobserver (an outer make's would let the command run past the lease), then ours. Removed
    are `-j`, `-jN`, `-j N`, `--jobs`, `--jobs=N`, `--jobs N` and `--jobserver-auth`/`-fds`;
    everything else, and every word after `--` (variable assignments), is kept as it was."""
    words = _words(existing or "")
    kept: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            kept += words[i - 1 :]
            break
        if word.startswith(_JOBSERVER_FLAGS) or word.startswith("--jobs="):
            continue
        if word in ("-j", "--jobs"):
            if i < len(words) and words[i].isdigit():
                i += 1  # the count given as its own word
            continue
        if word.startswith("-j") and word[2:].isdigit():
            continue
        kept.append(word)
    flags = [f"-j{jobs}", f"--jobserver-auth=fifo:{fifo}"]
    if "--" in kept:  # ours go before the assignments
        at = kept.index("--")
        return " ".join([*kept[:at], *flags, *kept[at:]])
    return " ".join([*kept, *flags])


@contextlib.contextmanager
def jobserver(jobs: int) -> Iterator[Path]:
    """A fifo jobserver for `jobs` jobs: the command itself holds the implicit first token, so
    the fifo holds `jobs - 1`. In a directory only this user may enter; removed afterwards."""
    directory = Path(tempfile.mkdtemp(prefix="leasehold-jobs-"))
    try:
        fifo = directory / "fifo"
        os.mkfifo(fifo, 0o600)
        # Open read-write without blocking, so the tokens can be written before any reader
        # exists, and so the fifo never sees end-of-file while the command runs.
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                raise OSError(f"{fifo} is not a fifo")
            os.write(fd, b"+" * (jobs - 1))
            yield fifo
        finally:
            os.close(fd)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
