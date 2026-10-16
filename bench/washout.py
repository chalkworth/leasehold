"""The wait between two runs until the machine is quiet again, so that one arm does not run in
what the arm before it left behind.

The machine is quiet when all of these hold at one reading:

* the load average is at most a quarter of the CPUs (at least 2);
* CPU pressure (PSI `some` avg10) is under 10%;
* memory pressure is under 0.5%;
* almost nothing was read back from swap over the last 30 s (`pswpin`, at most 16 pages);
* dirty and writeback pages together are under 64 MiB.

The wait reads the machine every 5 s, after a settling pause, and gives up after `max_wait_s`.
`washed` runs a run only after a quiet wait: a wait that times out marks that attempt invalid
and waits again, once; a second timeout leaves the run invalid and not run.

`drop_caches` empties the page cache (`sync`, then `3` to `/proc/sys/vm/drop_caches`) before a
wait. It needs root or passwordless sudo for that one write, which `check_drop_caches` checks
before anything runs.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

MIB = 1 << 20
CPU_SOME_MAX = 10.0
MEM_SOME_MAX = 0.5
SWAPIN_WINDOW_S = 30.0
SWAPIN_PAGES_MAX = 16
DIRTY_MAX_BYTES = 64 * MIB
DROP_CACHES = "/proc/sys/vm/drop_caches"


@dataclass(frozen=True)
class Reading:
    t: float  # monotonic seconds
    load1: float
    cpu_some: float
    mem_some: float
    pswpin: int  # pages read back from swap since boot
    dirty_bytes: int  # Dirty + Writeback


def _psi_some(resource: str) -> float:
    try:
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
            kind, *fields = line.split()
            if kind == "some":
                return float(dict(f.split("=") for f in fields)["avg10"])
    except (OSError, KeyError, ValueError):
        pass
    return 0.0


def read_machine() -> Reading:
    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        mem[key] = int(rest.split()[0]) * 1024
    vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return Reading(
        t=time.monotonic(),
        load1=float(Path("/proc/loadavg").read_text().split()[0]),
        cpu_some=_psi_some("cpu"),
        mem_some=_psi_some("memory"),
        pswpin=int(vm.get("pswpin", 0)),
        dirty_bytes=mem.get("Dirty", 0) + mem.get("Writeback", 0),
    )


def not_quiet(now: Reading, history: list[Reading], cpus: int) -> list[str]:
    """Why the machine is not quiet at `now` (empty: it is). `history` holds earlier readings,
    oldest first."""
    why = []
    if now.load1 > max(2, cpus / 4):
        why.append(f"load {now.load1:.2f}")
    if now.cpu_some >= CPU_SOME_MAX:
        why.append(f"cpu pressure {now.cpu_some}%")
    if now.mem_some >= MEM_SOME_MAX:
        why.append(f"memory pressure {now.mem_some}%")
    old = [r for r in history if r.t <= now.t - SWAPIN_WINDOW_S]
    if not old:
        why.append(f"swap-in not yet watched for {SWAPIN_WINDOW_S:g} s")
    elif now.pswpin - old[-1].pswpin > SWAPIN_PAGES_MAX:
        why.append(f"swap-in {now.pswpin - old[-1].pswpin} pages in {now.t - old[-1].t:.0f} s")
    if now.dirty_bytes >= DIRTY_MAX_BYTES:
        why.append(f"dirty and writeback {now.dirty_bytes / MIB:.0f} MiB")
    return why


def wait_quiet(
    cpus: int,
    max_wait_s: float,
    settle_s: float,
    log: Callable[[str], None],
    read: Callable[[], Reading] = read_machine,
    sleep: Callable[[float], None] = time.sleep,
    every_s: float = 5.0,
) -> dict:
    """Wait, at most `max_wait_s` after `settle_s`, until the machine is quiet. What it saw
    last, with `quiet` and, when it gave up, `timed_out` and the reasons."""
    sleep(settle_s)
    history: list[Reading] = []
    start: float | None = None
    while True:
        now = read()
        start = now.t if start is None else start
        why = not_quiet(now, history, cpus)
        history = [r for r in history if r.t > now.t - SWAPIN_WINDOW_S - 2 * every_s]
        history.append(now)
        timed_out = bool(why) and now.t - start >= max_wait_s
        if not why or timed_out:
            seen = asdict(now) | {
                "quiet": not why,
                "timed_out": timed_out,
                "waited_s": round(now.t - start + settle_s, 1),
                "why": why,
            }
            seen.pop("t")
            log(
                f"  machine before: load {now.load1:.2f}, cpu some {now.cpu_some}%, mem some "
                f"{now.mem_some}%, dirty {now.dirty_bytes / MIB:.0f} MiB"
                + (f" (not quiet after {max_wait_s:g} s: {'; '.join(why)})" if why else "")
            )
            return seen
        sleep(every_s)


def washed(
    wait: Callable[[], dict],
    run: Callable[[dict, int], dict],
    invalid: Callable[[dict, int], None],
    attempts: int = 2,
) -> dict | None:
    """`run(seen, attempt)` after a quiet `wait()`. A wait that is not quiet calls
    `invalid(seen, attempt)` and waits again, up to `attempts` waits in all; None if no wait was
    quiet (the run did not run)."""
    for attempt in range(1, attempts + 1):
        seen = wait()
        if seen.get("quiet"):
            return run(seen, attempt)
        invalid(seen, attempt)
    return None


def check_drop_caches() -> None:
    """A SystemExit saying what is needed, unless this process may write `drop_caches`."""
    if os.geteuid() == 0:
        return
    try:
        ok = subprocess.run(
            ["sudo", "-n", "-l", "tee", DROP_CACHES], capture_output=True, check=False
        )
    except OSError:
        ok = None
    if ok is None or ok.returncode != 0:
        raise SystemExit(
            f"--drop-caches needs root, or passwordless sudo for `tee {DROP_CACHES}`; "
            "run without it, or allow that one command in sudoers"
        )


def drop_caches(log: Callable[[str], None]) -> None:
    """Write dirty pages back, then empty the page cache; a SystemExit if the write fails."""
    subprocess.run(["sync"], check=False)
    if os.geteuid() == 0:
        Path(DROP_CACHES).write_text("3\n")
    else:
        done = subprocess.run(
            ["sudo", "-n", "tee", DROP_CACHES],
            input="3\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if done.returncode != 0:
            raise SystemExit(f"cannot drop the page cache: {done.stderr.strip()}")
    log("  page cache dropped")
