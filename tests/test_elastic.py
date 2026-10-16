"""Elastic leases' targets (`leasehold.elastic.targets`), a pure function: a lease grows one slot
at a time into real slack while nothing waits in its pool, headroom included, and is asked back
toward its least, largest extras first, when that lets a waiting item fit."""

from __future__ import annotations

import random

import pytest
from test_admission_ranges import _memory_case

from leasehold.admission import (
    AdmissionPolicy,
    Held,
    State,
    Waiting,
    _admit_least,
    _owed,
    _Tally,
    fits,
    grants,
    top,
)
from leasehold.elastic import Elastic, targets
from leasehold.model import MACHINE, Pool, PoolSet

G = 2**30


def pools(slots=6, headroom=0, **memory) -> AdmissionPolicy:
    """Pool `work` on the whole machine, and pool `ci` of up to 2 slots, no reservations."""
    shape = {
        "work": Pool(reserved=0, max=slots, headroom=headroom),
        "ci": Pool(reserved=0, max=2, priority=1),
    }
    return AdmissionPolicy.from_pools(PoolSet(slots=slots, pools=shape), **memory)


def lease(name, weight, least, most, *, user=None, pool="work", session=None, **kw) -> Elastic:
    return Elastic(
        key=("s", name),
        pool=pool,
        host=MACHINE,
        user=user if user is not None else name,
        weight=weight,
        min_weight=least,
        max_weight=most,
        session=session,
        **kw,
    )


def held(*leases: Elastic, fixed: tuple[Held, ...] = ()) -> tuple[Held, ...]:
    return fixed + tuple(
        Held(e.pool, e.host, e.user, e.weight, e.session, e.memory, e.memory_per_slot)
        for e in leases
    )


def waiting(name, weight, pool="work", **kw) -> Waiting:
    return Waiting(key=("s", name), pool=pool, user=name, weight=weight, order=(0.0, 0), **kw)


def aimed(policy, leases, *, wait=(), fixed=(), eligible=None) -> dict[str, int]:
    state = State(held=held(*leases, fixed=fixed), waiting=tuple(wait))
    every = {e.key for e in leases} if eligible is None else {("s", n) for n in eligible}
    return {k[1]: t for k, t in targets(state, policy, leases, every).items()}


# --- growing ------------------------------------------------------------------------------------


def test_an_eligible_lease_grows_one_slot_into_slack():
    assert aimed(pools(), [lease("a", 2, 2, 4)]) == {"a": 3}


def test_a_lease_inside_its_hold_down_does_not_grow():
    assert aimed(pools(), [lease("a", 2, 2, 4)], eligible=[]) == {"a": 2}


def test_nothing_grows_while_an_item_waits_in_its_pool():
    one = [lease("a", 2, 2, 6)]
    assert aimed(pools(), one, wait=[waiting("w", 4, pool="work")]) == {"a": 2}
    # An item waiting in another pool keeps only the room it needs.
    assert aimed(pools(), one, wait=[waiting("w", 2, pool="ci")]) == {"a": 3}
    assert aimed(
        pools(), one, fixed=(Held("work", MACHINE, "x", 2),), wait=[waiting("w", 2, pool="ci")]
    ) == {"a": 2}


def test_an_elastic_lease_grows_into_the_headroom_a_fixed_range_leaves():
    policy = pools(slots=8, headroom=2)
    assert grants(State(waiting=(waiting("r", 1, max_weight=8),)), policy)[0].weight == 6
    assert aimed(policy, [lease("a", 6, 1, 8)]) == {"a": 7}


def test_growth_stops_at_the_most_and_at_what_is_free():
    assert aimed(pools(), [lease("a", 4, 2, 4)]) == {"a": 4}
    full = (Held("work", MACHINE, "x", 3),)
    assert aimed(pools(), [lease("a", 3, 2, 6)], fixed=full) == {"a": 3}


def test_leases_take_turns_by_user_then_session_least_extra_first():
    policy = pools(slots=7)
    ann = lease("a", 3, 1, 6, user="ann")  # extra 2
    bob = lease("b", 2, 1, 6, user="bob")  # extra 1
    # One slot free: bob, who has fewer extra slots, takes it.
    assert aimed(policy, [ann, bob], fixed=(Held("work", MACHINE, "x", 1),)) == {"a": 3, "b": 3}
    s1 = lease("s1", 2, 1, 6, user="ann", session="one")
    s2 = lease("s2", 3, 1, 6, user="ann", session="two")
    assert aimed(policy, [s1, s2], fixed=(Held("work", MACHINE, "x", 1),)) == {"s1": 3, "s2": 3}


def test_growth_fits_the_memory_budget_and_one_leases_ceiling():
    budget = pools(memory=4 * G)
    assert aimed(budget, [lease("a", 4, 2, 6, memory_per_slot=G)]) == {"a": 4}
    assert aimed(budget, [lease("a", 3, 2, 6, memory_per_slot=G)]) == {"a": 4}
    ceiling = pools(lease_memory=3 * G)
    assert aimed(ceiling, [lease("a", 3, 2, 6, memory_per_slot=G)]) == {"a": 3}


def test_nothing_grows_while_memory_is_short_or_the_pool_is_capped_or_closed():
    one = [lease("a", 2, 1, 4)]
    assert aimed(pools(memory=8 * G, memory_free=4 * G), one) == {"a": 2}
    assert aimed(pools(capped=frozenset({"work"})), one) == {"a": 2}
    assert aimed(pools(closed=frozenset({"work"})), one) == {"a": 2}


# --- shrinking ----------------------------------------------------------------------------------


def test_a_lease_is_asked_back_until_a_waiting_item_fits():
    assert aimed(pools(), [lease("a", 6, 2, 6)], wait=[waiting("w", 3)]) == {"a": 3}


def test_the_largest_extras_are_lowered_first():
    big, small = lease("a", 4, 1, 4), lease("b", 2, 1, 4)
    assert aimed(pools(), [big, small], wait=[waiting("w", 2)]) == {"a": 2, "b": 2}
    assert aimed(pools(), [big, small], wait=[waiting("w", 4)]) == {"a": 1, "b": 1}


def test_nothing_is_lowered_when_lowering_everything_would_not_be_enough():
    one = [lease("a", 4, 2, 4)]
    blocked = (Held("work", MACHINE, "x", 2),)
    assert aimed(pools(), one, fixed=blocked, wait=[waiting("w", 5)]) == {"a": 4}


def test_a_lease_whose_slots_would_not_help_is_not_lowered():
    # The item waits for room in its own pool, ci, which holds 2 of its 2: work's lease
    # giving slots back would not let it in.
    full_ci = (Held("ci", MACHINE, "x", 2),)
    assert aimed(
        pools(), [lease("a", 4, 1, 4)], fixed=full_ci, wait=[waiting("w", 1, pool="ci")]
    ) == {"a": 4}
    # The item fits its pool but not the machine: the lease gives back what it needs.
    assert aimed(pools(), [lease("a", 6, 1, 6)], wait=[waiting("w", 1, pool="ci")]) == {"a": 5}


def test_a_lease_is_asked_back_for_the_memory_a_waiting_item_needs():
    budget = pools(memory=6 * G)
    one = [lease("a", 4, 2, 4, memory_per_slot=G)]
    assert aimed(budget, one, wait=[waiting("w", 1, memory=3 * G)]) == {"a": 3}


def test_under_memory_pressure_every_elastic_lease_is_asked_down_to_its_least():
    pressing = pools(memory=8 * G, memory_pressing=True)
    assert aimed(pressing, [lease("a", 4, 2, 6), lease("b", 2, 1, 3)]) == {"a": 2, "b": 1}


def test_a_lease_past_what_it_could_be_granted_now_is_asked_down_to_it():
    smaller = AdmissionPolicy.from_pools(
        PoolSet(
            slots=6,
            pools={
                "work": Pool(reserved=0, max=3, headroom=0),
                "ci": Pool(reserved=0, max=2, priority=1),
            },
        )
    )
    assert aimed(smaller, [lease("a", 5, 2, 6)]) == {"a": 3}


def test_with_no_elastic_lease_there_is_no_target():
    assert targets(State(held=(Held("work", MACHINE, "x", 2),)), pools(), [], set()) == {}


# --- properties ---------------------------------------------------------------------------------


def _elastic_case(rng: random.Random):
    """A random policy and state as the ledger has them after a decision (`_memory_case`, its
    admitted items moved from the line to the leases), with some leases elastic: (policy,
    state, elastic leases, eligible keys)."""
    policy, state = _memory_case(rng)
    if rng.random() < 0.5:  # often without memory, where more of the slot rules bind
        policy = AdmissionPolicy(
            slots=policy.slots, pools=policy.pools, closed=policy.closed, capped=policy.capped
        )
    got = {g.key: g.weight for g in grants(state, policy)}
    admitted = tuple(
        Held(w.pool, MACHINE, w.user, got[w.key], w.session, w.memory, w.memory_per_slot)
        for w in state.waiting
        if w.key in got
    )
    every = state.held + admitted
    leases = []
    for i, h in enumerate(every):
        if rng.random() < 0.6:
            least = rng.choice([1, 1, rng.randint(1, h.weight)])
            most = h.weight + rng.randint(0, 4)
            leases.append(
                Elastic(
                    ("e", str(i)),
                    h.pool,
                    h.host,
                    h.user,
                    h.weight,
                    least,
                    most,
                    h.session,
                    h.memory,
                    h.memory_per_slot,
                )
            )
    after = State(
        held=every,
        waiting=tuple(w for w in state.waiting if w.key not in got),
        served=state.served,
    )
    eligible = {e.key for e in leases if rng.random() < 0.7}
    return policy, after, leases, eligible


def _moved(state: State, leases, target) -> State:
    """`state` with each elastic lease (keyed by its place in `state.held`) at its target."""
    out = list(state.held)
    for e in leases:
        h = out[int(e.key[1])]
        out[int(e.key[1])] = Held(
            h.pool, h.host, h.user, target[e.key], h.session, h.memory, h.memory_per_slot
        )
    return State(held=tuple(out), waiting=state.waiting, served=state.served)


@pytest.mark.parametrize("block", range(4))
def test_targets_stay_in_range_grow_one_slot_and_never_while_an_item_waits(block):
    for seed in range(block * 1000, block * 1000 + 1000):
        policy, state, leases, eligible = _elastic_case(random.Random(seed))
        target = targets(state, policy, leases, eligible)
        waits = {w.pool for w in state.waiting}
        for e in leases:
            assert e.min_weight <= target[e.key] <= e.max_weight, seed
            if target[e.key] > e.weight:
                assert target[e.key] == e.weight + 1, seed
                assert e.key in eligible and e.pool not in waits, seed


@pytest.mark.parametrize("block", range(4))
def test_growth_never_overfills_slots_the_budget_or_one_leases_ceiling(block):
    for seed in range(block * 1000, block * 1000 + 1000):
        policy, state, leases, eligible = _elastic_case(random.Random(seed))
        target = targets(state, policy, leases, eligible)
        grown = [e for e in leases if target[e.key] > e.weight]
        if not grown:
            continue
        after = _Tally.of(_moved(state, leases, target), policy)
        for e in grown:
            assert after.used.get(e.pool, 0) <= top(policy, e.pool), seed
            free = policy.slots[MACHINE] - after.host_used.get(MACHINE, 0)
            assert free >= _owed(after, policy, e.pool), seed
            limit = policy.lease_memory.get(MACHINE)
            if limit is not None and e.memory_per_slot and MACHINE in policy.memory:
                assert target[e.key] * e.memory_per_slot <= limit, seed
        if MACHINE in policy.memory:
            assert after.memory.get(MACHINE, 0) <= policy.memory[MACHINE], seed


@pytest.mark.parametrize("block", range(4))
def test_the_first_blocked_item_fits_after_the_targets_when_shrinking_alone_could_do_it(block):
    checked = 0
    for seed in range(block * 1000, block * 1000 + 1000):
        policy, state, leases, eligible = _elastic_case(random.Random(seed))
        if MACHINE in policy.memory_pressing:
            continue
        _, stopped = _admit_least(state, policy, _Tally.of(state, policy))
        heads = [
            w
            for pool in sorted(policy.pools, key=lambda n: (policy.pools[n].priority, n))
            for w in stopped.get(pool, [])
        ]
        if not heads:
            continue
        head = heads[0]
        least = {e.key: e.min_weight for e in leases}
        floor = _Tally.of(_moved(state, leases, least), policy)
        if not fits(floor, policy, head.pool, head.weight, head.memory, head.memory_per_slot):
            continue
        target = targets(state, policy, leases, eligible)
        after = _Tally.of(_moved(state, leases, target), policy)
        assert fits(after, policy, head.pool, head.weight, head.memory, head.memory_per_slot), seed
        checked += 1
    assert checked > 10
