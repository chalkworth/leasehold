"""A lease handed between processes: `leasehold acquire`, `keep` and `release`.

`hold -- cmd` ties a lease to one command's life. A hook pair -- a CI runner's job-started and
job-completed scripts, say -- cannot: the process that asks for the lease ends long before the
work does, and a later, unrelated process gives it back. So:

* `acquire --lease-id ID` waits in line like `hold` (same exit codes: 75 busy, 77 refused, 69
  no daemon), prints the granted lease as one JSON line and exits 0. The caller names the id,
  so the releasing process can name it too.
* `--keep` starts a detached renewer (`keep`) before exiting: its own session, stdio on
  /dev/null, so it holds no pipe of the caller open. It renews every TTL/3.
* `release ID` gives the lease back; the renewer sees it gone at its next renewal and exits.

The TTL stays the safety net. The renewer stops renewing -- and the lease ends one TTL later --
when the lease is released, revoked or unknown to the daemon, when `--watch-pid`'s process is
gone (pid *and* birth time, so a reused pid does not count), or after `--max-hold` seconds
whatever happens. Killing the renewer ends the lease the same way. Without `--keep`, a lease
lives one TTL unless something else renews it.

`keep --attach` is the renewer of a lease an agent's hook offered to the command's shell
(`leasehold.attach`): it also adopts that shell by scan when it does not attach itself, measures
the lease's scope, and when stopped by SIGTERM writes what the scope used for the hook's `post`.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable

from leasehold import attach
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import EXIT_NOT_STARTED, EXIT_REFUSED, EXIT_UNAVAILABLE
from leasehold.hold import wait_for_grant
from leasehold.process import process_birth, process_matches

#: The longest a kept lease is renewed when the caller names no `--max-hold`: six hours, the
#: default timeout of a GitHub Actions job.
DEFAULT_MAX_HOLD_S = 6 * 3600.0


def _say(message: str) -> None:
    sys.stderr.write(f"leasehold: {message}\n")
    sys.stderr.flush()


def acquire(
    client: Client,
    *,
    lease_id: str,
    kind: str = "work",
    pool: str | None = None,
    weight: int = 1,
    labels: dict[str, str] | None = None,
    session: str | None = None,
    timeout_s: float | None = None,
    ttl_s: float | None = None,
    memory: str | None = None,
    max_weight: int | None = None,
    memory_per_slot: str | None = None,
) -> tuple[int, dict | None]:
    """Wait for the lease `lease_id` of `weight` to `max_weight` slots (None: `weight`):
    `(0, lease)` once granted, its `weight` the size granted, else `(exit code, None)`.
    `memory` (a size such as `4g`) is the memory the work declares in all, `memory_per_slot`
    what it declares for each slot granted."""
    request = {
        "kind": kind,
        "pool": pool,
        "weight": weight,
        "labels": labels or {},
        "lease_id": lease_id,
        "session": session,
        "ttl_s": ttl_s,
        "memory": memory,
        "max_weight": max_weight if max_weight is not None and max_weight > weight else None,
        "memory_per_slot": memory_per_slot,
    }
    code, answer = wait_for_grant(client, request, timeout_s, _say, fault=EXIT_REFUSED)
    if code is not None:
        return code, None
    return 0, answer["lease"]


def keeper_argv(
    socket: str,
    lease_id: str,
    *,
    ttl_s: float,
    max_hold_s: float,
    watch_pid: int | None,
    attach: bool = False,
) -> list[str]:
    """The `keep` command `acquire --keep` starts, with this interpreter; with `attach`, the
    renewer of a lease offered to an agent's command (`keep --attach`)."""
    argv = [sys.executable, "-m", "leasehold", "--socket", socket, "keep", lease_id]
    argv += ["--ttl", f"{ttl_s:g}", "--max-hold", f"{max_hold_s:g}"]
    if watch_pid is not None:
        argv += ["--watch-pid", str(watch_pid)]
    if attach:
        argv.append("--attach")
    return argv


def spawn_keeper(argv: list[str]) -> int:
    """Start `argv` detached: a session of its own, stdio on /dev/null, no fds inherited. Its
    pid."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def keep(
    client: Client,
    lease_id: str,
    *,
    ttl_s: float,
    max_hold_s: float = DEFAULT_MAX_HOLD_S,
    watch_pid: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Renew `lease_id` every TTL/3 until a reason to stop; that reason. The lease then ends at
    its TTL unless it already has."""
    birth = None if watch_pid is None else process_birth(watch_pid)
    if watch_pid is not None and birth is None:
        return f"pid {watch_pid} is not running"
    interval = max(0.05, ttl_s / 3)
    deadline = clock() + max_hold_s
    while True:
        if watch_pid is not None and not process_matches(watch_pid, birth):
            return f"pid {watch_pid} ended"
        if clock() >= deadline:
            return f"held for --max-hold {max_hold_s:g}s"
        try:
            answer = client.renew(lease_id, ttl_s=ttl_s)
        except Unavailable:
            answer = None  # a restarting daemon keeps the lease for its TTL: ask again
        except ApiError as exc:
            return str(exc)
        if answer is not None and answer["status"] != "ok":
            return f"the lease is {answer['status']}"
        sleep(interval)


def release(client: Client, lease_id: str) -> int:
    try:
        client.release(lease_id)
    except Unavailable as exc:
        _say(str(exc))
        return EXIT_UNAVAILABLE
    except ApiError as exc:
        _say(str(exc))
        return 1
    return 0


def acquire_main(client: Client, socket: str, args) -> int:
    """`leasehold acquire`: the lease as one JSON line on stdout, the renewer started first."""
    code, lease = acquire(
        client,
        lease_id=args.lease_id,
        kind=args.kind,
        pool=args.pool,
        weight=args.weight,
        labels=args.labels,
        session=args.session,
        timeout_s=args.timeout,
        ttl_s=args.ttl,
        memory=getattr(args, "memory", None),
        max_weight=getattr(args, "max_weight", None),
        memory_per_slot=getattr(args, "memory_per_slot", None),
    )
    if lease is None:
        return code
    if args.keep:
        argv = keeper_argv(
            socket,
            args.lease_id,
            ttl_s=float(lease["ttl_s"]),
            max_hold_s=args.max_hold,
            watch_pid=args.watch_pid,
        )
        try:
            lease["keeper_pid"] = spawn_keeper(argv)
        except OSError as exc:
            with contextlib.suppress(Unavailable, ApiError):
                client.release(args.lease_id)
            _say(f"cannot start the renewer: {exc}")
            return EXIT_NOT_STARTED
    sys.stdout.write(json.dumps(lease) + "\n")
    sys.stdout.flush()
    return 0


class _Stopped(Exception):
    pass


def _stopped(_sig, _frame) -> None:
    raise _Stopped


def keep_main(client: Client, args) -> int:
    watcher = None
    if getattr(args, "attach", False):
        watcher = attach.Watcher(args.lease_id, os.environ, args.watch_pid)
        signal.signal(signal.SIGTERM, _stopped)
    try:
        reason = keep(
            client,
            args.lease_id,
            ttl_s=args.ttl,
            max_hold_s=args.max_hold,
            watch_pid=args.watch_pid,
        )
    except _Stopped:
        reason = "told to stop"
    finally:
        if watcher is not None:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            watcher.finish()
    with contextlib.suppress(OSError):
        _say(f"stopped renewing {args.lease_id} (pid {os.getpid()}): {reason}")
    return 0
