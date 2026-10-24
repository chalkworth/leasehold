"""Leases for Python callers: `lease()` takes one, waiting in line if need be, and a thread of the
lease's own keeps it renewed until it is released.

* The wait gives its place in line back however it ends without a grant: `Busy` when `timeout`
  is up, `Cancelled` when `cancel` is set, and anything raised while it waits (an interrupt).
  A refusal, or an error answer under 500, is `Refused`; an error answer of 500 or more is
  `DaemonError`: a daemon that answers is never taken for none.
* With no daemon, `if_unavailable="run"` gives an unleased `Lease` (`held` false) and `"fail"`
  raises `Unavailable`.
* The lease is renewed every TTL/3, counted from the start of the last renewal, and a renewal
  waits at most TTL/3 for its answer, so one renewal that goes unanswered never costs the lease.
* A revoked lease (only `lend` is ever revoked), or a `lend` lease lost to its TTL, is `lost`.
  A `work` or `external` lease the daemon no longer knows is asked for again; refused, it is
  `lost`. `on_lost` hears either, once, on the renewal thread. Nothing is raised into the
  caller's thread and no signal handler is installed: `check()` raises `LeaseLost` when asked.
* Leaving the `with` block, or `release()`, stops the renewals and releases the lease, however
  the block ends.
* A lease that ran a contained command (`popen`) sends what the command used with its release
  (`leasehold.measure`): the last such command's, when it ran more than one.
* `parked` says whether the daemon's memory net (`leasehold.net`) has paused the lease's
  command, as of the last renewal; the lease's `_say` hears when it is paused and when it runs
  again.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leasehold import client as _client
from leasehold import discovery, jobs, weights
from leasehold.kinds import PREEMPTIBLE

__all__ = [
    "Busy",
    "Cancelled",
    "DaemonError",
    "Lease",
    "LeaseError",
    "LeaseLost",
    "Refused",
    "Unavailable",
    "Waiting",
    "child_env",
    "find_daemon",
    "judge",
    "lease",
    "lease_request",
    "own_env",
    "wait",
]

#: The most one acquire request waits before asking again (each ask keeps the place in line).
POLL_S = 20.0
#: The most one ask waits while a `cancel` event may end the wait.
CANCEL_POLL_S = 1.0
#: While nothing changes in line, how often a wait says it is still waiting.
PROGRESS_S = 20.0
#: How often a busy follower (`leasehold.follow`) is stepped between renewals.
_FOLLOW_S = 0.2

_log = logging.getLogger("leasehold")


class LeaseError(Exception):
    """A lease could not be had or kept."""


class Busy(LeaseError):
    """The wait in line timed out; the place was given back. `position` and `blocked` are the
    last the daemon said, `answer` its last answer."""

    def __init__(self, waited_s: float, answer: dict) -> None:
        super().__init__(f"gave up after {waited_s:g}s in line")
        self.waited_s = waited_s
        self.position: int | None = answer.get("position")
        self.blocked: str | None = answer.get("blocked")
        self.answer = answer


class Refused(LeaseError):
    """The daemon said no: a `refused` answer (`status` None), or an error answer under 500.
    `code` is the daemon's code for the refusal, when it gives one (`quota_exceeded`), and
    `answer` the daemon's whole answer."""

    def __init__(self, reason: str, *, status: int | None = None, answer: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.answer = answer or {"reason": reason, "status": status}
        self.code: str | None = self.answer.get("code")


class DaemonError(LeaseError):
    """The daemon answered with an error of 500 or more, a fault of its own."""

    def __init__(self, reason: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class Unavailable(LeaseError, _client.Unavailable):
    """No daemon could be found or reached, with `if_unavailable="fail"`."""


class Cancelled(LeaseError):
    """The `cancel` event was set while waiting in line; the place was given back."""


class LeaseLost(LeaseError):
    """The lease was revoked or lost for good (`Lease.lost` is why)."""


@dataclass(frozen=True)
class Waiting:
    """Where a wait in line stands: its place, what blocks it (None: nothing named), and the
    seconds waited so far."""

    position: int | None
    blocked: str | None
    waited_s: float


def find_daemon(socket: str | os.PathLike | None = None, *, env=None) -> Path | None:
    """The socket of the daemon to use, as the `leasehold` command finds it
    (`leasehold.discovery`): `socket` when given, else one named in `env` (default ours), else
    the first daemon that answers; None when none does."""
    found = discovery.candidates(
        None if socket is None else str(socket), None, os.environ if env is None else env
    )
    return discovery.choose(found)


def lease_request(
    *,
    kind: str = "work",
    pool: str | None = None,
    weight: int = 1,
    labels: dict[str, str] | None = None,
    session: str | None = None,
    ttl_s: float | None = None,
    memory: str | None = None,
    max_weight: int | None = None,
    memory_per_slot: str | None = None,
    rule: str | None = None,
    repo: str | None = None,
) -> dict:
    """An acquire request for a new lease of its own id: `weight` to `max_weight` slots (None,
    or `weight`: no range is sent). `memory` (a size such as `4g`) is the memory the work
    declares in all, `memory_per_slot` for each slot it is granted; None declares none. `rule`
    (`shell.Rule.key`) and `repo` are what the work's outcome will be filed under, so a daemon
    can count what that rule's past runs there used (None: not sent). It never
    asks for an elastic lease: `hold` makes its lease elastic later, once the command's jobs
    show they follow the jobserver (`leasehold.follow`)."""
    if max_weight is not None and max_weight < weight:
        raise ValueError(
            f"max_weight {max_weight} is below weight {weight}; range must be (least, most)"
        )
    ranged = max_weight is not None and max_weight > weight
    return {
        "kind": kind,
        "pool": pool,
        "weight": weight,
        "labels": labels or {},
        "lease_id": uuid.uuid4().hex,
        "session": session,
        "ttl_s": ttl_s,
        "memory": memory,
        "max_weight": max_weight if ranged else None,
        "memory_per_slot": memory_per_slot,
        "rule": rule,
        "repo": repo,
    }


def wait(
    client,
    request: dict,
    timeout_s: float | None,
    *,
    on_wait: Callable[[Waiting], object] | None = None,
    cancel: threading.Event | None = None,
) -> dict:
    """Ask for `request`'s lease, waiting in line (each ask keeps the place) until it is
    granted: the daemon's answer. `on_wait` hears where the wait stands as it starts, whenever
    the place or what blocks it changes, and at least every `PROGRESS_S`. Raises `Busy` once
    `timeout_s` is up, `Cancelled` once `cancel` is set, `Refused`, `DaemonError`, or the
    client's `Unavailable` when no daemon answers. Every way out but a grant, a refusal and an
    unreachable daemon gives the place back first."""
    start = time.monotonic()
    deadline = None if timeout_s is None else start + timeout_s
    poll = POLL_S if cancel is None else CANCEL_POLL_S
    waited = False
    last: tuple | None = None
    last_said = start
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("cancelled while waiting in line")
            left = poll if deadline is None else max(0.0, deadline - time.monotonic())
            # The first ask answers at once, so a wait is reported as it starts.
            answer = client.acquire(**request, wait_s=min(poll, left) if waited else 0.0)
            status = answer["status"]
            if status == "granted":
                return answer
            if status == "refused":
                raise Refused(str(answer.get("reason")), answer=answer)
            now = time.monotonic()
            where = (answer.get("position"), answer.get("blocked"))
            if on_wait is not None and (where != last or now - last_said >= PROGRESS_S):
                on_wait(Waiting(*where, waited_s=now - start))
                last, last_said = where, now
            waited = True
            if deadline is not None and time.monotonic() >= deadline:
                raise Busy(timeout_s, answer)
    except _client.ApiError as exc:
        if exc.status >= 500:
            _give_back(client, request)
            raise DaemonError(str(exc), exc.status) from exc
        raise Refused(str(exc), status=exc.status) from exc
    except (Refused, _client.Unavailable):
        raise
    except BaseException:
        _give_back(client, request)
        raise


def _give_back(client, request: dict) -> None:
    with contextlib.suppress(_client.Unavailable, _client.ApiError):
        client.release(request["lease_id"])


def judge(kind: str, renewal: dict | None, silent_s: float, ttl: float) -> str:
    """What a renewal's answer (None: none came in time; `silent_s` since the last that did)
    means for a lease of `kind`: "ok"; "revoked"; "lost", a `lend` lease the daemon no longer
    knows or that has had no answer for longer than its TTL; "ask", a `work` or `external`
    lease the daemon no longer knows, to be asked for again; else "wait"."""
    status = None if renewal is None else renewal["status"]
    if status in ("ok", "revoked"):
        return status
    if kind in PREEMPTIBLE and (status == "unknown" or (status is None and silent_s > ttl)):
        return "lost"
    return "ask" if status == "unknown" else "wait"


def own_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`env` (default ours) as every command run under a lease, or unleased, starts from:
    without `LEASEHOLD_WANT`, which is what the lease was asked for, not its command."""
    return {k: v for k, v in (os.environ if env is None else env).items() if k != weights.ENV_WANT}


def child_env(
    lease_id: str,
    granted: dict,
    count: int,
    *,
    job_env: bool,
    threads: jobs.Threads = "jobs",
    env: Mapping[str, str] | None = None,
    socket: str | None = None,
    elastic_jobs: int | None = None,
) -> dict[str, str]:
    """The command's environment: `env` (default ours) without `LEASEHOLD_WANT` (what this
    lease was asked for is not what the commands it runs ask for), with the lease's
    `LEASEHOLD_*` variables (`LEASEHOLD_WEIGHT` is the weight granted), and with `job_env` the
    job-count and thread-count variables for this lease (`jobs.sized_env`). Without `job_env`,
    the enclosing lease's sizing is removed (`jobs.unsized_env`). Which variables an enclosing
    lease set is read from `env` before this lease's id is added, so it counts only when `env`
    holds a `LEASEHOLD_LEASE_ID`. `socket`, the daemon's, is passed on as `LEASEHOLD_SOCKET`,
    made absolute. `elastic_jobs`, for a lease whose jobserver may grow to that many jobs, is
    passed to `jobs.sized_env`."""
    outer = own_env(env)
    lease = {
        "LEASEHOLD_LEASE_ID": lease_id,
        "LEASEHOLD_GRANT": str(granted["grant"]),
        "LEASEHOLD_POOL": granted["pool"],
        "LEASEHOLD_WEIGHT": str(granted["weight"]),
    }
    if socket:
        lease["LEASEHOLD_SOCKET"] = os.path.abspath(socket)
    if job_env:
        return {**jobs.sized_env(outer, count, threads, elastic_jobs=elastic_jobs), **lease}
    return {**jobs.unsized_env(outer), **lease}


#: The fields of a renewal's answer that say what the lease holds now.
_HELD = ("weight", "min_weight", "max_weight", "pool", "jobs")


class Lease:
    """A lease, granted or (`held` false) run without one. A granted lease is renewed from a
    thread of its own until `release()`, which leaving its `with` block calls.

    `weight` and `jobs` are what it holds now, `min_weight` and `max_weight` its range.
    `lost` is None, or why the lease was revoked or lost for good. `oom_killed` says whether
    the OOM killer ended a process of the last contained command run under it. A `work` or
    `external` lease the daemon forgot and grants again holds the new grant (its
    `LEASEHOLD_GRANT` included); while it waits in line again, it keeps its last grant, as a
    command under `hold` runs on."""

    def __init__(
        self,
        client=None,
        request: dict | None = None,
        answer: dict | None = None,
        *,
        on_lost: Callable[[str], object] | None = None,
        _say: Callable[[str], object] = lambda _: None,
        _rule: str | None = None,
    ) -> None:
        self.lost: str | None = None
        #: The `[commands]` rule that sized the lease (`shell.Rule.key`), for its outcome.
        self._rule = _rule
        #: What its last contained command used (`supervise.Command`), sent with the release.
        self._outcome: dict | None = None
        #: Whether the OOM killer ended a process of the last contained command run under it
        #: (`supervise.Command.oom_killed`): what `run` cannot say in its exit status.
        self.oom_killed = False
        #: Whether the daemon's memory net has the lease's command paused, as the last renewal
        #: said.
        self.parked = False
        self._on_lost = on_lost
        self._say = _say
        self._stop = threading.Event()
        #: Set once `lost` is.
        self._lost_event = threading.Event()
        #: Set to wake the renewal thread: a release, or a follower to step.
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._follower = None
        if client is None or request is None or answer is None:
            self.held = False
            self.lease_id = self.kind = None
            self._granted: dict = {}
            self._answer: dict = {}
            return
        self.held = True
        self._client = client
        self._request = request
        self._answer = answer
        self._granted = dict(answer["lease"])
        self.lease_id: str | None = request["lease_id"]
        self.kind: str | None = request["kind"]
        self._take(self._granted)
        self._ttl = float(self._granted["ttl_s"])
        self._interval = max(0.05, self._ttl / 3)
        self._quick = client
        if isinstance(client, _client.Client):
            # Calls that must not hold the next renewal up wait at most one interval.
            self._quick = _client.Client(client.path, timeout=min(client.timeout, self._interval))
        self._thread = threading.Thread(
            target=self._renew_until_released, name=f"leasehold-renew-{self.lease_id[:8]}",
            daemon=True,
        )  # fmt: skip
        self._thread.start()

    @property
    def pool(self) -> str | None:
        return self._granted.get("pool")

    @property
    def weight(self) -> int | None:
        return self._granted.get("weight")

    @property
    def min_weight(self) -> int | None:
        return self._granted.get("min_weight")

    @property
    def max_weight(self) -> int | None:
        return self._granted.get("max_weight")

    @property
    def jobs(self) -> int | None:
        return self._granted.get("jobs")

    def _take(self, answer: dict, base: dict | None = None) -> None:
        """Hold what `answer` says, over `base` (default what is held now), in one assignment."""
        updated = dict(self._granted if base is None else base)
        for name in _HELD:
            if answer.get(name) is not None:
                updated[name] = answer[name]
        weight = int(updated["weight"])
        updated["weight"] = weight
        updated["min_weight"] = int(updated.get("min_weight") or weight)
        updated["max_weight"] = int(updated.get("max_weight") or weight)
        updated["jobs"] = int(updated.get("jobs") or weight)
        self._granted = updated

    def _follow(self, follower, every: float | None) -> None:
        """Step `follower` (`leasehold.follow`) from the renewal thread, renewing at least every
        `every` seconds; None stops it."""
        self._follower = follower
        self._interval = max(0.05, self._ttl / 3 if every is None else min(self._ttl / 3, every))
        self._wake.set()

    def _renew_until_released(self) -> None:
        last_ok = last_attempt = time.monotonic()
        next_attempt = last_ok + self._interval
        pending = queued = False
        while True:
            follower = self._follower
            wait = next_attempt - time.monotonic()
            if follower is not None and follower.busy():
                wait = min(wait, _FOLLOW_S)
            self._wake.wait(max(0.0, wait))
            self._wake.clear()
            if self._stop.is_set():
                return
            # A shorter interval (a follower's) counts from the start of the last renewal.
            next_attempt = min(next_attempt, last_attempt + self._interval)
            if time.monotonic() < next_attempt:
                if follower is not None:
                    follower.step()
                continue
            last_attempt = time.monotonic()
            next_attempt = last_attempt + self._interval
            try:
                renewal = self._quick.renew(self.lease_id)
            except (_client.Unavailable, _client.ApiError):
                renewal = None
            verdict = judge(self.kind, renewal, time.monotonic() - last_ok, self._ttl)
            if verdict in ("ok", "revoked"):
                last_ok = time.monotonic()
            if verdict == "ok" and not pending:
                self._take(renewal)
                self._heard_parked(bool(renewal.get("parked")))
                if follower is not None:
                    follower.heard(renewal)
                    follower.step()
            elif verdict == "revoked":
                # Renewals go on, so the weight stays held while the caller stops its work, until
                # the revoke grace ends and the daemon no longer knows the lease ("lost").
                self._lose(renewal.get("reason") or "revoked")
            elif verdict == "lost":
                self._lose("the lease was lost")
                return
            if verdict == "ask":
                pending = True
            if pending and self.lost is None and not self._stop.is_set():
                again = self._ask_again()
                status = again.get("status")
                if status in ("granted", "queued") and self._stop.is_set():
                    # Released while it asked: the grant, or the place in line, goes back too.
                    with contextlib.suppress(_client.Unavailable, _client.ApiError):
                        self._client.release(self.lease_id)
                    return
                if status == "granted":
                    self._take(again["lease"], base=again["lease"])
                    pending = queued = False
                    self._say("the daemon had lost the lease; asked for it again")
                elif status == "queued" and not queued:
                    queued = True
                    self._say(
                        "the daemon had lost the lease; asked for it again, and waiting in line "
                        f"(place {again.get('position')})"
                    )
                elif status == "refused":
                    self._lose(
                        f"the daemon had lost the lease and now refuses it ({again.get('reason')})"
                    )
                    return

    def _heard_parked(self, parked: bool) -> None:
        if parked == self.parked:
            return
        self.parked = parked
        if parked:
            self._say(
                "memory ran short, so the command is paused and its memory moved to swap; it "
                "runs again once memory frees"
            )
        else:
            self._say("the command runs again")

    def _ask_again(self) -> dict:
        try:
            return self._quick.acquire(**self._request, wait_s=0)
        except (_client.Unavailable, _client.ApiError):
            return {}

    def _lose(self, reason: str) -> None:
        if self.lost is not None:
            return
        self.lost = reason
        self._lost_event.set()
        if self._on_lost is not None:
            try:
                self._on_lost(reason)
            except Exception:  # noqa: BLE001 -- a caller's callback must not end the renewals
                _log.exception("leasehold: on_lost raised")

    def check(self) -> None:
        """Raise `LeaseLost` if the lease was revoked or lost for good."""
        if self.lost is not None:
            raise LeaseLost(self.lost)

    def env(
        self,
        base: Mapping[str, str] | None = None,
        *,
        job_env: bool = True,
        threads: jobs.Threads = "jobs",
        elastic_jobs: int | None = None,
    ) -> dict[str, str]:
        """The environment for a command run under this lease, from `base` (default ours): the
        lease's `LEASEHOLD_*` variables and, with `job_env`, job and thread counts for the
        weight it holds now (`child_env`; `elastic_jobs` is the most jobs a jobserver may grow
        to). Unleased, `base` without `LEASEHOLD_WANT`."""
        if not self.held:
            return own_env(base)
        snapshot = self._granted
        return child_env(
            self.lease_id,
            snapshot,
            snapshot["jobs"],
            job_env=job_env,
            threads=threads,
            env=base,
            socket=getattr(self._client, "path", None),
            elastic_jobs=elastic_jobs,
        )

    def popen(
        self,
        argv: Sequence[str],
        *,
        jobserver: bool = False,
        elastic: bool | None = None,
        contain: bool = True,
        grace: float = 30.0,
        job_env: bool = True,
        threads: jobs.Threads = "jobs",
        memory: str | None = None,
        memory_per_slot: str | None = None,
        env: Mapping[str, str] | None = None,
        **popen,
    ):
        """Start `argv` under this lease (`leasehold.supervise`): a `Command` leading a process
        group of its own, in the lease's environment (`env()`, from `env`), in a systemd scope
        sized to the lease where the daemon and this caller allow one and `contain`, with a
        make jobserver when `jobserver` (a range is then elastic unless `elastic` is false). A
        revoked `lend` lease stops its group, SIGTERM then SIGKILL after `grace` seconds, and so
        does its end. `popen` goes to `subprocess.Popen`. Raises `NotStarted`."""
        from leasehold.supervise import Command

        return Command(
            self, argv, jobserver=jobserver, elastic=elastic, contain=contain, grace_s=grace,
            job_env=job_env, threads=threads, memory=memory, memory_per_slot=memory_per_slot,
            env=env, **popen,
        )  # fmt: skip

    def run(self, argv: Sequence[str], **options) -> int:
        """`popen(argv, **options)` waited for: the command's exit status (negative: the signal
        that ended it). `oom_killed` then says whether the OOM killer ended any of its
        processes."""
        with self.popen(argv, **options) as command:
            return command.wait()

    def release(self) -> None:
        """Stop renewing and release the lease. Safe to call more than once."""
        if not self.held or self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self._interval + 1.0)
        with contextlib.suppress(_client.Unavailable, _client.ApiError):
            if isinstance(self._client, _client.Client):
                self._client.release(self.lease_id, outcome=self._outcome)
            else:  # a stand-in (`lease(client=...)`) need not take an outcome
                self._client.release(self.lease_id)

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def lease(
    weight: int | tuple[int, int] = 1,
    *,
    kind: str = "work",
    pool: str | None = None,
    labels: Mapping[str, str] | None = None,
    session: str | None = None,
    timeout: float | None = None,
    ttl: float | None = None,
    memory: str | None = None,
    memory_per_slot: str | None = None,
    if_unavailable: str = "run",
    on_wait: Callable[[Waiting], object] | None = None,
    on_lost: Callable[[str], object] | None = None,
    cancel: threading.Event | None = None,
    socket: str | os.PathLike | None = None,
    client=None,
) -> Lease:
    """Take a lease of `weight` slots, or of `(least, most)` slots granted as many as fit, of
    `kind` (`work`, `external`, `lend` or `maintenance`), waiting in line up to `timeout`
    seconds (None: until granted); the granted `Lease`, renewed until it is released. `memory`
    (a size such as `4g`) is the memory the work declares in all, `memory_per_slot` for each
    slot granted; `ttl` the lease's TTL in seconds (None: the daemon's). The daemon is the one
    at `socket`, else the one `find_daemon` finds; `client` replaces it with any object that has
    the methods `acquire`, `renew` and `release` of `leasehold.client.Client`. With no daemon,
    `if_unavailable` `run` gives an unleased `Lease`, `fail` raises `Unavailable`. Raises
    `Busy`, `Refused`, `DaemonError` or `Cancelled` as `wait` does."""
    if if_unavailable not in ("run", "fail"):
        raise ValueError(f"if_unavailable {if_unavailable!r} is not run or fail")
    least, most = (weight, weight) if isinstance(weight, int) else weight
    request = lease_request(
        kind=kind,
        pool=pool,
        weight=least,
        labels=dict(labels or {}),
        session=session,
        ttl_s=ttl,
        memory=memory,
        max_weight=most,
        memory_per_slot=memory_per_slot,
    )
    try:
        if client is None:
            try:
                path = find_daemon(socket)
            except discovery.DiscoveryError as exc:
                raise _client.Unavailable(f"{exc}; see `leasehold doctor`") from exc
            if path is None:
                raise _client.Unavailable(
                    "no leasehold daemon answers; start one with `leasehold serve --user` "
                    "(or `--system`), or see `leasehold doctor`"
                )
            client = _client.Client(path)
        answer = wait(client, request, timeout, on_wait=on_wait, cancel=cancel)
    except _client.Unavailable as exc:
        if if_unavailable == "fail":
            raise Unavailable(str(exc)) from exc
        return Lease()
    return Lease(client, request, answer, on_lost=on_lost)
