"""Sizing a command to its lease: job-count variables, and a GNU make jobserver.

A lease of weight W runs `jobs` parallel jobs (the daemon's `[jobs] per_slot`, by default one
per slot). `hold` tells its command in two ways:

* **Variables** many tools read, set only where the environment leaves them unset, so a value
  the user chose wins: `JOB_VARIABLES`. On by default; `hold --no-job-env` turns them off.
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

#: Variables set to the job count when unset, and who reads each.
JOB_VARIABLES: dict[str, str] = {
    "LEASEHOLD_JOBS": "anything: the job count itself",
    "CARGO_BUILD_JOBS": "cargo build, test and check",
    "CMAKE_BUILD_PARALLEL_LEVEL": "cmake --build",
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "pytest -n auto (pytest-xdist)",
    "RUST_TEST_THREADS": "test binaries built by cargo",
    "GOMAXPROCS": "Go programs",
    "OMP_NUM_THREADS": "OpenMP: numpy and other BLAS users, compilers with -fopenmp",
}

_JOBSERVER_FLAGS = ("--jobserver-auth=", "--jobserver-fds=")


def job_env(jobs: int, env: Mapping[str, str]) -> dict[str, str]:
    """The `JOB_VARIABLES` that `env` leaves unset, each set to `jobs`."""
    return {name: str(jobs) for name in JOB_VARIABLES if not env.get(name)}


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
