"""Take leases from Python: a range granted as many slots as fit, a command run under it, a wait
that gives up, and a wait cancelled from another thread. Run it against a daemon of your own on
this directory's machine.toml (see README.md)."""

import sys
import threading

import leasehold

sys.stdout.reconfigure(line_buffering=True)  # in order with the command's output
print("daemon:", leasehold.find_daemon())

with leasehold.lease((2, 4), labels={"example": "python"}) as lease:
    print(f"granted {lease.weight} of 2-4 slots in pool {lease.pool}, {lease.jobs} jobs")
    code = lease.run(
        [
            sys.executable,
            "-c",
            "import os; print('  the command sees', os.environ['LEASEHOLD_WEIGHT'], 'slots')",
        ]
    )
    print("the command exited", code)

    # The range left 1 of the 4 slots (extras never take a pool's last one), so a lease of 2
    # waits in line.
    try:
        leasehold.lease(2, timeout=2, on_wait=lambda w: print(f"  waiting, place {w.position}"))
    except leasehold.Busy as busy:
        print(f"busy after {busy.waited_s:.0f}s: {busy}")

    stopping = threading.Event()
    threading.Timer(1, stopping.set).start()
    try:
        leasehold.lease(2, cancel=stopping)
    except leasehold.Cancelled:
        print("cancelled: the place in line was given back")

print(
    "released; the daemon now holds",
    len(leasehold.client.Client(leasehold.find_daemon()).leases()["leases"]),
    "leases",
)
