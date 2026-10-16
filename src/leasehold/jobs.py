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
  fewer. A pytest run with N xdist workers takes the job count divided by N, so the workers
  share the lease's threads.

  A variable already set is the user's, and is left alone, unless the enclosing lease set it to
  the value it still has. `LEASEHOLD_SET` records each name a lease set with its value
  (`OMP_NUM_THREADS=4,GOMAXPROCS=4`). It is read only when the environment holds a
  `LEASEHOLD_LEASE_ID`, and only names in `JOB_VARIABLES` count. A user value equal to the
  recorded one cannot be told from the lease's. A nested lease sets the recorded names to its
  own values. A shim inside a lease takes no lease of its own, and only lowers them (`resized`).

  A user's `OMP_NUM_THREADS`, when it is a plain count, is copied to `OPENBLAS_NUM_THREADS`
  and `MKL_NUM_THREADS` where the user left them unset. A user's `OPENBLAS_NUM_THREADS` or
  `MKL_NUM_THREADS` is left alone and changes nothing else. `LEASEHOLD_JOBS` and
  `LEASEHOLD_THREADS` are always the job count and the thread count this lease chose.
* **A jobserver** (`hold --jobserver`, `Jobserver`): a named pipe holding `jobs - 1` tokens,
  announced in `MAKEFLAGS` as `--jobserver-auth=fifo:PATH`, the protocol of GNU make 4.4. make,
  ninja 1.13 and later, cargo and rustc, and GCC's LTO take a token before each extra job, so
  the whole tree of builds under the command, sub-makes included, runs at most `jobs` at once.
  A tool that does not speak the protocol (or make before 4.4, which warns and runs serially)
  still has the variables. The command must not pass its own `-jN`, on its command line or in
  a recipe: make then runs its own jobserver (`own_job_count` finds one on a command line).

The variables are set once, when the command starts: a process cannot change another's
environment. Only the jobserver's tokens can follow an elastic lease (`leasehold.follow`) as
it grows and shrinks, so for one the variables that would take a make out of the jobserver
are left unset, and those whose tool also takes tokens are set to the most jobs it may grow to
(`job_env`'s `elastic_jobs`).

Prior art: steve and guildmaster run one jobserver for a whole machine; here the jobserver is
per lease, so it follows what the machine's daemon granted.
"""

from __future__ import annotations

import array
import contextlib
import fcntl
import os
import re
import shutil
import stat
import tempfile
import termios
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

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

#: Worker variables an elastic lease leaves unset: the tool passes the count on as `-jN`, which
#: takes its make out of the jobserver (`cmake --build` runs `make -jN`).
ELASTIC_SKIPPED = frozenset({"CMAKE_BUILD_PARALLEL_LEVEL"})

#: Worker variables an elastic lease sets to the most jobs it may grow to: the tool also takes
#: a token for each job, so the jobserver keeps it to the lease.
ELASTIC_MOST = frozenset({"CARGO_BUILD_JOBS"})

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

#: A command's `threads` setting: "jobs" (the job count), a count of at least 1, or "jobs/N",
#: the job count divided among N worker processes (`thread_count`).
Threads = int | str

_JOBSERVER_FLAGS = ("--jobserver-auth=", "--jobserver-fds=")

#: A `threads` share: the job count divided among N workers ("jobs/N"), and at most K
#: ("jobs/N:K"), each a count of 1 to 6 digits with no leading zero.
_SHARE = re.compile(r"jobs/[1-9][0-9]{0,5}(:[1-9][0-9]{0,5})?")


def is_share(value: object) -> bool:
    """Whether `value` is a `threads` share, "jobs/N" or "jobs/N:K" (`_SHARE`)."""
    return isinstance(value, str) and _SHARE.fullmatch(value) is not None


def thread_count(threads: Threads, jobs: int) -> int:
    """The thread count `threads` gives a lease of `jobs` jobs, never more than `jobs` nor
    less than 1: `jobs` for "jobs"; `jobs // N` for "jobs/N", and at most K for "jobs/N:K";
    else `threads`."""
    if threads == "jobs":
        return jobs
    if isinstance(threads, str) and threads.startswith("jobs/"):
        share, _, most = threads[5:].partition(":")
        count = jobs // (count_of(share) or 1)
        if count_of(most) is not None:
            count = min(count, count_of(most) or jobs)
        return max(1, min(count, jobs))
    return max(1, min(int(threads), jobs))


def count_of(value: str | None) -> int | None:
    """`value` as a count of at least 1, of at most `MAX_DIGITS` digits; None otherwise. A
    longer string of digits is never converted."""
    if value is None or len(value) > MAX_DIGITS or not (value.isascii() and value.isdigit()):
        return None
    count = int(value)
    return count if count >= 1 else None


def recorded(env: Mapping[str, str]) -> dict[str, str]:
    """The names in `JOB_VARIABLES` and values `env`'s `LEASEHOLD_SET` records, read only when
    `env` holds a `LEASEHOLD_LEASE_ID`. Empty when it is missing or any part of it is
    malformed; other names are not believed."""
    if not env.get("LEASEHOLD_LEASE_ID"):
        return {}
    out: dict[str, str] = {}
    for part in env.get(LEASEHOLD_SET, "").split(","):
        if not part:
            continue
        pair = _PAIR.fullmatch(part)
        if pair is None:
            return {}
        if pair[1] in JOB_VARIABLES and pair[1] not in _OWN:
            out[pair[1]] = pair[2]
    return out


def leasehold_set(env: Mapping[str, str]) -> frozenset[str]:
    """The names whose value in `env` is the one its lease recorded setting (`recorded`)."""
    return frozenset(name for name, value in recorded(env).items() if env.get(name) == value)


def job_env(
    jobs: int,
    env: Mapping[str, str],
    threads: Threads = "jobs",
    *,
    cap: bool = False,
    elastic_jobs: int | None = None,
) -> dict[str, str]:
    """The variables a lease of `jobs` jobs sets in `env`: each of `WORKER_VARIABLES` to `jobs`
    and each of `THREAD_VARIABLES` to `thread_count(threads, jobs)`, but not one the user set
    (present, and not at the value its lease recorded). The user's `OMP_NUM_THREADS`, when it
    is a plain count, goes to `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` instead, with or
    without `cap`. With `cap`, each other thread variable the enclosing lease set gets the
    lower of its value and the new count.
    `LEASEHOLD_JOBS`, `LEASEHOLD_THREADS` and `LEASEHOLD_SET` are always set.

    For a lease whose jobserver follows it up to `elastic_jobs` jobs, the variables leave the
    count to the jobserver (`ELASTIC_SKIPPED`, `ELASTIC_MOST`)."""
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
        if elastic_jobs is not None and name in ELASTIC_SKIPPED:
            continue
        if name in WORKER_VARIABLES:
            out[name] = str(elastic_jobs if elastic_jobs and name in ELASTIC_MOST else jobs)
        elif omp is not None and name in BLAS_FAMILY:
            copied = count_of(omp)
            if copied is not None:
                out[name] = str(copied)  # the user's count wins, as for OMP_NUM_THREADS itself
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
    env: Mapping[str, str],
    jobs: int,
    threads: Threads = "jobs",
    *,
    cap: bool = False,
    elastic_jobs: int | None = None,
) -> dict[str, str]:
    """`env` with `job_env` applied. With `elastic_jobs`, an enclosing lease's value of a name
    in `ELASTIC_SKIPPED` is removed too."""
    sized = job_env(jobs, env, threads, cap=cap, elastic_jobs=elastic_jobs)
    if elastic_jobs is not None:
        ours = leasehold_set(env)
        env = {k: v for k, v in env.items() if k not in ELASTIC_SKIPPED or k not in ours}
    return {**env, **sized}


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


#: A job count given on a command line: `-j8`, `-sj8`, `--jobs=8`, and a bare `-j` (no limit).
#: Letters before the `j` are make's short options that take no argument.
_OWN_COUNT = re.compile(r"-[bBdeiknpqrRsStvw]*j[0-9]*|--jobs(=[0-9]*)?")


def own_job_count(argv: Sequence[str]) -> str | None:
    """The first job count `argv` gives itself (`-j8`, `-j 8`, `--jobs=8`, `--jobs 8`, or a
    bare `-j` or `--jobs`, which sets no limit; also inside a word such as
    `sh -c "make -j8"`), as written; None when it gives none. A make given one runs its own
    jobserver and leaves the lease's."""
    words = [w for arg in argv for w in arg.split()]
    for i, word in enumerate(words):
        if not _OWN_COUNT.fullmatch(word):
            continue
        if word in ("-j", "--jobs") and i + 1 < len(words) and words[i + 1].isdigit():
            return f"{word} {words[i + 1]}"
        return word
    return None


class Jobserver:
    """A fifo jobserver for `jobs` jobs, as a context manager: the command itself holds the
    implicit first token, so the fifo starts with `jobs - 1`. In a directory only this user may
    enter (`fifo`); removed on leaving.

    `tokens` is how many tokens should be in play: in the fifo, or taken by a job that will put
    them back. `set` changes it. More are written to the fifo at once, so more jobs run. Fewer
    are owed (`owed`): a thread blocked reading the fifo takes each token a job gives back
    until none is owed, so no running job is stopped. A token it reads once none is owed (the
    count rose again first) goes straight back. A token is owed until it comes back, however
    long that takes: one a killed job held never does, and stays owed."""

    def __init__(self, jobs: int) -> None:
        self.tokens = max(0, jobs - 1)
        self.fifo = Path()
        self._directory: Path | None = None
        self._fd: int | None = None
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._owed = 0
        self._closing = False
        self._reading = False
        self._reader: threading.Thread | None = None
        self._reader_fd: int | None = None

    def __enter__(self) -> Jobserver:
        self._directory = Path(tempfile.mkdtemp(prefix="leasehold-jobs-"))
        try:
            self.fifo = self._directory / "fifo"
            os.mkfifo(self.fifo, 0o600)
            # Open read-write without blocking, so the tokens can be written before any reader
            # exists, and so the fifo never sees end-of-file while the command runs.
            self._fd = os.open(self.fifo, os.O_RDWR | os.O_NONBLOCK)
            if not stat.S_ISFIFO(os.fstat(self._fd).st_mode):
                raise OSError(f"{self.fifo} is not a fifo")
            os.write(self._fd, b"+" * self.tokens)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc) -> None:
        with self._lock:
            self._closing = True
            self._wake.notify_all()
            if self._reading and self._fd is not None:
                # The reader is blocked in read(): one token wakes it, and it then stops.
                with contextlib.suppress(OSError):
                    os.write(self._fd, b"+")
        if self._reader is not None:
            self._reader.join(timeout=5)
            self._reader = None
        with self._lock:
            for name in ("_reader_fd", "_fd"):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None

    @property
    def owed(self) -> int:
        """Tokens in play past `tokens`, still to be taken back as jobs give them back."""
        with self._lock:
            return self._owed

    def settle(self, timeout: float) -> int:
        """Wait up to `timeout` seconds for no token to be owed; `owed` then."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._owed and not self._closing:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._wake.wait(left)
            return self._owed

    def taken(self) -> int:
        """How many tokens are out of the fifo now, held by jobs (or lost with one that was
        killed): what is in play less what the fifo holds."""
        with self._lock:
            if self._fd is None:
                return 0
            free = array.array("i", [0])
            fcntl.ioctl(self._fd, termios.FIONREAD, free)
            return max(0, self.tokens + self._owed - free[0])

    def set(self, tokens: int) -> None:
        """Have `tokens` in play: write the missing ones (after cancelling what is owed), or owe
        the ones past it."""
        tokens = max(0, tokens)
        with self._lock:
            if self._fd is None or self._closing:
                return
            if tokens > self.tokens:
                more = tokens - self.tokens
                cancelled = min(more, self._owed)
                self._owed -= cancelled
                if more > cancelled:
                    os.write(self._fd, b"+" * (more - cancelled))
            else:
                self._owed += self.tokens - tokens
            self.tokens = tokens
            if self._owed and self._reader is None:
                self._start_reader()
            self._wake.notify_all()

    def add(self, count: int) -> None:
        """Put `count` more tokens in play."""
        if count > 0:
            self.set(self.tokens + count)

    def _start_reader(self) -> None:
        # Its own descriptor, blocking: a read waits beside the jobs' own reads and gets its
        # turn, where a poll every so often almost never wins a token from a busy make.
        self._reader_fd = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.set_blocking(self._reader_fd, True)
        self._reader = threading.Thread(target=self._take_back, name="jobserver", daemon=True)
        self._reader.start()

    def _take_back(self) -> None:
        """The reader: while tokens are owed, read one at a time and keep it, or put it back
        when no longer owed."""
        while True:
            with self._lock:
                while not self._owed and not self._closing:
                    self._wake.wait()
                if self._closing:
                    return
                self._reading = True
                fd = self._reader_fd
            try:
                got = os.read(fd, 1) if fd is not None else b""
            except OSError:
                got = b""
            with self._lock:
                self._reading = False
                if not got or self._closing:
                    return
                if self._owed:
                    self._owed -= 1
                    self._wake.notify_all()
                elif self._fd is not None:
                    os.write(self._fd, got)


@contextlib.contextmanager
def jobserver(jobs: int) -> Iterator[Path]:
    """A `Jobserver` for `jobs` jobs whose tokens stay as they started: its fifo."""
    with Jobserver(jobs) as server:
        yield server.fifo
