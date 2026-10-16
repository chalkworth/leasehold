"""Memory in the pure admission decision: an item counts what it declares, or its weight at the
host's assumed memory per slot; a host's budget bounds what is counted there; `memory_free`
bounds what one decision admits while memory is short; `blocker` says which of slots, budget
or shortage holds an item back. Without a budget, admission is exactly as it was before memory
was counted."""

from __future__ import annotations

import random

import admission_before_memory as before
import pytest

from leasehold.admission import (
    AdmissionPolicy,
    Held,
    PoolPolicy,
    State,
    Waiting,
    _Tally,
    blocker,
    decide,
    fits,
    positions,
)
from leasehold.model import MACHINE, Pool, PoolSet

G = 2**30


def policy(slots=6, reserved=3, **memory) -> AdmissionPolicy:
    pools = PoolSet(
        slots=slots,
        pools={
            "work": Pool(reserved=reserved, max=slots, priority=0),
            "background": Pool(reserved=0, max=slots - reserved, priority=1),
        },
    )
    return AdmissionPolicy.from_pools(pools, **memory)


def item(name, weight=3, memory=None, pool="work", user="u", order=0, session=None) -> Waiting:
    return Waiting(
        key=("s", name),
        pool=pool,
        user=user,
        weight=weight,
        order=(float(order), order),
        session=session,
        memory=memory,
    )


def held(weight=3, memory=None, pool="work", user="u") -> Held:
    return Held(pool=pool, host=MACHINE, user=user, weight=weight, memory=memory)


def names(admits) -> list[str]:
    return [item_id for _scope, item_id in admits]


def test_no_budget_counts_no_memory():
    state = State(waiting=(item("a", memory=100 * G), item("b", memory=100 * G, order=1)))
    assert names(decide(state, policy())) == ["a", "b"]


def test_declared_memory_counts_against_the_budget():
    p = policy(memory=10 * G)
    state = State(waiting=(item("a", memory=6 * G), item("b", memory=6 * G, order=1)))
    assert names(decide(state, p)) == ["a"]
    after = State(held=(held(memory=6 * G),), waiting=(item("b", memory=6 * G, order=1),))
    assert blocker(after, p, ("s", "b")) == (
        "the memory budget is used: 6.0 GiB of 10.0 GiB counted; this counts its declared 6.0 GiB"
    )


def test_undeclared_memory_is_assumed_per_slot_of_weight():
    p = policy(memory=10 * G, assumed_memory=2 * G)
    state = State(waiting=(item("a", weight=3), item("b", weight=3, order=1)))
    assert names(decide(state, p)) == ["a"]  # 6 GiB + 6 GiB > 10 GiB, though 6 slots fit
    light = State(waiting=(item("a", weight=3), item("b", weight=2, order=1)))
    assert names(decide(light, p)) == ["a", "b"]  # 6 + 4 = 10, within
    reason = blocker(State(held=(held(),), waiting=(item("b", order=1),)), p, ("s", "b"))
    assert reason is not None and "(weight 3 at 2.0 GiB a slot, none declared)" in reason


def test_without_assumed_memory_undeclared_items_count_nothing():
    p = policy(memory=1 * G)
    state = State(waiting=(item("a"), item("b", order=1)))
    assert names(decide(state, p)) == ["a", "b"]


def test_a_declared_size_overrides_the_assumption_both_ways():
    p = policy(memory=10 * G, assumed_memory=2 * G)
    small = State(waiting=(item("a", weight=3, memory=1 * G), item("b", weight=3, order=1)))
    assert names(decide(small, p)) == ["a", "b"]  # 1 + 6 GiB


def test_slots_still_bind_when_memory_fits():
    p = policy(memory=100 * G)
    state = State(waiting=tuple(item(str(i), order=i, memory=G) for i in range(3)))
    assert names(decide(state, p)) == ["0", "1"]


def test_a_blocked_head_is_not_backfilled_around_for_memory():
    p = policy(memory=10 * G)
    state = State(
        held=(held(weight=1, memory=5 * G),),
        waiting=(item("big", weight=1, memory=6 * G), item("small", weight=1, order=1, memory=G)),
    )
    assert decide(state, p) == []


def test_standing_memory_is_counted_and_refused_past_it():
    p = policy(memory=10 * G, standing_memory={"work": 8 * G})
    assert names(decide(State(waiting=(item("a", memory=3 * G),)), p)) == []
    assert p.refusal("work", 3, 3 * G) == (
        "its declared 3.0 GiB cannot fit the 10.0 GiB memory budget even on an idle machine: "
        "8.0 GiB is held by standing work"
    )
    assert p.refusal("work", 3, 2 * G) is None


def test_more_than_the_budget_is_refused():
    p = policy(memory=4 * G, assumed_memory=G)
    assert (
        p.refusal("work", 1, 5 * G) == "its declared 5.0 GiB is more than the 4.0 GiB memory budget"
    )
    assert p.refusal("work", 5) == (
        "5.0 GiB (weight 5 at 1.0 GiB a slot, none declared) is more than the 4.0 GiB memory budget"
    )
    assert p.refusal("work", 4) is None


def test_memory_free_bounds_what_one_decision_admits():
    p = policy(
        memory=100 * G, assumed_memory=G, memory_free=4 * G, memory_short="memory pressure 30%"
    )
    state = State(waiting=(item("a", weight=3), item("b", weight=1, order=1)))
    assert names(decide(state, p)) == ["a", "b"]  # 3 + 1 = 4 GiB
    big = State(waiting=(item("a", weight=5),))
    assert decide(big, p) == []
    assert blocker(big, p, ("s", "a")) == (
        "memory is short (memory pressure 30%): 4.0 GiB may start now; this counts 5.0 GiB "
        "(weight 5 at 1.0 GiB a slot, none declared)"
    )


def test_memory_free_does_not_cap_slots_to_reservations():
    """A request heavier than its pool's reservation still starts while memory is short, when
    what it counts fits what is free (the guard no longer caps pools to `reserved`)."""
    p = policy(memory=100 * G, assumed_memory=G, memory_free=5 * G)
    assert names(decide(State(waiting=(item("a", weight=5),)), p)) == ["a"]


def test_zero_memory_free_admits_nothing_that_counts_memory():
    p = policy(memory=100 * G, assumed_memory=G, memory_free=0)
    assert decide(State(waiting=(item("a", weight=1),)), p) == []


def test_pressing_admits_nothing_whatever_it_counts():
    """Under real pressure nothing new starts: not an item declaring 0 bytes, not one counted
    as 0 because nothing is assumed per slot, not one on a budget of 0."""
    for p, declared in (
        (policy(memory=100 * G, assumed_memory=G, memory_free=0, memory_pressing=True), 0),
        (policy(memory=100 * G, memory_free=0, memory_pressing=True), None),
        (policy(memory=0, memory_free=0, memory_pressing=True), 0),
    ):
        state = State(waiting=(item("a", weight=4, memory=declared),))
        assert decide(state, p) == []
        assert blocker(state, p, ("s", "a")) == (
            "memory is short: nothing new starts until the pressure eases"
        )


def test_pressing_is_off_by_default_and_only_where_named():
    p = policy(memory=100 * G, assumed_memory=G)
    assert p.memory_pressing == frozenset()
    assert names(decide(State(waiting=(item("a", weight=4, memory=0),)), p)) == ["a"]


def test_an_item_short_only_of_memory_free_lets_what_fits_start_behind_it():
    """While the guard settles, the head that fits its slots and the budget but not what is
    free waits without blocking: a lighter item behind it, in another session, that fits what
    is free starts. Its own session's later items keep waiting behind it."""
    p = policy(memory=100 * G, assumed_memory=G, memory_free=2 * G)
    state = State(
        waiting=(
            item("heavy", weight=3, user="ann", session="s1"),
            item("after-heavy", weight=1, user="ann", session="s1", order=1),
            item("light", weight=1, user="bob", order=2),
        )
    )
    assert names(decide(state, p)) == ["light"]


def test_a_head_that_fails_slots_or_the_budget_still_blocks_its_pool():
    by_slots = policy(memory=100 * G, assumed_memory=G, memory_free=100 * G)
    state = State(
        held=(held(weight=4),),
        waiting=(item("heavy", weight=3, user="ann"), item("light", weight=1, user="bob", order=1)),
    )
    assert decide(state, by_slots) == []
    by_budget = policy(memory=4 * G, assumed_memory=G, memory_free=100 * G)
    state = State(
        held=(held(weight=1, memory=3 * G),),
        waiting=(
            item("big", weight=1, memory=2 * G, user="ann"),
            item("light", weight=1, memory=G // 2, user="bob", order=1),
        ),
    )
    assert decide(state, by_budget) == []


def test_heaviest_is_cut_to_what_fits_beside_standing_memory():
    """The weight the daemon serves a rule at is one that refusal accepts: undeclared, its
    assumed memory must fit the budget beside the standing memory."""
    p = policy(memory=6 * G, assumed_memory=G, standing_memory={"work": 4 * G})
    assert p.heaviest("work") == 2
    assert p.refusal("work", 2) is None
    assert p.refusal("work", 3) is not None
    assert policy(memory=6 * G, assumed_memory=G).heaviest("work") == 6
    assert policy(memory=6 * G).heaviest("work") == 6  # nothing assumed: slots alone


def test_fits_takes_memory_optionally():
    p = policy(memory=4 * G)
    tally = _Tally.of(State(), p)
    assert fits(tally, p, "work", 3)
    assert fits(tally, p, "work", 3, 4 * G)
    assert not fits(tally, p, "work", 3, 5 * G)


def test_hosts_count_memory_apart():
    p = AdmissionPolicy(
        slots={"a": 4, "b": 4},
        pools={
            "pa": PoolPolicy(host="a", reserved=0, max=4),
            "pb": PoolPolicy(host="b", reserved=0, max=4),
        },
        memory={"a": 4 * G},
    )
    state = State(
        waiting=(
            item("x", weight=1, memory=3 * G, pool="pa"),
            item("y", weight=1, memory=3 * G, pool="pa", order=1),
            item("z", weight=1, memory=30 * G, pool="pb", order=2),
        )
    )
    assert names(decide(state, p)) == ["x", "z"]  # host b has no budget


# --- parity without memory ---------------------------------------------------------------------


def _random_case(rng: random.Random):
    slots = rng.randint(1, 10)
    pools = {}
    left = slots
    for i, name in enumerate(rng.sample(["work", "external", "lend", "observed", "ci"], 3)):
        reserved = rng.randint(0, min(left, 3))
        left -= reserved
        pools[name] = Pool(
            reserved=reserved,
            max=rng.randint(max(1, reserved), slots) if slots else 1,
            priority=i,
            per_user_max_running=rng.choice([None, 1, 2]),
        )
    pool_set = PoolSet(slots=slots, pools=pools)
    names_ = list(pools)
    users = ["ann", "bob", None]
    sessions = ["s1", "s2", None]
    held = tuple(
        Held(
            pool=rng.choice(names_),
            host=MACHINE,
            user=rng.choice(users),
            weight=rng.randint(1, 3),
            session=rng.choice(sessions),
        )
        for _ in range(rng.randint(0, 4))
    )
    waiting = tuple(
        Waiting(
            key=("s", f"w{i}"),
            pool=rng.choice(names_),
            user=rng.choice(users),
            weight=rng.randint(1, 4),
            order=(float(rng.randint(0, 20)), i),
            session=rng.choice(sessions),
        )
        for i in range(rng.randint(0, 10))
    )
    served = {(rng.choice(names_), rng.choice(users)): rng.randint(1, 9) for _ in range(3)}
    closed = frozenset(rng.sample(names_, rng.randint(0, 1)))
    capped = frozenset(rng.sample(names_, rng.randint(0, 2)))
    standing = {rng.choice(names_): rng.randint(0, 2)} if rng.random() < 0.3 else {}
    return pool_set, held, waiting, served, closed, capped, standing


#: The fields added since `admission_before_memory`: memory, and ranges of weight.
_NEW_FIELDS = frozenset({"memory", "memory_per_slot", "max_weight"})


def _old(cls, obj):
    return cls(**{k: v for k, v in vars(obj).items() if k not in _NEW_FIELDS})


@pytest.mark.parametrize("block", range(4))
def test_without_a_budget_slot_admission_is_as_before_memory(block):
    """Every decision, line, blocker and refusal is the one made before memory was counted, both
    with no memory fields at all and with a budget that counts nothing (no item declares memory
    and none is assumed)."""
    for seed in range(block * 200, block * 200 + 200):
        _compare(random.Random(seed))


def _compare(rng: random.Random) -> None:
    pool_set, held_, waiting, served, closed, capped, standing = _random_case(rng)
    old_policy = before.AdmissionPolicy.from_pools(
        pool_set, closed=closed, standing=standing, capped=capped
    )
    old_state = before.State(
        held=tuple(_old(before.Held, h) for h in held_),
        waiting=tuple(_old(before.Waiting, w) for w in waiting),
        served=served,
    )
    state = State(held=held_, waiting=waiting, served=served)
    for new_policy in (
        AdmissionPolicy.from_pools(pool_set, closed=closed, standing=standing, capped=capped),
        AdmissionPolicy.from_pools(
            pool_set, closed=closed, standing=standing, capped=capped, memory=G
        ),
    ):
        assert decide(state, new_policy) == before.decide(old_state, old_policy)
        assert positions(state, new_policy) == before.positions(old_state, old_policy)
        for w in waiting:
            assert blocker(state, new_policy, w.key) == before.blocker(old_state, old_policy, w.key)
        for name in pool_set.pools:
            for weight in range(1, 12):
                assert new_policy.refusal(name, weight) == old_policy.refusal(name, weight)
