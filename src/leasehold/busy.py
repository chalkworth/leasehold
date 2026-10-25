"""What an agent, or a person at a shim, is told when it waits for a lease or the machine stayed
busy: who holds it, how long that has run and usually runs, why the command waits, its place in
line, and when to try again. Built from what the daemon already answers: the last `queued`
answer to an acquire (`position`, `blocked`), the lease listing (`GET /v1/leases`) and the
outcomes of past runs (`GET /v1/outcomes`). `reason`, `retry_in`, `advice` and `waited` are
pure; `told` and `ahead` ask the daemon first.

How long a holder usually runs is the median run time of the clean runs (exit 0, no OOM kill)
of its `[commands]` rule in its repository, which a lease carries as its `rule` and `repo`
labels (`leasing.lease_request`). There is no figure until `USUAL_RUNS` such runs are known.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

from leasehold.client import ApiError, Client, Unavailable

#: What a refusal adds to the reason when it cannot say when to retry.
ADVICE = "The command did not run. Do lighter work first, then run it again."
#: How many clean past runs give a usual run time.
USUAL_RUNS = 3
#: A command that waited at least this long for its lease says so when it starts (`waited`).
WAIT_NOTED_S = 5.0


def holder(lease: Mapping) -> str:
    """A lease in a few words: its command (`cmd` label), else its shim, else its kind and id;
    and its session."""
    labels = lease.get("labels") or {}
    what = labels.get("cmd") or labels.get("shim")
    what = what or f"{lease.get('kind') or 'a'} lease {lease.get('lease_id') or ''}".rstrip()
    session = lease.get("session")
    return f"{what} (session {session})" if session else str(what)


def _slots(n: int) -> str:
    return "1 slot" if n == 1 else f"{n} slots"


def span(seconds: float, *, rough: bool = False) -> str:
    """A duration in a few characters: `45s`, `2m10s`, `1h05m`; with `rough`, a duration of a
    minute or more in whole minutes (`3m`)."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    if rough and s < 3600:
        return f"{round(s / 60)}m"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def usual(lease: Mapping, runs: Sequence[Mapping]) -> float | None:
    """How long `lease`'s command usually runs, in seconds: the median of the clean `runs`
    (outcomes) of its rule in its repository. None when the lease names no rule, or fewer than
    `USUAL_RUNS` clean runs are known."""
    labels = lease.get("labels") or {}
    rule = labels.get("rule")
    if not rule or lease.get("kind") == "observed":
        return None
    repo = labels.get("repo") or None
    times = sorted(
        float(run["wall_s"])
        for run in runs
        if run.get("rule") == rule
        and run.get("repo") == repo
        and run.get("exit_status") == 0
        and not run.get("oom_kills")
    )
    if len(times) < USUAL_RUNS:
        return None
    middle = len(times) // 2
    return times[middle] if len(times) % 2 else (times[middle - 1] + times[middle]) / 2


def _running(lease: Mapping, now: float) -> float | None:
    acquired = lease.get("acquired_at")
    return None if acquired is None else max(0.0, now - float(acquired))


def _heaviest(leases: Sequence[Mapping], pool: str | None) -> list[Mapping]:
    """The leases in `pool` (all of them when `pool` holds none), heaviest first."""
    mine = [lease for lease in leases if lease.get("pool") == pool]
    return sorted(mine or leases, key=lambda lease: -int(lease.get("weight") or 0))


def _holds(lease: Mapping, runs: Sequence[Mapping], now: float) -> str:
    """`lease` in a few words: what it holds, how long it has run, and how long it usually
    runs when that is known."""
    part = f"{holder(lease)} holds {_slots(int(lease.get('weight') or 0))}"
    running = _running(lease, now)
    if running is not None:
        part += f", running {span(running)}"
        took = usual(lease, runs)
        if took is not None:
            part += f" (it usually takes ~{span(took, rough=True)})"
    return part


def reason(
    answer: Mapping,
    leases: Sequence[Mapping],
    *,
    pool: str | None,
    waited_s: float | None = None,
    gave_up: bool = False,
    runs: Sequence[Mapping] = (),
    now: float | None = None,
) -> str:
    """One line: the heaviest lease in `pool` (in any pool when `pool` holds none), how long it
    has run by `now` (default the time now) and usually runs (from `runs`, past outcomes), and
    how much the others hold; why the request waits (`blocked`) and its place in line
    (`position`), or, with `gave_up`, the place it gave up. A refusal adds `advice`."""
    held = _heaviest(leases, pool)
    parts = []
    if held:
        top, rest = held[0], held[1:]
        part = _holds(top, runs, time.time() if now is None else now)
        if rest:
            others = sum(int(lease.get("weight") or 0) for lease in rest)
            leases_word = "lease holds" if len(rest) == 1 else "leases hold"
            part += f", {len(rest)} other {leases_word} {others}"
        parts.append(part)
    if answer.get("blocked"):
        parts.append(str(answer["blocked"]))
    if answer.get("position") is not None:
        place = answer["position"]
        parts.append(f"gave up its place in line (#{place})" if gave_up else f"queued #{place}")
    head = "machine busy" if waited_s is None else f"machine busy for {waited_s:g}s"
    return f"{head}: {'; '.join(parts) or 'no room'}"


def retry_in(
    leases: Sequence[Mapping], runs: Sequence[Mapping], *, pool: str | None, now: float
) -> float | None:
    """Seconds until the heaviest lease in `pool` usually ends (`usual`); None when that is not
    known, or it has already run longer than usual."""
    held = _heaviest(leases, pool)
    if not held:
        return None
    took, running = usual(held[0], runs), _running(held[0], now)
    if took is None or running is None or running >= took:
        return None
    return took - running


def advice(left_s: float | None) -> str:
    """What a refusal says to do: retry in about `left_s` seconds when that is known."""
    if left_s is None:
        return ADVICE
    minutes = max(1, round(left_s / 60))
    when = "1 minute" if minutes == 1 else f"{minutes} minutes"
    return f"The command did not run. Retry in about {when}, or do lighter work first."


def _outcomes(client: Client) -> list[Mapping]:
    try:
        return list(client.outcomes()["outcomes"])
    except (Unavailable, ApiError, KeyError, TypeError):
        return []


def told(client: Client, answer: Mapping, *, pool: str | None, waited_s: float) -> tuple[str, str]:
    """`reason` for a request that waited `waited_s` and then gave up its place in line, and the
    `advice` for it, from the leases the daemon lists now and the outcomes it keeps (none when
    it does not answer)."""
    try:
        leases = client.leases()["leases"]
    except (Unavailable, ApiError):
        leases = []
    runs = _outcomes(client) if leases else []
    now = time.time()
    why = reason(answer, leases, pool=pool, waited_s=waited_s, gave_up=True, runs=runs, now=now)
    return why, advice(retry_in(leases, runs, pool=pool, now=now))


def ahead(client: Client, pool: str | None) -> str | None:
    """The heaviest lease in `pool` now, in a few words (`holder`), for a request that has to
    wait; None when there is none, or the daemon does not answer."""
    try:
        held = _heaviest(client.leases()["leases"], pool)
    except (Unavailable, ApiError, KeyError, TypeError):
        return None
    return holder(held[0]) if held else None


def waited(program: str, seconds: float, behind: str | None) -> str:
    """What a command that waited `seconds` in line, `behind` a lease (`ahead`), says once it
    is granted, so that a slow start is not taken for a slow command."""
    after = f", behind {behind}" if behind else ""
    return (
        f"leasehold: {program} waited {seconds:.0f}s in line for its lease{after}; "
        "the command itself has not been slow."
    )
