"""Elastic leases: the size each one should have now (`targets`), a pure function of what is
held, what waits and the policy. The ledger (`leasehold.ledger`) works the targets out inside
its transactions and stores them; a holder reads its target and moves to it
(`leasehold.ledger.Ledger.resize`).

An elastic lease asked for a range and promised two things: it takes more slots when they are
offered, and it gives slots back when asked. Nothing is ever taken from it: a holder that
ignores a lower target keeps its slots. Every lease, elastic or not, is admitted and first
sized exactly as `leasehold.admission.grants` says. After that, per host:

* while `memory_pressing` names the host, every elastic lease there is asked down to its least;
* a lease larger than the most it could be granted now (`AdmissionPolicy.most`, after a pool's
  `max` was lowered, say) is asked down to that;
* for each item whose turn came but which does not fit (`_admit_least`'s stopped items, in the
  order pools are served), when lowering the elastic leases on its host to their least would
  let it fit, the largest extras are lowered first, one slot at a time, until it does. Then
  each lowered lease, smallest extra first, gets back what the item can spare. The item is then
  counted as admitted, so the next item's room comes after it. An item short only of the
  memory allowance (`memory_free`) lowers nothing, since smaller leases do not make memory
  free any sooner;
* a lease grows only in an open pool that is not capped, with nothing waiting in it, on a host
  with memory neither short nor pressing and not draining for a whole item, and only when it is
  in `eligible` (the caller's hold-down: those whose size last changed long enough ago). It may
  grow one slot per decision, up to its most, into real slack: the slot rules hold with room
  kept for every stopped item, and the memory it counts fits the budget beside theirs, as for an
  extra slot of a range (`admission._extra_fits`). The `headroom` setting says how much of the
  pool's and the host's `headroom` it leaves free: all of it ("keep", the default, as for that
  slot), one slot of each ("leave-one"), or none ("use"), since it gives the slots back when an
  item arrives for them. Leases take turns by user, then session, then lease, the least extra
  first.

A lease never goes below its least or above its most. Growth never fills a pool past its top,
a host past its slots or into another pool's reservation, and never counts memory past the
budget.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from leasehold.admission import (
    AdmissionPolicy,
    ItemKey,
    State,
    Waiting,
    _admit_least,
    _extra_fits,
    _Kept,
    _pools_in_order,
    _Tally,
    draining,
    fits,
)

#: How much of the headroom growth leaves free (the module docstring).
Headroom = Literal["keep", "leave-one", "use"]

#: The choices of `Headroom`.
HEADROOM_MODES: tuple[str, ...] = get_args(Headroom)

#: What growth leaves of the headroom unless told otherwise.
DEFAULT_HEADROOM: Headroom = "keep"


@dataclass(frozen=True)
class Elastic:
    """An elastic lease. Its `weight` (the size it holds now) is also in `State.held`."""

    key: ItemKey
    pool: str
    host: str
    user: str | None
    #: The size it holds now.
    weight: int
    #: The least and the most it asked for.
    min_weight: int
    max_weight: int
    session: str | None = None
    #: The memory, in bytes, it declared in all; None: none.
    memory: int | None = None
    #: The memory, in bytes, it declared for each slot; None: none. A lease declaring this
    #: counts it, whatever `memory` says.
    memory_per_slot: int | None = None


def targets(
    state: State,
    policy: AdmissionPolicy,
    leases: Sequence[Elastic],
    eligible: Collection[ItemKey] = frozenset(),
    *,
    headroom: Headroom = DEFAULT_HEADROOM,
) -> dict[ItemKey, int]:
    """The size each of `leases` should have now (the module docstring). A lease in a pool the
    policy does not have keeps its size. `eligible` are the leases that may grow, leaving
    `headroom` of the headroom free."""
    tally = _Tally.of(state, policy)
    # What admission would still let in counts as held (after a decision, nothing is).
    _, stopped = _admit_least(state, policy, tally)
    target = {e.key: e.weight for e in leases}
    known = [e for e in leases if e.pool in policy.pools]

    def lower(e: Elastic, to: int) -> None:
        if to < target[e.key]:
            _move(tally, policy, e, to - target[e.key])
            target[e.key] = to

    for e in known:
        least = e.min_weight
        if e.host in policy.memory_pressing:
            lower(e, least)
        else:
            lower(e, max(least, _most(policy, e)))
    # An item counted as admitted (`_make_room`) is in the tally; only the others are kept
    # room for.
    kept: dict[str, list[Waiting]] = {}
    for pool in _pools_in_order(policy):
        for head in stopped.get(pool, []):
            if not _make_room(tally, policy, known, target, head):
                kept.setdefault(pool, []).append(head)
    _grow(state, policy, tally, known, target, eligible, _Kept.of(policy, kept), headroom)
    return target


def _most(policy: AdmissionPolicy, e: Elastic) -> int:
    """The most `e` could be granted now (`AdmissionPolicy.most`)."""
    return policy.most(e.pool, e.min_weight, e.max_weight, e.memory, e.memory_per_slot)


def _slot_memory(policy: AdmissionPolicy, e: Elastic) -> int:
    """The memory one slot of `e` counts on its host: its per-slot size, nothing beside a
    declared total, else the host's assumed size (0 without a budget)."""
    if e.host not in policy.memory:
        return 0
    if e.memory_per_slot is not None:
        return e.memory_per_slot
    if e.memory is not None:
        return 0
    return policy.assumed_memory.get(e.host, 0)


def _move(tally: _Tally, policy: AdmissionPolicy, e: Elastic, slots: int) -> None:
    """Count `slots` more (fewer, when negative) for `e` in `tally`."""
    tally.used[e.pool] = tally.used.get(e.pool, 0) + slots
    tally.host_used[e.host] = tally.host_used.get(e.host, 0) + slots
    memory = slots * _slot_memory(policy, e)
    if memory:
        tally.memory[e.host] = tally.memory.get(e.host, 0) + memory


def _copy(tally: _Tally) -> _Tally:
    return _Tally(
        used=dict(tally.used),
        host_used=dict(tally.host_used),
        running=dict(tally.running),
        session_running=dict(tally.session_running),
        served=dict(tally.served),
        session_served=dict(tally.session_served),
        last_turn=tally.last_turn,
        memory=dict(tally.memory),
        fresh_memory=dict(tally.fresh_memory),
    )


def _fits(tally: _Tally, policy: AdmissionPolicy, head: Waiting) -> bool:
    return fits(tally, policy, head.pool, head.weight, head.memory, head.memory_per_slot)


def _make_room(
    tally: _Tally,
    policy: AdmissionPolicy,
    leases: Sequence[Elastic],
    target: dict[ItemKey, int],
    head: Waiting,
) -> bool:
    """Lower the elastic leases on `head`'s host, largest extra first, until `head` fits, when
    lowering them all to their least would do it; then count `head` as admitted in `tally`.
    Nothing is lowered, and `head` is not counted, when that would not be enough. Whether it
    was counted."""
    host = policy.pools[head.pool].host
    if host in policy.memory_pressing:
        return False
    mine = [e for e in leases if e.host == host and target[e.key] > e.min_weight]
    if not _fits(tally, policy, head):
        trial = _copy(tally)
        for e in mine:
            _move(trial, policy, e, e.min_weight - target[e.key])
        if not _fits(trial, policy, head):
            return False
        lowered: dict[ItemKey, int] = {}
        while not _fits(tally, policy, head):
            e = max(
                (x for x in mine if target[x.key] > x.min_weight),
                key=lambda x: (target[x.key] - x.min_weight, x.key),
            )
            _move(tally, policy, e, -1)
            target[e.key] -= 1
            lowered[e.key] = lowered.get(e.key, 0) + 1
        # Give back what the item can spare: a slot lowered in a pool that does not limit it.
        for e in sorted(
            (x for x in mine if x.key in lowered),
            key=lambda x: (target[x.key] - x.min_weight, x.key),
        ):
            while lowered[e.key]:
                _move(tally, policy, e, 1)
                if not _fits(tally, policy, head):
                    _move(tally, policy, e, -1)
                    break
                target[e.key] += 1
                lowered[e.key] -= 1
    counted = policy.counted(host, head.weight, head.memory, head.memory_per_slot)
    tally.add(head.pool, host, head.user, head.session, head.weight, counted)
    if counted:
        tally.fresh_memory[host] = tally.fresh_memory.get(host, 0) + counted
    return True


@dataclass
class _Turns:
    """Each user's and each session's extra slots on a host, for taking turns to grow."""

    users: dict[tuple[str, str | None], int] = field(default_factory=dict)
    sessions: dict[tuple[str, str | None, str | None], int] = field(default_factory=dict)

    def add(self, e: Elastic, slots: int) -> None:
        user = (e.host, e.user)
        session = (e.host, e.user, e.session)
        self.users[user] = self.users.get(user, 0) + slots
        self.sessions[session] = self.sessions.get(session, 0) + slots

    def order(self, e: Elastic, extra: int) -> tuple:
        return (
            self.users.get((e.host, e.user), 0),
            self.sessions.get((e.host, e.user, e.session), 0),
            extra,
            e.key,
        )


def _grow(
    state: State,
    policy: AdmissionPolicy,
    tally: _Tally,
    leases: Sequence[Elastic],
    target: dict[ItemKey, int],
    eligible: Collection[ItemKey],
    kept: _Kept,
    headroom: Headroom,
) -> None:
    """Grow each eligible lease by at most one slot into real slack (the module docstring)."""
    waiting = {w.pool for w in state.waiting}
    drains = draining(state, policy)
    turns = _Turns()
    for e in leases:
        turns.add(e, target[e.key] - e.min_weight)
    growing = [
        e
        for e in leases
        if e.key in eligible
        and target[e.key] == e.weight
        and target[e.key] < _most(policy, e)
        and e.pool not in waiting
        and e.pool not in policy.closed
        and e.pool not in policy.capped
        and e.host not in policy.memory_pressing
        and e.host not in policy.memory_free
        and e.host not in drains
    ]
    while growing:
        e = min(growing, key=lambda x: turns.order(x, target[x.key] - x.min_weight))
        growing.remove(e)
        memory = _slot_memory(policy, e)
        if _extra_fits(tally, policy, e.pool, kept, memory, headroom=headroom):
            _move(tally, policy, e, 1)
            target[e.key] += 1
            turns.add(e, 1)
