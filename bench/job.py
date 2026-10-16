"""One benchmark job: a fixed amount of hashing spread over as many worker processes as the job
is told it may use, the stand-in for a test run or a build that uses every core it is given.

    job.py cpu --work BYTES [--ignore-env | --jobserver]
    job.py mem --work BYTES --size BYTES [--ignore-env | --jobserver]

`cpu` hashes a buffer that fits in cache, BYTES in all, split evenly over the workers. `mem` first
allocates and touches SIZE bytes of private memory, then its workers hash that memory, BYTES in
all; its footprint is SIZE whatever the worker count, like one large test process or a linker.

The worker count is `LEASEHOLD_JOBS` when it is set, as a well-behaved tool sizes itself from
`CARGO_BUILD_JOBS`, `PYTEST_XDIST_AUTO_NUM_WORKERS` and the like; otherwise, or with
`--ignore-env` (a tool that ignores them), it is the number of CPUs.

With `--jobserver` the job is a client of the GNU make jobserver in `MAKEFLAGS`
(`--jobserver-auth=fifo:PATH`, or the older `R,W` pair of descriptors), as make itself is under
an outer make: its work is cut into small tasks, it always runs one on the token it holds by
being started, and runs each further one only on a token it reads from the jobserver, which it
writes back when that task ends. How many tasks run at once then follows how many tokens the
jobserver holds while the job runs. Without a jobserver in `MAKEFLAGS` it exits 2.

The job prints `BENCH start <epoch> <workers> <cgroup>` when it starts and `BENCH end <epoch>`
when its work is done (a jobserver client gives `-jN` from `MAKEFLAGS`, or 0, as its workers,
and adds the most tasks it ran at once to its end line), and exits 0; if a worker dies (the OOM
killer), it stops the others and exits 137. It raises its own OOM score so that, under memory
pressure, the kernel picks a job over the benchmark's harness and probes.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import mmap
import os
import select
import signal
import sys
import time

PAGE = mmap.PAGESIZE
CHUNK = 1 << 20  # bytes hashed per call
HOT = 256 * 1024  # the cpu job's in-cache buffer
TASKS_PER_CPU = 4  # a jobserver client's tasks: enough that a token freed mid-job gets used


def workers(ignore_env: bool) -> int:
    if not ignore_env:
        try:
            return max(1, int(os.environ["LEASEHOLD_JOBS"]))
        except (KeyError, ValueError):
            pass
    return os.cpu_count() or 1


def _hash_hot(quota: int) -> None:
    buf = os.urandom(HOT)
    done = 0
    while done < quota:
        hashlib.sha256(buf).digest()
        done += HOT


def _hash_region(view: memoryview, lo: int, hi: int, quota: int) -> None:
    done, at = 0, lo
    while done < quota:
        end = min(hi, at + CHUNK)
        hashlib.sha256(view[at:end]).digest()
        done += end - at
        at = lo if end >= hi else end


def _raise_oom_score() -> None:
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as f:
        f.write("500")


def run(kind: str, work: int, size: int, n: int) -> int:
    view = None
    if kind == "mem":
        size = max(PAGE, size - size % PAGE)
        mem = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
        mem[::PAGE] = b"\x01" * (size // PAGE)  # every page is now resident (or swapped)
        view = memoryview(mem)
    pids = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:
            try:
                if view is None:
                    _hash_hot(work // n)
                else:
                    lo, hi = size * i // n, size * (i + 1) // n
                    _hash_region(view, lo, hi, work // n)
            finally:
                os._exit(0)
        pids.append(pid)
    code = 0
    for _ in pids:
        pid, status = os.wait()
        if os.waitstatus_to_exitcode(status) != 0 and code == 0:
            code = 137
            for other in pids:
                if other != pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(other, signal.SIGKILL)
    return code


def jobserver_auth(makeflags: str) -> tuple[str, int]:
    """(`--jobserver-auth` value, `-jN`) from `MAKEFLAGS`: the last of each, as make reads them;
    ("", 0) where one is missing."""
    auth, jobs = "", 0
    for word in makeflags.split():
        if word.startswith(("--jobserver-auth=", "--jobserver-fds=")):
            auth = word.split("=", 1)[1]
        elif word.startswith("-j") and word[2:].isdigit():
            jobs = int(word[2:])
    return auth, jobs


def open_jobserver(auth: str) -> tuple[int, int]:
    """(read fd, write fd) of the jobserver `auth` names, the read end non-blocking."""
    if auth.startswith("fifo:"):
        fd = os.open(auth[5:], os.O_RDWR | os.O_NONBLOCK)
        return fd, fd
    r, w = (int(x) for x in auth.split(","))
    os.set_blocking(r, False)
    return r, w


def run_jobserver(kind: str, work: int, size: int, auth: str) -> tuple[int, int]:
    """The job as a jobserver client: (exit code, most tasks run at once). Every token read is
    written back, whatever happens."""
    rfd, wfd = open_jobserver(auth)
    tasks = TASKS_PER_CPU * (os.cpu_count() or 1)
    view, size = _touch(kind, size)
    pending = list(range(tasks))
    running: dict[int, bytes | None] = {}  # pid: the token it runs on (None: our own)
    code, peak = 0, 0
    try:
        while pending or running:
            if pending:
                token = None if None not in running.values() else _take_token(rfd)
                if token != b"":
                    running[_task(view, size, pending.pop(0), tasks, work)] = token
                    peak = max(peak, len(running))
                    continue
            for pid, status in _reap(running, block=not pending):
                held = running.pop(pid)
                if held:
                    os.write(wfd, held)
                if os.waitstatus_to_exitcode(status) != 0 and code == 0:
                    code = 137
                    pending.clear()
                    for other in running:
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(other, signal.SIGKILL)
    finally:
        for held in running.values():
            if held:
                os.write(wfd, held)
    return code, peak


def _take_token(rfd: int) -> bytes:
    """A token, or b"" if none came within a moment (another client may read it first)."""
    ready, _, _ = select.select([rfd], [], [], 0.05)
    if not ready:
        return b""
    try:
        return os.read(rfd, 1)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return b""
        raise


def _reap(running: dict, block: bool) -> list[tuple[int, int]]:
    """The tasks that have ended: one at least if `block`, else whichever have."""
    out = []
    if block and running:
        out.append(os.wait())
    while len(out) < len(running):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        out.append((pid, status))
    return out


def _task(view, size: int, i: int, tasks: int, work: int) -> int:
    pid = os.fork()
    if pid == 0:
        try:
            if view is None:
                _hash_hot(work // tasks)
            else:
                _hash_region(view, size * i // tasks, size * (i + 1) // tasks, work // tasks)
        finally:
            os._exit(0)
    return pid


def _touch(kind: str, size: int):
    if kind != "mem":
        return None, size
    size = max(PAGE, size - size % PAGE)
    mem = mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    mem[::PAGE] = b"\x01" * (size // PAGE)  # every page is now resident (or swapped)
    return memoryview(mem), size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("kind", choices=["cpu", "mem"])
    parser.add_argument("--work", type=int, required=True, help="bytes to hash in all")
    parser.add_argument("--size", type=int, default=0, help="mem: bytes to allocate and touch")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ignore-env", action="store_true", help="use every CPU regardless")
    mode.add_argument(
        "--jobserver", action="store_true", help="run tasks on the jobserver in MAKEFLAGS"
    )
    args = parser.parse_args(argv)
    _raise_oom_score()
    auth, jobs = jobserver_auth(os.environ.get("MAKEFLAGS", ""))
    if args.jobserver and not auth:
        print("job.py: --jobserver, but MAKEFLAGS names no jobserver", file=sys.stderr)
        return 2
    n = jobs if args.jobserver else workers(args.ignore_env)
    cgroup = "?"
    with contextlib.suppress(OSError), open("/proc/self/cgroup") as f:
        cgroup = f.read().split("::", 1)[1].strip() or "?"
    print(f"BENCH start {time.time():.3f} {n} {cgroup}", flush=True)
    if args.jobserver:
        code, peak = run_jobserver(args.kind, args.work, args.size, auth)
        if code == 0:
            print(f"BENCH end {time.time():.3f} {peak}", flush=True)
        return code
    code = run(args.kind, args.work, args.size, n)
    if code == 0:
        print(f"BENCH end {time.time():.3f}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
