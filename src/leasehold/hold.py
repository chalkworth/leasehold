"""`leasehold hold -- cmd`: run one command under a lease.

Acquire (waiting in line if need be), start the command in its own process group, renew the
lease while it runs, release it when the command ends, and exit with the command's code. The
lease covers the whole process group: anything the command leaves running in it is stopped
when it ends. Run from a terminal's foreground, the command's group gets the terminal while it
runs (`leasehold.terminal`); otherwise it runs in a session of its own.

* A revoked lease (only `lend` is ever revoked), or a `lend` lease lost to its TTL, stops the
  group -- SIGTERM, then SIGKILL after the grace -- and exits `EXIT_REVOKED`.
* Waiting longer than `--timeout` exits `EXIT_BUSY` (75: busy, not failed), the place in line
  given back, after a line that says who holds the machine and when to retry
  (`leasehold.busy`). Under an agent whose tool timeout `LEASEHOLD_TOOL_TIMEOUT_S` gives, the
  wait is at most half of that (`leasehold.toolwait`). A command granted after a wait of
  `busy.WAIT_NOTED_S` or more says how long it waited, and behind what. A request that could
  never be granted, or that the daemon answers with an error (a 5xx included), exits
  `EXIT_REFUSED`: a daemon that answers is never taken for none.
* With no daemon, the command runs without a lease, after a warning (`run_unleased`); with
  `if_unavailable="fail"`, `hold` exits `EXIT_UNAVAILABLE` instead.
* A `work` or `external` lease the daemon no longer knows (it was down longer than the TTL) is
  asked for again; the command keeps running, without a lease if the daemon now refuses it.
* The lease is renewed every TTL/3, counted from the start of the last renewal, and a renewal
  waits at most TTL/3 for its answer, so one renewal that goes unanswered never costs the lease.
* SIGINT, SIGTERM or SIGHUP before the command runs (while waiting in line, or while it is
  started) gives back the place in line or the lease, stops anything already started, and exits
  128 + the signal, except that SIGINT ends `hold` by SIGINT (`exits.end_by`). Once the command
  runs, they are sent to its group. With the terminal, a command killed by SIGINT or SIGQUIT
  (Ctrl-C, Ctrl-backslash) ends `hold` by the same signal, so the shell that ran `hold` sees
  the interrupt and stops the loop or script it is in; otherwise a command killed by a signal
  makes `hold` exit 128 + the signal.

Where the daemon allows it and the caller runs in the user's own session, the command is started
in a systemd user scope sized to the lease (`leasehold.contain`); elsewhere it runs as is. What a
contained command used is sent with the release (`leasehold.measure`), filed under the
`[commands]` rule that sized it when `hold` was given no weight.
When memory ended the command, `hold` says so as the last line on stderr, in the memory
verdict's words (`leasehold.verdict`): `leasehold: ... This is not a test failure.` and what
to do next. Its exit code stays the command's own. For a contained command the OOM killer
reached, the line before it gives the peak, the memory leasehold counted for it and a
`--memory` that would count more than the peak (`oom_line`), unless the command's own hard cap
ended it.

`--memory-max` (or the matching rule's `memory_max`, else `[commands] memory_max`) caps a
contained command's memory: past it the kernel kills the whole command, and the verdict names
the cap. Where the cap cannot hold (no scope here, no lease, or a platform with no cap), `hold`
says so before the command runs, and runs it uncapped.

A lease asked for as a range (`--weight 2-4`) is sized to the weight it was granted: its job
and thread counts, jobserver, `LEASEHOLD_WEIGHT`, `CPUWeight` and, with a memory per slot,
`MemoryHigh`. The command does not inherit `LEASEHOLD_WANT` (`leasehold.weights`).

A range run with a jobserver is elastic unless told not to: once the command's jobs take
jobserver tokens, the lease is made elastic, and its jobserver's tokens and its scope follow
the lease's target as it grows and shrinks (`leasehold.follow`). Such a lease is renewed at
least every `[elastic] settle_s`, and its command gets no `CMAKE_BUILD_PARALLEL_LEVEL` and a
`CARGO_BUILD_JOBS` of the most jobs it may grow to (`jobs.job_env`). `--elastic` without
`--jobserver` is refused (exit 2), and a command line giving its own `-jN` is warned about.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shlex
import signal
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from leasehold import (
    busy,
    contain,
    jobs,
    leasing,
    maintenance,
    measure,
    shell,
    supervise,
    terminal,
    toolwait,
    weights,
)
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import (
    EXIT_BUSY,
    EXIT_NOT_STARTED,
    EXIT_REFUSED,
    EXIT_REVOKED,
    EXIT_UNAVAILABLE,
    end_by,
)
from leasehold.kinds import PREEMPTIBLE, WHOLE
from leasehold.leasing import child_env, lease_request

__all__ = [
    "EXIT_BUSY",
    "EXIT_NOT_STARTED",
    "EXIT_REFUSED",
    "EXIT_REVOKED",
    "EXIT_UNAVAILABLE",
    "child_env",
    "hold",
    "lease_request",
    "wait_for_grant",
    "waiting_line",
]
#: The signals that interrupt `hold` before its command runs, and are sent on to it after.
_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
#: The signals that, killing the command on a terminal, end `hold` the same way.
_INTERRUPTS = (signal.SIGINT, signal.SIGQUIT)


def _say(message: str) -> None:
    sys.stderr.write(f"leasehold hold: {message}\n")
    sys.stderr.flush()


@dataclasses.dataclass
class Waited:
    """How long a request waited in line (`seconds`), and the lease ahead of it when it began
    to wait (`busy.ahead`; None: it did not wait, or the daemon did not say)."""

    seconds: float = 0.0
    behind: str | None = None


def wait_for_grant(
    client: Client,
    request: dict,
    timeout_s: float | None,
    say=_say,
    *,
    quiet_unavailable: bool = False,
    fault: int = EXIT_UNAVAILABLE,
    waited: Waited | None = None,
) -> tuple[int | None, dict]:
    """Ask for `request`'s lease, waiting in line (`leasing.wait`) until it is granted or
    `timeout_s` is up. `(None, answer)` on a grant, else `(exit code, answer)`: `EXIT_BUSY`
    with the place in line given back; `EXIT_REFUSED` when the daemon said no (a `refused`
    answer, or an error answer under 500); `EXIT_UNAVAILABLE` when no daemon answers (said,
    unless `quiet_unavailable`); `fault` when the daemon answers with an error of 500 or more,
    a fault of its own rather than an answer to the request (`EXIT_UNAVAILABLE` unless given).
    For an error answer, `answer["reason"]` is the daemon's error text. Giving up while a
    maintenance lease holds the machine, or drains it, it also says who holds it and until
    when, as one line a script can read (`maintenance.holder_line`). While it waits it says
    its place and why whenever they change, and at least every `leasing.PROGRESS_S` with the
    time waited so far. `waited`, when given, is filled in with how long it waited and the lease
    ahead of it."""
    said: list[str] = []
    started = time.monotonic()

    def waiting(where: leasing.Waiting) -> None:
        if waited is not None and not said:
            waited.behind = busy.ahead(client, request.get("pool"))
        line = waiting_line(request, {"position": where.position, "blocked": where.blocked})
        if said and said[-1] == line:
            say(f"{line}; {where.waited_s:.0f}s so far")
        else:
            say(line)
            said.append(line)

    try:
        return None, leasing.wait(client, request, timeout_s, on_wait=waiting)
    except leasing.Busy as exc:
        say(f"gave up after {timeout_s:g}s in line")
        if exc.answer.get("maintenance"):
            say(maintenance.holder_line(exc.answer["maintenance"]))
        return EXIT_BUSY, exc.answer
    except leasing.Refused as exc:
        say(f"refused: {exc.reason}" if exc.status is None else exc.reason)
        return EXIT_REFUSED, exc.answer
    except leasing.DaemonError as exc:
        say(exc.reason)
        return fault, {"reason": exc.reason, "status": exc.status}
    except Unavailable as exc:
        if not quiet_unavailable:
            say(f"{exc}; is a daemon running? (`leasehold doctor` says what it finds)")
        return EXIT_UNAVAILABLE, {}
    finally:
        if waited is not None:
            waited.seconds = time.monotonic() - started


def waiting_line(request: dict, answer: dict) -> str:
    """What `hold` and `acquire` print while queued: what they wait for, their place and why."""
    weight = request.get("weight", 1)
    most = request.get("max_weight") or weight
    slots = "1 slot" if most == weight == 1 else f"{weights.text(weight, most)} slots"
    if request.get("kind") in WHOLE:
        slots = "the whole machine"
    where = f" in pool {request['pool']}" if request.get("pool") else ""
    line = f"waiting for {slots}{where} ({request['kind']}): place {answer.get('position')}"
    blocked = answer.get("blocked")
    return f"{line}; {blocked}" if blocked else line


class _Interrupted(BaseException):
    """`hold` got SIGINT, SIGTERM or SIGHUP (`signum`) before its command ran."""

    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """While inside, SIGINT, SIGTERM and SIGHUP raise `_Interrupted`."""

    def interrupt(sig, _frame):
        raise _Interrupted(sig)

    previous = {s: signal.signal(s, interrupt) for s in _SIGNALS}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)


def _interrupted(sig: int) -> int:
    """How `hold` ends when `sig` interrupts it before its command runs: it says so, then ends
    by SIGINT for SIGINT, else exits 128 + `sig`."""
    _say(f"interrupted ({signal.Signals(sig).name}) before the command ran")
    return end_by(sig) if sig == signal.SIGINT else 128 + sig


def _ended(returncode: int, tty: bool) -> int:
    """`hold`'s exit for a command that ended with `returncode` (negative: killed by that
    signal). With the terminal (`tty`), a command killed by SIGINT or SIGQUIT ends this process
    by the same signal (`exits.end_by`); otherwise a signal's end is 128 + the signal."""
    if returncode >= 0:
        return returncode
    if tty and -returncode in _INTERRUPTS:
        return end_by(-returncode)
    return 128 - returncode


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
    threads: jobs.Threads | None = None,
    if_unavailable: str = "run",
    max_weight: int | None = None,
    memory_per_slot: str | None = None,
    elastic: bool | None = None,
    memory_max: str | None = None,
) -> int:
    """Run `argv` under a lease of `weight` to `max_weight` slots (None: `weight`), sized to
    the weight granted (`leasehold.jobs`): the job-count and thread-count variables unless
    `job_env` is false, a make jobserver when `jobserver` is true, and when contained a scope
    weighted to the lease and held to `memory` (a size such as `4g`), or to `memory_per_slot`
    for each slot granted, when given. Either is also declared with the request, so a daemon
    with a memory budget counts it. With no `weight`, the command is weighed as the daemon's
    `[commands]` rules weigh it (`auto_size`), range and memory included; with no `threads`,
    its thread count is the matching rule's `threads`, and "jobs" when no rule matches.
    With no daemon (`client` None: none could be found), `if_unavailable` `run` runs it without
    a lease (`run_unleased`), `fail` exits `EXIT_UNAVAILABLE`. A leased command gets the
    client's socket as `LEASEHOLD_SOCKET`, so a `hold` or shim inside it reaches the same
    daemon. `elastic` makes a range elastic once the command's jobs take jobserver tokens
    (`leasehold.follow`; None: whenever `jobserver` is true). It needs `jobserver`: without
    one, nothing makes the jobs follow the lease, so it exits 2.
    `memory_max` is the command's hard cap (a size, a share of the machine's memory such as
    `50%`, or `off`); None: the matching rule's when `hold` weighs the command, else the
    daemon's `[commands] memory_max`."""
    if not argv:
        _say("no command given (leasehold hold [options] -- cmd ...)")
        return 2
    if elastic and not jobserver:
        _say(
            "--elastic needs --jobserver: without a jobserver nothing makes the command's jobs "
            "follow the lease as it grows and shrinks"
        )
        return 2
    if jobserver and (count := jobs.own_job_count(argv)) is not None:
        _say(
            f"the command gives its own job count ({count}): a make given one runs its own "
            "jobserver and leaves the lease's, so it runs that many jobs (with no number, as "
            "many as it can) whatever the lease holds, and the lease keeps its size; leave "
            "the count out to use the lease's"
        )
    if weight is None and kind in WHOLE:
        weight = 1  # the daemon gives a maintenance lease the whole machine, whatever is asked
    request = None
    rule_key = None
    try:
        with _interruptible():
            # Rules are read for the weight, or for the threads the job variables use.
            if weight is None or (threads is None and job_env):
                rule = auto_size(client, argv) if client is not None else _UNSIZED
                if weight is None:
                    weight, max_weight = rule.weight, rule.max_weight
                    rule_key = rule.key
                    if memory is None and memory_per_slot is None:
                        memory, memory_per_slot = rule.memory, rule.memory_per_slot
                    if memory_max is None:
                        memory_max = rule.memory_max
                if threads is None:
                    threads = rule.threads
            threads = "jobs" if threads is None else threads
            if elastic is None:
                elastic = jobserver
            request = lease_request(
                kind=kind,
                pool=pool,
                weight=weight,
                labels=labels,
                session=session,
                ttl_s=ttl_s,
                memory=memory,
                max_weight=max_weight,
                memory_per_slot=memory_per_slot,
                rule=rule_key,
                repo=measure.repository() if rule_key is not None else None,
            )
            limit_s = toolwait.limit(os.environ, timeout_s)
            waited = Waited()
            if client is None:
                code, answer = EXIT_UNAVAILABLE, {}
            else:
                # A fault in the daemon (500 and over) is no answer to run unleased on: `hold`
                # refuses then, whatever `if_unavailable` says.
                code, answer = wait_for_grant(
                    client,
                    request,
                    limit_s,
                    _say,
                    quiet_unavailable=if_unavailable == "run",
                    fault=EXIT_REFUSED,
                    waited=waited,
                )
            if code == EXIT_UNAVAILABLE and if_unavailable == "run":
                _say("no daemon answers; running without a lease")
                return run_unleased(argv, grace_s=grace_s, memory_max=memory_max)
            program = Path(argv[0]).name
            if code == EXIT_BUSY and client is not None:
                if cut := toolwait.said(os.environ, timeout_s):
                    _say(cut)
                why, todo = busy.told(client, answer, pool=request["pool"], waited_s=limit_s)
                _say(f"not running {program}: {why}. {todo}")
            if code is not None:
                return code
            if waited.seconds >= busy.WAIT_NOTED_S:
                sys.stderr.write(busy.waited(program, waited.seconds, waited.behind) + "\n")
            return run_leased(
                client,
                argv,
                request,
                answer,
                grace_s=grace_s,
                job_env=job_env,
                jobserver=jobserver,
                memory=memory,
                threads=threads,
                memory_per_slot=memory_per_slot,
                elastic=elastic,
                rule=rule_key,
                memory_max=memory_max,
            )
    except _Interrupted as exc:
        if client is not None and request is not None:
            with contextlib.suppress(Unavailable, ApiError):
                client.release(request["lease_id"])
        return _interrupted(exc.signum)


#: What `auto_size` gives a command no rule matches: weight 1, no memory, "jobs" threads.
_UNSIZED = shell.Rule(programs=frozenset(), weight=1)


def auto_size(
    client: Client,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> shell.Rule:
    """The rule of the daemon's `[commands]` that `argv`, read as a command line, matches, as
    the agent hooks and `leasehold commands --test` match it, for its weights and memory. Its
    `threads` is the whole command line's (`shell.threads`), read with `env` and `cwd`
    (default ours): a count, "jobs", or a share of the job count ("jobs/N", "jobs/N:K").
    `_UNSIZED` when no rule matches, with the line's `threads` when a pytest run on it starts
    xdist workers, and `_UNSIZED` when the rules cannot be read."""
    try:
        rules = shell.served(client.commands()).rules
        found = list(shell.programs(shlex.join(argv)))
        matched = shell.match_programs(rules, found)
        count = shell.threads(
            rules, found, env=os.environ if env is None else env, cwd=cwd or Path.cwd()
        )
        if matched is None:
            return dataclasses.replace(_UNSIZED, threads=count or _UNSIZED.threads)
    except (
        Unavailable,
        ApiError,
        LookupError,
        ValueError,
        TypeError,
        AttributeError,
        OSError,
        re.error,
    ):
        return _UNSIZED
    return dataclasses.replace(matched.rule, threads=count or matched.rule.threads)


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
    threads: jobs.Threads = "jobs",
    memory_per_slot: str | None = None,
    elastic: bool = False,
    rule: str | None = None,
    memory_max: str | None = None,
) -> int:
    """`hold` once `request` is granted (`answer`): run `argv` under the lease
    (`supervise.Command`: sized to the weight granted, its thread pools to `threads`, held to
    `memory` or `memory_per_slot` when contained and capped at `memory_max`, a jobserver with
    `jobserver`, elastic with `elastic`), renewing the lease, and release it when the command
    ends, with what a contained command used filed under `rule` (`shell.Rule.key`; None: no
    rule sized it); the command's exit code (`_ended`), or `EXIT_REVOKED`. SIGINT, SIGTERM or
    SIGHUP before the command runs gives the lease back and stops what was started of it."""

    def lost(reason: str) -> None:
        if request["kind"] not in PREEMPTIBLE:
            _say(f"{reason}; the command runs on without a lease")

    lease = leasing.Lease(client, request, answer, on_lost=lost, _say=_say, _rule=rule)
    return _run(
        lease,
        argv,
        grace_s=grace_s,
        job_env=job_env,
        jobserver=jobserver,
        memory=memory,
        threads=threads,
        memory_per_slot=memory_per_slot,
        elastic=elastic,
        memory_max=memory_max,
    )


def run_unleased(
    argv: Sequence[str], *, grace_s: float = 30.0, memory_max: str | None = None
) -> int:
    """Run `argv` as `hold` runs a leased command, but with no lease, scope or job variables
    (its environment is `leasing.own_env`): its own process group (with the terminal, when
    this process has it), signals forwarded, and what it leaves in the group stopped when it
    ends. The command's exit code (`_ended`). A `memory_max` asked for cannot hold without a
    scope, which `hold` says."""
    return _run(leasing.Lease(), argv, grace_s=grace_s, memory_max=memory_max)


def _run(lease: leasing.Lease, argv: Sequence[str], **options) -> int:
    """Run `argv` under `lease` (`supervise.Command`), its group given the terminal when this
    process has it and sent the signals this process gets; release the lease when it ends."""
    tty = terminal.owned()
    command = interrupted = None
    try:
        with contextlib.ExitStack() as stack:
            relay = stack.enter_context(_relayed())
            try:
                command = stack.enter_context(
                    supervise.Command(
                        lease, argv, tty=tty, cancel_fd=relay.wake_fd, say=_say, **options
                    )
                )
            except contain.Cancelled:
                interrupted = relay.received
            except supervise.NotStarted as exc:
                _say(str(exc))
                return EXIT_NOT_STARTED
            if command is not None:
                interrupted = relay.forward_to(command.pid)
                if interrupted is None:
                    if command.uncontained:
                        _say(command.uncontained)
                    if command.uncapped:
                        _say(command.uncapped)
                    if tty:
                        stack.enter_context(foreground(command.pid))
                    code = command.wait()
                    capped = command.memory is not None and command.memory.kind == "cap"
                    if (
                        command.oom_killed
                        and not capped
                        and (line := oom_line(command.peak_bytes, _counted(lease)))
                    ):
                        _say(line)
                    if command.memory is not None:
                        sys.stderr.write(command.memory.agent + "\n")
                        sys.stderr.flush()
    finally:
        lease.release()
    if interrupted is not None:
        return _interrupted(interrupted)
    if command.stopped_for is not None:
        return EXIT_REVOKED
    return _ended(code, tty)


#: The step a suggested `--memory` is rounded up to: 256 MiB.
_MEMORY_STEP = 256 << 20


def suggested_memory(peak: int) -> str:
    """A `--memory` size for a command that peaked at `peak` bytes: about 15% more, rounded up
    to `_MEMORY_STEP`, in GiB when whole, else MiB."""
    size = -(-int(peak * 1.15) // _MEMORY_STEP) * _MEMORY_STEP
    return f"{size >> 30}g" if size % (1 << 30) == 0 else f"{size >> 20}m"


def _gib(size: int) -> str:
    return f"{size / 2**30:.1f} GiB"


def _counted(lease: leasing.Lease) -> int | None:
    """The memory the daemon counted for `lease` at its weight now: what it counted at the grant,
    which is the declared total, or else a size per slot (declared or learned) times the weight.
    None for a daemon that does not say."""
    first = (lease._answer or {}).get("lease") or {}
    counted = first.get("memory_counted_bytes")
    if counted is None or not first.get("weight") or lease.weight is None:
        return None
    if first.get("memory_bytes") is not None and first.get("memory_per_slot_bytes") is None:
        return counted
    return counted * lease.weight // first["weight"]


def oom_line(peak: int | None, counted: int | None) -> str | None:
    """What `hold` says, before the memory verdict, when the kernel's OOM killer ended a process
    of its command: the command's peak, the memory leasehold counted for it (0: none, as without
    `[memory]`; None: unknown), and a `--memory` above the peak (`suggested_memory`). The peak
    is at most what the command was let use before the kill, so that size is a least. None
    when there is nothing to say."""
    said = []
    if peak is not None:
        said.append(f"its peak was {_gib(peak)}")
    if counted:
        said.append(f"leasehold counted {_gib(counted)} for it")
    elif counted == 0:
        said.append("leasehold counted no memory for it")
    if not said:
        return None
    line = ", and ".join(said)
    if peak is not None:
        line += (
            f". Declare at least --memory {suggested_memory(peak)} (or a rule's `memory`) so "
            "that leasehold counts what it uses"
        )
    return line


@contextlib.contextmanager
def foreground(pgid: int) -> Iterator[None]:
    """While inside, the group `pgid` (started with the terminal) has the terminal; the
    terminal comes back when it is stopped (`leasehold.terminal`) and on leaving."""
    with terminal.follow_stops(pgid):
        terminal.hand_to(pgid)
        try:
            yield
        finally:
            terminal.take_back(pgid)


class _Relay:
    """Where SIGTERM, SIGINT and SIGHUP go while a command is started: the first of them is kept
    (`received`, and noted on `wake_fd`) until `forward_to` names the command's group, and from
    then on each is sent to that group."""

    def __init__(self) -> None:
        self.received: int | None = None
        self.wake_fd, self._wake = os.pipe()
        os.set_blocking(self._wake, False)
        self._pgid: int | None = None

    def __call__(self, sig, _frame) -> None:
        if self._pgid is not None:
            supervise.signal_group(self._pgid, sig)
            return
        if self.received is None:
            self.received = sig
        with contextlib.suppress(OSError):
            os.write(self._wake, b"x")

    def forward_to(self, pgid: int) -> int | None:
        """Send each signal from now on to the group `pgid`; the one kept before, if any."""
        self._pgid = pgid
        return self.received

    def close(self) -> None:
        os.close(self.wake_fd)
        os.close(self._wake)


@contextlib.contextmanager
def _relayed() -> Iterator[_Relay]:
    """While inside, SIGTERM, SIGINT and SIGHUP sent to this process go to a `_Relay`."""
    relay = _Relay()
    previous = {s: signal.signal(s, relay) for s in _SIGNALS}
    try:
        yield relay
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        relay.close()
