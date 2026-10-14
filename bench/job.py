"""One benchmark job: a fixed amount of hashing spread over as many worker processes as the job
is told it may use, the stand-in for a test run or a build that uses every core it is given.

    job.py cpu --work BYTES [--ignore-env]
    job.py mem --work BYTES --size BYTES [--ignore-env]

`cpu` hashes a buffer that fits in cache, BYTES in all, split evenly over the workers. `mem` first
allocates and touches SIZE bytes of private memory, then its workers hash that memory, BYTES in
all; its footprint is SIZE whatever the worker count, like one large test process or a linker.

The worker count is `LEASEHOLD_JOBS` when it is set, as a well-behaved tool sizes itself from
`CARGO_BUILD_JOBS`, `PYTEST_XDIST_AUTO_NUM_WORKERS` and the like; otherwise, or with
`--ignore-env` (a tool that ignores them), it is the number of CPUs.

The job prints `BENCH start <epoch> <workers> <cgroup>` when it starts and `BENCH end <epoch>`
when its work is done, and exits 0; if a worker dies (the OOM killer), it stops the others and
exits 137. It raises its own OOM score so that, under memory pressure, the kernel picks a job
over the benchmark's harness and probes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import mmap
import os
import signal
import sys
import time

PAGE = mmap.PAGESIZE
CHUNK = 1 << 20  # bytes hashed per call
HOT = 256 * 1024  # the cpu job's in-cache buffer


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("kind", choices=["cpu", "mem"])
    parser.add_argument("--work", type=int, required=True, help="bytes to hash in all")
    parser.add_argument("--size", type=int, default=0, help="mem: bytes to allocate and touch")
    parser.add_argument("--ignore-env", action="store_true", help="use every CPU regardless")
    args = parser.parse_args(argv)
    _raise_oom_score()
    n = workers(args.ignore_env)
    cgroup = "?"
    with contextlib.suppress(OSError), open("/proc/self/cgroup") as f:
        cgroup = f.read().split("::", 1)[1].strip() or "?"
    print(f"BENCH start {time.time():.3f} {n} {cgroup}", flush=True)
    code = run(args.kind, args.work, args.size, n)
    if code == 0:
        print(f"BENCH end {time.time():.3f}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
