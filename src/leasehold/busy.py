"""What an agent, or a person at a shim, is told when the machine stayed busy: who holds it, why
the command waits and its place in line. Built from what the daemon already answers: the last
`queued` answer to an acquire (`position`, `blocked`) and the lease listing (`GET /v1/leases`).
`reason` is pure; `told` asks the daemon for the listing first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from leasehold.client import ApiError, Client, Unavailable

#: What a refusal adds to the reason: what to do about it.
ADVICE = "The command did not run. Do lighter work first, then run it again."


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


def reason(
    answer: Mapping,
    leases: Sequence[Mapping],
    *,
    pool: str | None,
    waited_s: float | None = None,
    gave_up: bool = False,
) -> str:
    """One line: the heaviest lease in `pool` (in any pool when `pool` holds none) and how much
    the others hold, why the request waits (`blocked`) and its place in line (`position`), or,
    with `gave_up`, the place it gave up. A refusal adds `ADVICE`."""
    mine = [lease for lease in leases if lease.get("pool") == pool]
    held = sorted(mine or leases, key=lambda lease: -int(lease.get("weight") or 0))
    parts = []
    if held:
        top, rest = held[0], held[1:]
        part = f"{holder(top)} holds {_slots(int(top.get('weight') or 0))}"
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


def told(client: Client, answer: Mapping, *, pool: str | None, waited_s: float) -> str:
    """`reason` for a request that waited `waited_s` and then gave up its place in line, with
    the leases the daemon lists now (none when it does not answer)."""
    try:
        leases = client.leases()["leases"]
    except (Unavailable, ApiError):
        leases = []
    return reason(answer, leases, pool=pool, waited_s=waited_s, gave_up=True)
