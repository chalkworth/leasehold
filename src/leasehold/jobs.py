"""Sizing a command to its lease: job-count and thread-count variables, and a GNU make
jobserver.

A lease of weight W runs `jobs` parallel jobs (the daemon's `[jobs] per_slot`, by default one
per slot). `hold` tells its command in two ways:

* **Variables** many tools read (`JOB_VARIABLES`; `sized_env`). On by default; `hold
  --no-job-env` turns them off. They come in two groups. `WORKER_VARIABLES` say how many jobs or
  worker processes a tool fans out to, and are set to the job count. `THREAD_VARIABLES` limit
  the parallelism inside each process (OpenMP and BLAS pools, Go, Polars, rayon, Numba,
  process pools sized from the CPU count), and are set to the thread count: the job count,
  unless the command's `threads` setting (a `[commands]` rule's, or `hold --threads`) gives
  fewer. A command whose jobs are worker processes, as `pytest -n` runs, takes `threads = 1`,
  so its N workers run N threads between them rather than N each.

  A variable already set is the user's, and is left alone, unless the lease this command runs
  in set it to the value it still has. `LEASEHOLD_SET` records each name a lease set with its
  value (`OMP_NUM_THREADS=4,GOMAXPROCS=4`), and is read only beside `LEASEHOLD_LEASE_ID`. A
  user value equal to the recorded one cannot be told from the lease's. A nested lease sets the
  recorded names to its own values. When its thread count comes from the rules, it lowers each
  recorded thread variable to its own count and never raises one (`cap`); `hold --threads`
  sets its own count either way.

  A user's `OMP_NUM_THREADS` is copied to `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` where
  the user left them unset, because OpenBLAS and MKL read their own variable before
  `OMP_NUM_THREADS`. Only a plain count is copied. A user's `OPENBLAS_NUM_THREADS` or
  `MKL_NUM_THREADS` is left alone and changes nothing else. `LEASEHOLD_JOBS` and
  `LEASEHOLD_THREADS` are always the job count and the thread count this lease chose.
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
import re
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
    "LEASEHOLD_THREADS": "anything: the thread count leasehold chose",
    "OMP_NUM_THREADS": "OpenMP: numpy and other BLAS users, PyTorch, compilers with -fopenmp",
    "OPENBLAS_NUM_THREADS": "OpenBLAS (numpy's and scipy's wheels), before OMP_NUM_THREADS",
    "MKL_NUM_THREADS": "Intel MKL; PyTorch reads OMP_NUM_THREADS, then this overrides it",
    "RUST_TEST_THREADS": "test binaries built by cargo (cargo test runs them one at a time)",
    "GOMAXPROCS": "Go programs, go build and go test included",
    "POLARS_MAX_THREADS": "Polars",
    "RAYON_NUM_THREADS": "Rust programs using rayon",
    "NUMBA_NUM_THREADS": (
        "Numba, as a ceiling: numba.set_num_threads() above it is an error, which keeps the "
        "lease's limit"
    ),
    "LOKY_MAX_CPU_COUNT": "joblib and loky process pools: scikit-learn's n_jobs=-1",
    "DASK_NUM_WORKERS": "Dask's threaded and multiprocessing schedulers",
    "PYTHON_CPU_COUNT": (
        "Python 3.13 and later: os.cpu_count() and os.process_cpu_count(), and so "
        "multiprocessing, ProcessPoolExecutor and Dask's default worker count"
    ),
}

#: Every variable `job_env` may set.
JOB_VARIABLES: dict[str, str] = WORKER_VARIABLES | THREAD_VARIABLES

#: Each name a lease set for its command with the value it set, `NAME=VALUE` pairs joined by
#: commas: the lease's values, not the user's, while they are unchanged.
LEASEHOLD_SET = "LEASEHOLD_SET"

#: `OMP_NUM_THREADS`, and the variables a user's value of it is copied to.
BLAS_FAMILY = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")

#: Always this lease's own, and never listed in `LEASEHOLD_SET`.
_OWN = ("LEASEHOLD_JOBS", "LEASEHOLD_THREADS")

#: The most digits a count read from the environment may have; a longer value is not a count.
MAX_DIGITS = 6

#: One `LEASEHOLD_SET` pair: leasehold only ever sets counts.
_PAIR = re.compile(rf"([A-Z_][A-Z0-9_]*)=([0-9]{{1,{MAX_DIGITS}}})")

#: A command's `threads` setting: "jobs" (the job count) or a count of at least 1.
Threads = int | Literal["jobs"]

_JOBSERVER_FLAGS = ("--jobserver-auth=", "--jobserver-fds=")


def thread_count(threads: Threads, jobs: int) -> int:
    """The thread count `threads` gives a lease of `jobs` jobs: `jobs` for "jobs", else
    `threads`, never more than `jobs`."""
    if threads == "jobs":
        return jobs
    return max(1, min(int(threads), jobs))


def count_of(value: str | None) -> int | None:
    """`value` as a count of at least 1, of at most `MAX_DIGITS` digits; None otherwise. A
    longer string of digits is never converted."""
    if value is None or len(value) > MAX_DIGITS or not (value.isascii() and value.isdigit()):
        return None
    count = int(value)
    return count if count >= 1 else None


def recorded(env: Mapping[str, str]) -> dict[str, str]:
    """The names and values `env`'s `LEASEHOLD_SET` records, read only beside
    `LEASEHOLD_LEASE_ID`; empty when it is missing or any part of it is malformed."""
    if not env.get("LEASEHOLD_LEASE_ID"):
        return {}
    out: dict[str, str] = {}
    for part in env.get(LEASEHOLD_SET, "").split(","):
        if not part:
            continue
        pair = _PAIR.fullmatch(part)
        if pair is None:
            return {}
        out[pair[1]] = pair[2]
    return out


def leasehold_set(env: Mapping[str, str]) -> frozenset[str]:
    """The names whose value in `env` is the one its lease recorded setting (`recorded`)."""
    return frozenset(name for name, value in recorded(env).items() if env.get(name) == value)


def job_env(
    jobs: int, env: Mapping[str, str], threads: Threads = "jobs", *, cap: bool = False
) -> dict[str, str]:
    """The variables a lease of `jobs` jobs sets in `env`: each of `WORKER_VARIABLES` to `jobs`
    and each of `THREAD_VARIABLES` to `thread_count(threads, jobs)`, but not one the user set
    (present, and not at the value its lease recorded). The user's `OMP_NUM_THREADS`, when it
    is a plain count, goes to `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` instead. With `cap`,
    a thread variable the enclosing lease set keeps its value when that is lower.
    `LEASEHOLD_JOBS`, `LEASEHOLD_THREADS` and `LEASEHOLD_SET` are always set."""
    count = thread_count(threads, jobs)
    ours = leasehold_set(env)

    def users(name: str) -> bool:
        return bool(env.get(name)) and name not in ours

    def capped(value: str | None) -> int:
        current = count_of(value) if cap else None
        return count if current is None else min(current, count)

    omp = env["OMP_NUM_THREADS"] if users("OMP_NUM_THREADS") else None
    out: dict[str, str] = {}
    for name in JOB_VARIABLES:
        if name in _OWN or users(name):
            continue
        if name in WORKER_VARIABLES:
            out[name] = str(jobs)
        elif omp is not None and name in BLAS_FAMILY:
            copied = count_of(omp)
            if copied is not None:
                out[name] = str(copied)
            elif name in ours:
                out[name] = env[name]  # still the lease's: kept, and still recorded
        else:
            out[name] = str(capped(env[name] if name in ours else None))
    out[LEASEHOLD_SET] = ",".join(f"{name}={value}" for name, value in out.items())
    out["LEASEHOLD_JOBS"] = str(jobs)
    lease = env.get("LEASEHOLD_THREADS") if env.get("LEASEHOLD_LEASE_ID") else None
    out["LEASEHOLD_THREADS"] = str(capped(lease))
    return out


def sized_env(
    env: Mapping[str, str], jobs: int, threads: Threads = "jobs", *, cap: bool = False
) -> dict[str, str]:
    """`env` with `job_env` applied."""
    return {**env, **job_env(jobs, env, threads, cap=cap)}


def unsized_env(env: Mapping[str, str]) -> dict[str, str]:
    """`env` without an enclosing lease's sizing, for a command run with no job variables:
    every variable it set that still has its recorded value, and `LEASEHOLD_SET`,
    `LEASEHOLD_JOBS` and `LEASEHOLD_THREADS`."""
    drop = {*leasehold_set(env), LEASEHOLD_SET, *_OWN}
    return {name: value for name, value in env.items() if name not in drop}


def resized(env: Mapping[str, str], threads: Threads) -> dict[str, str] | None:
    """`env`, inside a lease that recorded the variables it set (`recorded`) and its job count
    (`LEASEHOLD_JOBS`), with each of the lease's thread variables lowered to the count
    `threads` gives that job count where it is higher; None when `env` is not such a
    lease's."""
    jobs = count_of(env.get("LEASEHOLD_JOBS"))
    if not recorded(env) or jobs is None:
        return None
    return sized_env(env, jobs, threads, cap=True)


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
