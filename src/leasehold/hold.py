"""`leasehold hold -- cmd`: run one command under a lease.

Acquire (waiting in line if need be), start the command in its own process group, renew the
lease while it runs, release it when the command ends, and exit with the command's code. The
lease covers the whole process group: anything the command leaves running in it is stopped
when it ends. Run from a terminal's foreground, the command's group gets the terminal while it
runs (`leasehold.terminal`); otherwise it runs in a session of its own.

* A revoked lease (only `lend` is ever revoked), or a `lend` lease lost to its TTL, stops the
  group -- SIGTERM, then SIGKILL after the grace -- and exits `EXIT_REVOKED`.
* Waiting longer than `--timeout` exits `EXIT_BUSY` (75: busy, not failed), the place in line
  given back. A request that could never be granted, or that the daemon answers with an error
  (a 5xx included), exits `EXIT_REFUSED`: a daemon that answers is never taken for none.
* With no daemon, the command runs without a lease, after a warning (`run_unleased`); with
  `if_unavailable="fail"`, `hold` exits `EXIT_UNAVAILABLE` instead.
* A `work` or `external` lease the daemon no longer knows (it was down longer than the TTL) is
  asked for again; the command keeps running.

Where the daemon allows it and the caller runs in the user's own session, the command is started
in a systemd user scope sized to the lease (`leasehold.contain`); elsewhere it runs as is.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from leasehold import contain, jobs, shell, terminal
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import (
    EXIT_BUSY,
    EXIT_NOT_STARTED,
    EXIT_REFUSED,
    EXIT_REVOKED,
    EXIT_UNAVAILABLE,
)
from leasehold.kinds import PREEMPTIBLE

__all__ = [
    "EXIT_BUSY",
    "EXIT_NOT_STARTED",
    "EXIT_REFUSED",
    "EXIT_REVOKED",
    "EXIT_UNAVAILABLE",
    "hold",
    "wait_for_grant",
    "waiting_line",
]
#: The most one acquire request waits before asking again (each ask keeps the place in line).
_POLL_S = 20.0
#: While nothing changes in line, how often a wait says it is still waiting.
PROGRESS_S = 20.0


def _say(message: str) -> None:
    sys.stderr.write(f"leasehold hold: {message}\n")
    sys.stderr.flush()


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def wait_for_grant(
    client: Client,
    request: dict,
    timeout_s: float | None,
    say=_say,
    *,
    quiet_unavailable: bool = False,
) -> tuple[int | None, dict]:
    """Ask for `request`'s lease, waiting in line (each ask keeps the place) until it is granted
    or `timeout_s` is up. `(None, answer)` on a grant, else `(exit code, answer)`: `EXIT_BUSY`
    with the place in line given back, `EXIT_REFUSED` or `EXIT_UNAVAILABLE` (said, unless
    `quiet_unavailable`). While it waits it says its place and why whenever they change, and
    at least every `PROGRESS_S` with the time waited so far."""
    start = time.monotonic()
    deadline = None if timeout_s is None else start + timeout_s
    waited = False
    last_line = None
    last_said = start
    answer: dict = {}
    try:
        while True:
            left = _POLL_S if deadline is None else max(0.0, deadline - time.monotonic())
            # The first ask answers at once, so a wait is reported as it starts.
            wait_s = min(_POLL_S, left) if waited else 0.0
            answer = client.acquire(**request, wait_s=wait_s)
            status = answer["status"]
            if status == "granted":
                return None, answer
            if status == "refused":
                say(f"refused: {answer.get('reason')}")
                return EXIT_REFUSED, answer
            line = waiting_line(request, answer)
            now = time.monotonic()
            if line != last_line:
                say(line)
                last_line, last_said = line, now
            elif now - last_said >= PROGRESS_S:
                say(f"{line}; {now - start:.0f}s so far")
                last_said = now
            waited = True
            if deadline is not None and time.monotonic() >= deadline:
                with contextlib.suppress(Unavailable, ApiError):
                    client.release(request["lease_id"])
                say(f"gave up after {timeout_s:g}s in line")
                return EXIT_BUSY, answer
    except Unavailable as exc:
        if not quiet_unavailable:
            say(f"{exc}; is a daemon running? (`leasehold doctor` says what it finds)")
        return EXIT_UNAVAILABLE, answer
    except ApiError as exc:
        say(str(exc))
        return EXIT_REFUSED, answer


def waiting_line(request: dict, answer: dict) -> str:
    """What `hold` and `acquire` print while queued: what they wait for, their place and why."""
    weight = request.get("weight", 1)
    slots = "1 slot" if weight == 1 else f"{weight} slots"
    where = f" in pool {request['pool']}" if request.get("pool") else ""
    line = f"waiting for {slots}{where} ({request['kind']}): place {answer.get('position')}"
    blocked = answer.get("blocked")
    return f"{line}; {blocked}" if blocked else line


class _Lease:
    def __init__(self, client: Client, lease_id: str, kind: str) -> None:
        self.client = client
        self.lease_id = lease_id
        self.kind = kind
        self.last_ok = time.monotonic()

    def renew(self) -> dict | None:
        try:
            answer = self.client.renew(self.lease_id)
        except (Unavailable, ApiError):
            return None
        if answer["status"] in ("ok", "revoked"):
            self.last_ok = time.monotonic()
        return answer

    def release(self) -> None:
        with contextlib.suppress(Unavailable, ApiError):
            self.client.release(self.lease_id)


def _stop_group(
    child: subprocess.Popen, grace_s: float, lease: _Lease | None, interval: float
) -> int:
    """SIGTERM the group, wait up to `grace_s` (renewing any lease, so the weight stays held
    while it stops), then SIGKILL whatever is left. The leader's exit status."""
    pgid = child.pid
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if child.poll() is not None and not _group_alive(pgid):
            break
        time.sleep(min(interval, 0.1))
        if lease is not None:
            lease.renew()
    _signal_group(pgid, signal.SIGKILL)
    return child.wait()


def hold(
    client: Client | None,
    argv: Sequence[str],
    *,
    kind: str = "work",
    pool: str | None = None,
    weight: int | None = None,
    labels: dict[str, str] | None = None,
    session: str | None = None,
    timeout_s: float | None = None,
    ttl_s: float | None = None,
    grace_s: float = 30.0,
    job_env: bool = True,
    jobserver: bool = False,
    memory: str | None = None,
    if_unavailable: str = "run",
) -> int:
    """Run `argv` under a lease, sized to it (`leasehold.jobs`): the job-count variables unless
    `job_env` is false, a make jobserver when `jobserver` is true, and when contained a scope
    weighted to the lease and held to `memory` (a size such as `4g`) when given. With no
    `weight`, the command is weighed as the daemon's `[commands]` rules weigh it (`auto_size`).
    With no daemon (`client` None: none could be found), `if_unavailable` `run` runs it without
    a lease (`run_unleased`), `fail` exits `EXIT_UNAVAILABLE`."""
    if not argv:
        _say("no command given (leasehold hold [options] -- cmd ...)")
        return 2
    if weight is None:
        weight, sized = auto_size(client, argv) if client is not None else (1, None)
        memory = memory or sized
    request = lease_request(
        kind=kind, pool=pool, weight=weight, labels=labels, session=session, ttl_s=ttl_s
    )
    if client is None:
        code, answer = EXIT_UNAVAILABLE, {}
    else:
        code, answer = wait_for_grant(
            client, request, timeout_s, _say, quiet_unavailable=if_unavailable == "run"
        )
    if code == EXIT_UNAVAILABLE and if_unavailable == "run":
        _say("no daemon answers; running without a lease")
        return run_unleased(argv, grace_s=grace_s)
    if code is not None:
        return code
    return run_leased(
        client,
        argv,
        request,
        answer,
        grace_s=grace_s,
        job_env=job_env,
        jobserver=jobserver,
        memory=memory,
    )


def auto_size(client: Client, argv: Sequence[str]) -> tuple[int, str | None]:
    """The weight (and memory) the daemon's `[commands]` rules give `argv`'s program and
    arguments, as the shims would weigh it; weight 1 and no memory when no rule matches or the
    rules cannot be read."""
    try:
        rules = shell.served(client.commands()).rules
    except (Unavailable, ApiError, KeyError, ValueError):
        return 1, None
    found = shell.match_programs(rules, [(argv[0], list(argv[1:]))])
    if found is None:
        return 1, None
    return found.rule.weight, found.rule.memory


def lease_request(
    *,
    kind: str = "work",
    pool: str | None = None,
    weight: int = 1,
    labels: dict[str, str] | None = None,
    session: str | None = None,
    ttl_s: float | None = None,
) -> dict:
    """An acquire request for a new lease of its own id, as `hold` sends it."""
    return {
        "kind": kind,
        "pool": pool,
        "weight": weight,
        "labels": labels or {},
        "lease_id": uuid.uuid4().hex,
        "session": session,
        "ttl_s": ttl_s,
    }


def run_leased(
    client: Client,
    argv: Sequence[str],
    request: dict,
    answer: dict,
    *,
    grace_s: float = 30.0,
    job_env: bool = True,
    jobserver: bool = False,
    memory: str | None = None,
) -> int:
    """`hold` once `request` is granted (`answer`): run `argv` sized to the lease, renewing it,
    and release it when the command ends; the command's exit code."""
    lease_id, kind = request["lease_id"], request["kind"]
    granted = answer["lease"]
    lease = _Lease(client, lease_id, kind)
    ttl = float(granted["ttl_s"])
    interval = max(0.05, ttl / 3)
    count = int(granted.get("jobs") or granted["weight"])
    with contextlib.ExitStack() as stack:
        env = child_env(lease_id, granted, count, job_env=job_env)
        if jobserver:
            try:
                fifo = stack.enter_context(jobs.jobserver(count))
            except OSError as exc:
                lease.release()
                _say(f"cannot make a jobserver: {exc}")
                return EXIT_NOT_STARTED
            env |= jobserver_env(os.environ, count, fifo)
        prefix = scope_prefix(request, answer, memory)
        if prefix and shutil.which(argv[0], path=env.get("PATH")) is None:
            # Said here: under systemd-run a missing program would be its error, not ours.
            lease.release()
            _say(f"cannot run {argv[0]}: not found or not executable")
            return EXIT_NOT_STARTED
        try:
            tty = terminal.owned()
            child, uncontained = spawn(argv, prefix, env, tty=tty)
        except OSError as exc:
            lease.release()
            _say(f"cannot run {argv[0]}: {exc}")
            return EXIT_NOT_STARTED
        if uncontained:
            _say(uncontained)
        if tty:
            stack.enter_context(foreground(child.pid))
        return _supervise(client, child, lease, request, kind, ttl, interval, grace_s)


def spawn(
    argv: Sequence[str], prefix: Sequence[str] | None, env: Mapping[str, str], *, tty: bool
) -> tuple[subprocess.Popen, str | None]:
    """Start `argv` (in the scope `prefix` makes, when there is one: `contain.spawn`) as the
    leader of a process group of its own: in this terminal's session with `tty` (this process
    has the terminal: `terminal.owned`), else in a session of its own. The process, and why it
    is not contained when a scope was asked for and could not be made."""
    group = {"process_group": 0} if tty else {"start_new_session": True}
    return contain.spawn(argv, prefix, env=env, **group)


@contextlib.contextmanager
def foreground(pgid: int) -> Iterator[None]:
    """While inside, the group `pgid` (started by `spawn` with `tty`) has the terminal; the
    terminal comes back when it is stopped (`leasehold.terminal`) and on leaving."""
    with terminal.follow_stops(pgid):
        terminal.hand_to(pgid)
        try:
            yield
        finally:
            terminal.take_back(pgid)


def run_unleased(argv: Sequence[str], *, grace_s: float = 30.0) -> int:
    """Run `argv` as `hold` runs a leased command, but with no lease, scope or job variables:
    its own process group (with the terminal, when this process has it), signals forwarded, and
    what it leaves in the group stopped when it ends. The command's exit code."""
    tty = terminal.owned()
    try:
        child, _ = spawn(argv, None, dict(os.environ), tty=tty)
    except OSError as exc:
        _say(f"cannot run {argv[0]}: {exc}")
        return EXIT_NOT_STARTED
    with contextlib.ExitStack() as stack:
        if tty:
            stack.enter_context(foreground(child.pid))
        stack.enter_context(_forwarding(child.pid))
        code = child.wait()
        if _group_alive(child.pid):
            _stop_group(child, grace_s, None, 0.1)
    return code if code >= 0 else 128 - code


@contextlib.contextmanager
def _forwarding(pgid: int) -> Iterator[None]:
    """While inside, SIGTERM, SIGINT and SIGHUP sent to this process go to the group `pgid`."""

    def forward(sig, _frame):
        _signal_group(pgid, sig)

    previous = {
        s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def scope_prefix(request: dict, answer: dict, memory: str | None) -> list[str] | None:
    """The `systemd-run` prefix for a granted lease's command, or None where it runs as is: the
    daemon turned containment off (`[contain] mode`), or `contain.probe` finds no user scope
    this caller may use."""
    settings = answer.get("contain") or {}
    if settings.get("mode", "auto") == "off" or not contain.probe().enforced:
        return None
    return contain.lease_argv(
        request["lease_id"],
        weight=int(answer["lease"]["weight"]),
        memory=memory,
        hard_cpu=bool(settings.get("hard_cpu")),
    )


def child_env(lease_id: str, granted: dict, count: int, *, job_env: bool) -> dict[str, str]:
    """The command's environment: ours, the lease's `LEASEHOLD_*` variables, and (with
    `job_env`) the job-count variables the environment leaves unset."""
    env = {
        **os.environ,
        "LEASEHOLD_LEASE_ID": lease_id,
        "LEASEHOLD_GRANT": str(granted["grant"]),
        "LEASEHOLD_POOL": granted["pool"],
        "LEASEHOLD_WEIGHT": str(granted["weight"]),
    }
    if job_env:
        env |= jobs.job_env(count, os.environ)
    return env


def jobserver_env(env: Mapping[str, str], count: int, fifo: Path) -> dict[str, str]:
    """`MAKEFLAGS` naming the jobserver, and the same in `CARGO_MAKEFLAGS` and `MFLAGS` where
    they are set (cargo reads those first, so an outer build's would win)."""
    flags = jobs.makeflags(env.get("MAKEFLAGS"), count, fifo)
    out = {"MAKEFLAGS": flags}
    for name in ("CARGO_MAKEFLAGS", "MFLAGS"):
        if env.get(name):
            out[name] = jobs.makeflags(env[name], count, fifo)
    return out


def _supervise(client, child, lease, request, kind, ttl, interval, grace_s) -> int:
    """Renew the lease while `child` runs; stop its process group when the lease is revoked or
    lost; release the lease once it is done. The child's exit code, or `EXIT_REVOKED`."""
    revoked: str | None = None
    try:
        with _forwarding(child.pid):
            while True:
                try:
                    code = child.wait(timeout=interval)
                    break
                except subprocess.TimeoutExpired:
                    pass
                renewal = lease.renew()
                status = None if renewal is None else renewal["status"]
                if status == "revoked":
                    revoked = renewal.get("reason") or "revoked"
                elif kind in PREEMPTIBLE and (
                    status == "unknown"
                    or (status is None and time.monotonic() - lease.last_ok > ttl)
                ):
                    revoked = "the lease was lost"
                elif status == "unknown":
                    with contextlib.suppress(Unavailable, ApiError):
                        client.acquire(**request, wait_s=0)
                    _say("the daemon had lost the lease; asked for it again")
                if revoked is not None:
                    _say(f"lease revoked ({revoked}); stopping the command")
                    code = _stop_group(child, grace_s, lease, interval)
                    break
            if revoked is None and _group_alive(child.pid):
                _stop_group(child, grace_s, lease, interval)
    finally:
        lease.release()
    if revoked is not None:
        return EXIT_REVOKED
    return code if code >= 0 else 128 - code
