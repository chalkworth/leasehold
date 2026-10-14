"""The interactive-latency probe: what a person at the machine feels while the jobs run.

    probe.py wakeup --out FILE.csv.gz
    probe.py git --repo DIR --out FILE.csv.gz [--every S]

`wakeup` sleeps 10 ms in a loop and records, for each sleep, how much later than asked it woke
(`overshoot_us`): the scheduling delay an editor or a shell sees on every keystroke. `git` times
`git status` in DIR every S seconds (default 5). Both run until SIGTERM or SIGINT, then write
their samples, one row each with the time it was taken (`t`, seconds since the epoch).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import signal
import subprocess
import sys
import time
from array import array

SLEEP_S = 0.010


class _Stop(Exception):
    pass


def _stop(_sig, _frame):
    raise _Stop


def wakeup(out: str) -> None:
    times, overshoot = array("d"), array("f")
    clock, wall = time.perf_counter, time.time
    try:
        while True:
            t0 = clock()
            time.sleep(SLEEP_S)
            late = clock() - t0 - SLEEP_S
            times.append(wall())
            overshoot.append(late * 1e6)
    except _Stop:
        pass
    n = min(len(times), len(overshoot))  # a stop between the two appends leaves one longer
    _write(
        out, ["t", "overshoot_us"], zip(times[:n], overshoot[:n], strict=True), "{:.3f}", "{:.0f}"
    )


def git(repo: str, out: str, every: float) -> None:
    rows: list[tuple[float, float, int]] = []
    try:
        while True:
            t0 = time.perf_counter()
            done = subprocess.run(
                ["git", "-C", repo, "status", "--porcelain"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            rows.append((time.time(), (time.perf_counter() - t0) * 1e3, done.returncode))
            time.sleep(every)
    except _Stop:
        pass
    _write(out, ["t", "ms", "rc"], rows, "{:.3f}", "{:.1f}", "{}")


def _write(path: str, header: list[str], rows, *formats: str) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow([fmt.format(v) for fmt, v in zip(formats, row, strict=True)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("what", choices=["wakeup", "git"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo")
    parser.add_argument("--every", type=float, default=5.0)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.what == "wakeup":
        wakeup(args.out)
    else:
        if not args.repo:
            parser.error("git needs --repo")
        git(args.repo, args.out, args.every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
