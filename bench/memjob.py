"""A scenario job: grows to a set amount of private memory over a set time, filled with data that
does not compress (so RAM-backed swap such as zram holds it page for page), then keeps touching
it for a set time and exits.

    memjob.py --size BYTES [--ramp S] [--hold S] [--late BYTES --late-at S]
              [--workers N] [--shared] [--write]

* `--ramp S`: the memory grows evenly over S seconds (0: at once).
* `--hold S`: after the ramp, the job passes over all its memory again and again for S seconds:
  reading it (hashing a few bytes of each page) or, with `--write`, writing a byte of each page.
* `--late BYTES --late-at S`: BYTES more are added S seconds after the start, as a link step.
* `--workers N`: N processes. Without `--shared` each worker grows and touches its own share of
  the memory. With `--shared` the first process grows all of it and forks N workers that each
  touch their own part of the one mapping: the other processes' page tables keep a swapped page's
  slot, as forked test workers do.

It prints one JSON line when it is full and one when it ends, and exits 0; if a worker dies (the
OOM killer), it kills the others and exits 137.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import mmap
import os
import signal
import sys
import time

PAGE = mmap.PAGESIZE
CHUNK = 16 << 20
_NOISE = os.urandom(CHUNK)


def _say(**what) -> None:
    print(json.dumps({"t": time.time(), "pid": os.getpid(), **what}), flush=True)


def _fill(mem: mmap.mmap, lo: int, hi: int, deadline_of) -> None:
    """Write noise into [lo, hi) of `mem`, keeping pace with `deadline_of(offset)`."""
    at = lo
    while at < hi:
        end = min(hi, at + CHUNK)
        mem[at:end] = _NOISE[: end - at]
        # One distinct word per page, so no two pages are alike.
        for page in range(at, end, PAGE):
            mem[page : page + 8] = page.to_bytes(8, "little")
        at = end
        wait = deadline_of(at) - time.monotonic()
        if wait > 0:
            time.sleep(wait)


def _touch(mem: mmap.mmap, lo: int, hi: int, until: float, write: bool) -> None:
    view = memoryview(mem)
    x = 0
    while time.monotonic() < until:
        for page in range(lo, hi, PAGE):
            if write:
                mem[page + 8] = (mem[page + 8] + 1) & 0xFF
            else:
                x ^= view[page + 8]
            if page % (CHUNK) == 0 and time.monotonic() >= until:
                break


def _worker(args, size: int, mem: mmap.mmap | None, lo: int, hi: int, start: float) -> None:
    ramp = max(args.ramp, 0.0)
    if mem is None:
        mem = mmap.mmap(-1, max(PAGE, size), flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
        lo, hi = 0, size
        _fill(mem, lo, hi, lambda at: start + ramp * at / max(1, size))
    late = None
    if args.late:
        late = mmap.mmap(-1, args.late // max(1, args.workers) // PAGE * PAGE or PAGE)
    until = start + ramp + args.hold
    if late is not None:
        _touch(mem, lo, hi, min(until, start + args.late_at), args.write)
        _fill(late, 0, len(late), lambda at: 0)
    _touch(mem, lo, hi, until, args.write)
    if late is not None:
        _touch(late, 0, len(late), until, args.write)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--size", type=int, required=True)
    p.add_argument("--ramp", type=float, default=0.0)
    p.add_argument("--hold", type=float, default=5.0)
    p.add_argument("--late", type=int, default=0)
    p.add_argument("--late-at", type=float, default=0.0)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--shared", action="store_true")
    p.add_argument("--write", action="store_true")
    args = p.parse_args(argv)
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as f:
        f.write("500")
    start = time.monotonic()
    n = max(1, args.workers)
    size = args.size // PAGE * PAGE
    mem = None
    if args.shared:
        mem = mmap.mmap(-1, max(PAGE, size), flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
        _fill(mem, 0, size, lambda at: start + args.ramp * at / max(1, size))
    pids = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                if mem is not None:
                    _worker(
                        args,
                        size,
                        mem,
                        size * i // n // PAGE * PAGE,
                        size * (i + 1) // n // PAGE * PAGE,
                        start,
                    )
                else:
                    _worker(args, size // n // PAGE * PAGE, None, 0, 0, start)
            except BaseException:  # noqa: BLE001
                code = 1
            os._exit(code)
        pids.append(pid)
    if mem is None:
        time.sleep(max(0.0, start + args.ramp - time.monotonic()))
    _say(event="full", size=size)
    code = 0
    for _ in pids:
        pid, status = os.wait()
        if os.waitstatus_to_exitcode(status) != 0 and code == 0:
            code = 137
            for other in pids:
                if other != pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(other, signal.SIGKILL)
    _say(event="end", code=code)
    return code


if __name__ == "__main__":
    sys.exit(main())
