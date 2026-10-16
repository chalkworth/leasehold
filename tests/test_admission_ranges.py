"""Ranges of weight in the pure admission decision: an item that asks for MIN-MAX is admitted
exactly as an item of MIN would be, then granted a size from MIN to MAX that shares what was
free among the pool's items, never takes slots a waiting head needs, another pool's
reservation, or memory the budget or the allowance does not have."""

from __future__ import annotations

import random
from dataclasses import replace

import admission_before_memory as before
import pytest
from test_admission_memory import _old, _random_case

from leasehold.admission import (
    AdmissionPolicy,
    Grant,
    Held,
    PoolPolicy,
    State,
    Waiting,
    _admit_least,
    _Tally,
    alone,
    blocker,
    decide,
    fits,
    grants,
    positions,
    top,
)
from leasehold.model import MACHINE, Pool, PoolSet

G = 2**30


def one_pool(slots=6, headroom=0, **memory) -> AdmissionPolicy:
    """One pool on the whole machine; no headroom unless asked, so the sizes are the share's."""
    pool = Pool(reserved=0, max=slots, headroom=headroom)
    return AdmissionPolicy.from_pools(PoolSet(slots=slots, pools={"work": pool}), **memory)


def item(name, least, most=None, *, user=None, order=0, pool="work", **kw) -> Waiting:
    return Waiting(
        key=("s", name),
        pool=pool,
        user=user if user is not None else name,
        weight=least,
        order=(float(order), order),
        max_weight=most,
        **kw,
    )


def sized(state, policy) -> dict[str, int]:
    return {g.key[1]: g.weight for g in grants(state, policy)}


# --- the sizes ----------------------------------------------------------------------------------


def test_one_range_alone_on_an_idle_pool_gets_its_most():
    assert sized(State(waiting=(item("a", 2, 4),)), one_pool(6)) == {"a": 4}


def test_two_ranges_arriving_together_share_what_is_free():
    state = State(waiting=(item("a", 2, 4, order=0), item("b", 2, 4, order=1)))
    assert sized(state, one_pool(6)) == {"a": 3, "b": 3}


def test_a_range_with_only_its_least_free_gets_its_least():
    state = State(
        held=(Held("work", MACHINE, "x", 4),),
        waiting=(item("a", 2, 4),),
    )
    assert sized(state, one_pool(6)) == {"a": 2}


def test_items_still_waiting_count_in_the_share():
    # 8 free; one admitted (2-8) and one waiting behind a per-user limit, which needs 1.
    policy = AdmissionPolicy(
        slots={MACHINE: 8},
        pools={"work": PoolPolicy(MACHINE, 0, 8, per_user_max_running=1)},
    )
    state = State(waiting=(item("a", 2, 8, user="u", order=0), item("b", 1, user="u", order=1)))
    assert sized(state, policy) == {"a": 7}
    # A waiting item that could use more takes its share: 8 // 2 = 4 each.
    state = State(waiting=(item("a", 2, 8, user="u", order=0), item("b", 1, 8, user="u", order=1)))
    assert sized(state, policy) == {"a": 4}


def test_a_blocked_heads_least_is_never_taken_by_extra_slots():
    # b (9 slots) is blocked behind a; the 8 free slots are b's to wait for, so a gets none.
    state = State(waiting=(item("a", 2, 6, order=0), item("b", 9, order=1)))
    assert sized(state, one_pool(10)) == {"a": 2}  # the share alone would give it 5


def test_while_memory_is_short_every_range_gets_its_least():
    # b fits slots and budget but not the allowance: it lets c by, which starts at its least.
    policy = one_pool(8, memory=100 * G, assumed_memory=G, memory_free=4 * G)
    state = State(waiting=(item("b", 5, order=0), item("c", 1, 8, order=1)))
    assert sized(state, policy) == {"c": 1}


def test_the_allowance_a_range_leaves_unused_is_there_for_the_next_least():
    # 4 GiB may start: a takes its least (1 GiB), and the 3 GiB left start d's least next time.
    policy = one_pool(8, memory=100 * G, assumed_memory=G, memory_free=4 * G)
    assert sized(State(waiting=(item("a", 1, 4),)), policy) == {"a": 1}
    after = one_pool(8, memory=100 * G, assumed_memory=G, memory_free=3 * G)
    state = State(held=(Held("work", MACHINE, "a", 1),), waiting=(item("d", 3),))
    assert sized(state, after) == {"d": 3}


def test_other_pools_reservations_are_never_taken():
    policy = AdmissionPolicy.from_pools(
        PoolSet(
            slots=6,
            pools={
                "work": Pool(reserved=0, max=6, headroom=0),
                "ci": Pool(reserved=3, max=3, priority=1),
            },
        )
    )
    assert sized(State(waiting=(item("a", 2, 6),)), policy) == {"a": 3}


def test_a_capped_pool_grants_the_least_alone():
    # Capped while the guard is on without a memory budget: as under memory pressure.
    policy = AdmissionPolicy.from_pools(
        PoolSet(slots=6, pools={"work": Pool(reserved=3, max=6, headroom=0)}),
        capped=frozenset({"work"}),
    )
    assert top(policy, "work") == 3
    assert sized(State(waiting=(item("a", 1, 6),)), policy) == {"a": 1}


def test_extra_slots_go_in_admission_order():
    # 5 free, two ranges of 1-5: the share is 2 each, and the slot left over stays free.
    state = State(waiting=(item("a", 1, 5, order=0), item("b", 1, 5, order=1)))
    assert grants(state, one_pool(5)) == [Grant(("s", "a"), 2), Grant(("s", "b"), 2)]


def two_pools(slots=8, **memory) -> AdmissionPolicy:
    pools = {
        "a": Pool(reserved=0, max=slots, headroom=0),
        "b": Pool(reserved=0, max=slots, priority=1, headroom=0),
    }
    return AdmissionPolicy.from_pools(PoolSet(slots=slots, pools=pools), **memory)


def test_extra_slots_are_dealt_round_the_items_one_at_a_time():
    # 8 slots: r1 and r2 (1-8) in a, f (4) in b; 2 slots are left to deal, one to each.
    state = State(
        waiting=(
            item("r1", 1, 8, order=0, pool="a"),
            item("r2", 1, 8, order=1, pool="a"),
            item("f", 4, order=2, pool="b"),
        )
    )
    assert sized(state, two_pools(8)) == {"r1": 2, "r2": 2, "f": 4}


def test_memory_for_extra_slots_is_dealt_round_the_items_too():
    # Room for 4 slots each, memory for 2 extra slots at 1 GiB a slot: one each, not two to r1.
    policy = one_pool(8, memory=4 * G)
    state = State(
        waiting=(
            item("r1", 1, 4, order=0, memory_per_slot=G),
            item("r2", 1, 4, order=1, memory_per_slot=G),
        )
    )
    assert sized(state, policy) == {"r1": 2, "r2": 2}


def test_extra_slots_keep_room_for_a_head_blocked_in_another_pool():
    # 3 held in b; r (1-5) in a takes its least, then h (5) in b does not fit the 4 left. Extra
    # slots for r would keep h waiting for r's whole life, so r gets none.
    state = State(
        held=(Held("b", MACHINE, "x", 3),),
        waiting=(item("r", 1, 5, pool="a", order=0), item("h", 5, pool="b", order=1)),
    )
    assert sized(state, two_pools(8)) == {"r": 1}
    # Once h is admitted, nothing is kept for it: r grows into what is left.
    state = State(
        held=(Held("b", MACHINE, "x", 1),),
        waiting=(item("r", 1, 5, pool="a", order=0), item("h", 5, pool="b", order=1)),
    )
    assert sized(state, two_pools(8)) == {"r": 2, "h": 5}


def test_a_head_whose_own_pool_is_full_keeps_no_room_on_the_host():
    # b holds its 8 of 8, and an 8 waits there: it needs b's own slots back, not the host's.
    pools = {
        "a": Pool(reserved=0, max=16, headroom=0),
        "b": Pool(reserved=0, max=8, priority=1, headroom=0),
    }
    policy = AdmissionPolicy.from_pools(PoolSet(slots=16, pools=pools))
    state = State(
        held=(Held("b", MACHINE, "x", 8),),
        waiting=(item("r", 1, 8, pool="a", order=0), item("h", 8, pool="b", order=1)),
    )
    assert sized(state, policy) == {"r": 8}


def test_the_host_keeps_the_largest_headroom_of_its_pools():
    # Three pools on 8 slots with headroom 2, 1 and 1: whichever pool grows, 2 stay free.
    pools = {
        "work": Pool(reserved=0, max=8, headroom=2),
        "ci": Pool(reserved=0, max=8, priority=1, headroom=1),
        "lend": Pool(reserved=0, max=8, priority=2, headroom=1),
    }
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools=pools))
    state = State(
        waiting=(
            item("w", 1, 8, pool="work", order=0),
            item("c", 1, 8, pool="ci", order=1),
            item("l", 1, 8, pool="lend", order=2),
        )
    )
    got = sized(state, policy)
    assert sum(got.values()) == 6
    pool_of = {"w": "work", "c": "ci", "l": "lend"}
    held = tuple(Held(pool_of[k], MACHINE, k, w) for k, w in got.items())
    assert sized(State(held=held, waiting=(item("n", 2, pool="work"),)), policy) == {"n": 2}


def test_extra_slots_leave_the_memory_the_hosts_headroom_would_count():
    # 16 slots (headroom 4), 48 GiB, 4 GiB a slot assumed: a 2-12 at 4 GiB a slot stops at 8
    # (32 GiB), leaving the 16 GiB four more slots would count, so a 1 then starts.
    policy = AdmissionPolicy.from_pools(
        PoolSet(slots=16, pools={"work": Pool(reserved=0, max=16)}),
        memory=48 * G,
        assumed_memory=4 * G,
    )
    big = item("r", 2, 12, memory_per_slot=4 * G)
    assert sized(State(waiting=(big,)), policy) == {"r": 8}
    held = (Held("work", MACHINE, "r", 8, memory_per_slot=4 * G),)
    assert sized(State(held=held, waiting=(item("n", 1),)), policy) == {"n": 1}


def test_extra_slots_leave_the_assumed_memory_of_the_headroom():
    # 8 slots (headroom 2), 10 GiB, 2 GiB a slot assumed: a 1-8 stops at 3 (6 GiB), leaving
    # the 4 GiB two slots count, so a 2 then starts.
    policy = AdmissionPolicy.from_pools(
        PoolSet(slots=8, pools={"work": Pool(reserved=0, max=8)}),
        memory=10 * G,
        assumed_memory=2 * G,
    )
    assert sized(State(waiting=(item("r", 1, 8),)), policy) == {"r": 3}
    held = (Held("work", MACHINE, "r", 3),)
    assert sized(State(held=held, waiting=(item("n", 2),)), policy) == {"n": 2}


def test_extra_slots_leave_the_headroom_at_the_ranges_own_per_slot_size():
    # 8 slots (headroom 2), a budget of 100, nothing assumed, 20 a slot: a 1-8 stops at 3 (60),
    # leaving the 40 two slots of its size count, so a 1 at 20 a slot then starts.
    policy = AdmissionPolicy.from_pools(
        PoolSet(slots=8, pools={"work": Pool(reserved=0, max=8)}), memory=100, assumed_memory=0
    )
    assert sized(State(waiting=(item("r", 1, 8, memory_per_slot=20),)), policy) == {"r": 3}
    held = (Held("work", MACHINE, "r", 3, memory_per_slot=20),)
    nxt = item("n", 1, memory_per_slot=20)
    assert sized(State(held=held, waiting=(nxt,)), policy) == {"n": 1}


def test_a_pool_whose_headroom_fills_it_keeps_nothing_on_the_host():
    pools = {
        "work": Pool(reserved=0, max=8, headroom=0),
        "zombie": Pool(reserved=0, max=8, priority=1, headroom=8),
    }
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools=pools))
    assert sized(State(waiting=(item("r", 1, 8),)), policy) == {"r": 8}


def test_an_observed_pools_headroom_keeps_nothing_on_the_host():
    pools = {
        "work": Pool(reserved=0, max=16, headroom=0),
        "observed": Pool(reserved=0, max=16, priority=1, headroom=4),
    }
    pool_set = PoolSet(slots=16, pools=pools)
    seen = AdmissionPolicy.from_pools(pool_set, observed=frozenset({"observed"}))
    assert sized(State(waiting=(item("r", 1, 16),)), seen) == {"r": 16}
    assert sized(State(waiting=(item("r", 1, 16),)), AdmissionPolicy.from_pools(pool_set)) == {
        "r": 12
    }


def test_alone_is_what_a_range_alone_is_granted_on_an_idle_machine():
    # [ceiling] memory 4 GiB at 1 GiB a slot: 4, though the pool's 8 less headroom 2 is 6.
    pool_set = PoolSet(slots=8, pools={"work": Pool(reserved=0, max=8, headroom=2)})
    policy = AdmissionPolicy.from_pools(pool_set, lease_memory=4 * G)
    assert alone(policy, "work", 1, 8, memory_per_slot=G) == 4
    assert alone(policy, "work", 1, 8) == 6
    # The guard's state is not an idle machine's: a capped pool still answers 6.
    capped = AdmissionPolicy.from_pools(pool_set, capped=frozenset({"work"}))
    assert alone(capped, "work", 1, 8) == 6
    # Beside a pool of headroom 2, a pool of headroom 0 still keeps the host's 2.
    pools = {
        "work": Pool(reserved=0, max=8, headroom=2),
        "ci": Pool(reserved=0, max=8, priority=1, headroom=0),
    }
    two = AdmissionPolicy.from_pools(PoolSet(slots=8, pools=pools))
    assert alone(two, "ci", 1, 8) == 6
    assert alone(two, "ci", 9) == 9  # never admitted: its least


@pytest.mark.parametrize("block", range(2))
def test_a_fixed_weight_of_alone_is_granted_and_no_more_than_the_range(block):
    """`alone` is what a range alone is granted on an idle machine, so a fixed request of that
    weight is never refused, is admitted alone, and asks no more than the range would get."""
    for seed in range(block * 1500, block * 1500 + 1500):
        rng = random.Random(seed)
        policy, state = _memory_case(rng)
        for w in state.waiting:
            fixed = alone(policy, w.pool, w.weight, w.max_weight, w.memory, w.memory_per_slot)
            idle = replace(
                policy,
                closed=frozenset(),
                capped=frozenset(),
                memory_free={},
                memory_pressing=frozenset(),
            )
            lone = grants(State(waiting=(w,)), idle)
            if not lone:
                assert fixed == w.weight
                continue
            assert fixed <= lone[0].weight, seed
            assert policy.refusal(w.pool, fixed, w.memory, w.memory_per_slot) is None, seed
            one = Waiting(**(vars(w) | {"weight": fixed, "max_weight": None}))
            assert [g.weight for g in grants(State(waiting=(one,)), idle)] == [fixed], seed


def test_extra_slots_keep_the_memory_a_blocked_head_needs():
    # 16 slots, 16 GiB, 2 GiB a slot assumed, 3 slots held (6 GiB). r (1-8) takes its least
    # (8 GiB counted); h (1, declaring 10 GiB) does not fit. Every extra slot would keep h out.
    policy = one_pool(16, memory=16 * G, assumed_memory=2 * G)
    state = State(
        held=(Held("work", MACHINE, "x", 3),),
        waiting=(item("r", 1, 8, order=0), item("h", 1, order=1, memory=10 * G)),
    )
    assert sized(state, policy) == {"r": 1}


# --- headroom -----------------------------------------------------------------------------------


def test_auto_headroom_leaves_a_quarter_of_the_pool_for_the_next_request():
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools={"work": Pool(max=8)}))
    assert policy.pools["work"].headroom == 2
    assert sized(State(waiting=(item("a", 1, 8),)), policy) == {"a": 6}
    # The next request of 2 starts at once, beside the 6.
    state = State(held=(Held("work", MACHINE, "a", 6),), waiting=(item("b", 2),))
    assert sized(state, policy) == {"b": 2}


def test_headroom_never_holds_back_a_least_or_a_fixed_size():
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools={"work": Pool(max=8)}))
    assert sized(State(waiting=(item("a", 8),)), policy) == {"a": 8}
    assert sized(State(waiting=(item("a", 7, 8),)), policy) == {"a": 7}


def test_headroom_is_a_count_or_auto():
    assert Pool(max=8, headroom=0).headroom_slots() == 0
    assert Pool(max=8, headroom=3).headroom_slots() == 3
    assert [Pool(max=n).headroom_slots() for n in (2, 3, 4, 6, 8, 9)] == [0, 0, 1, 1, 2, 2]
    with pytest.raises(ValueError):
        Pool(max=8, headroom=-1)
    with pytest.raises(ValueError):
        Pool(max=8, headroom="some")
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools={"work": Pool(max=8, headroom=0)}))
    assert sized(State(waiting=(item("a", 1, 8),)), policy) == {"a": 8}


def test_headroom_is_kept_on_the_host_as_well_as_in_the_pool():
    # Pool a may hold all 8 with headroom 2, but b already holds 4: a's range stops at 2.
    pools = {
        "a": Pool(reserved=0, max=8, headroom=2),
        "b": Pool(reserved=0, max=8, priority=1, headroom=0),
    }
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools=pools))
    state = State(held=(Held("b", MACHINE, "x", 4),), waiting=(item("r", 1, 8, pool="a"),))
    assert sized(state, policy) == {"r": 2}


def test_a_fixed_item_beside_a_range_keeps_its_weight():
    state = State(waiting=(item("a", 2, order=0), item("b", 1, 6, order=1)))
    assert sized(state, one_pool(6)) == {"a": 2, "b": 4}  # a needs only 2 of its share


def test_a_range_that_reaches_its_most_leaves_its_share_to_the_others():
    state = State(waiting=(item("a", 1, 2, order=0), item("b", 1, 8, order=1)))
    assert sized(state, one_pool(8)) == {"a": 2, "b": 6}


# --- memory -------------------------------------------------------------------------------------


def test_memory_per_slot_is_counted_at_the_granted_size():
    budget = one_pool(6, memory=10 * G)
    assert sized(State(waiting=(item("a", 2, 4, memory_per_slot=2 * G),)), budget) == {"a": 4}
    assert sized(State(waiting=(item("a", 2, 4, memory_per_slot=3 * G),)), budget) == {"a": 3}
    held = Held("work", MACHINE, "a", 3, memory_per_slot=3 * G)
    assert _Tally.of(State(held=(held,)), budget).memory[MACHINE] == 9 * G


def test_assumed_memory_is_counted_at_the_granted_size():
    policy = one_pool(6, memory=7 * G, assumed_memory=2 * G)
    assert sized(State(waiting=(item("a", 2, 4),)), policy) == {"a": 3}
    held = Held("work", MACHINE, "a", 3)
    assert _Tally.of(State(held=(held,)), policy).memory[MACHINE] == 6 * G


def test_a_declared_total_does_not_grow_with_extra_slots():
    policy = one_pool(6, memory=5 * G, assumed_memory=4 * G)
    assert sized(State(waiting=(item("a", 2, 4, memory=4 * G),)), policy) == {"a": 4}
    held = Held("work", MACHINE, "a", 4, memory=4 * G)
    assert _Tally.of(State(held=(held,)), policy).memory[MACHINE] == 4 * G


def test_no_extra_slot_starts_while_memory_is_short():
    # The allowance has room for the extra slots too, but it is kept for new work's least.
    policy = one_pool(6, memory=100 * G, assumed_memory=G, memory_free=6 * G)
    assert sized(State(waiting=(item("a", 2, 4),)), policy) == {"a": 2}


def test_nothing_starts_or_grows_while_memory_is_pressing():
    policy = one_pool(6, memory=100 * G, memory_pressing=True)
    assert grants(State(waiting=(item("a", 2, 4),)), policy) == []


def test_refusal_and_heaviest_judge_the_least_and_a_per_slot_size():
    policy = one_pool(6, memory=8 * G)
    assert policy.refusal("work", 2, memory_per_slot=4 * G) is None
    assert "budget" in (policy.refusal("work", 3, memory_per_slot=4 * G) or "")
    assert policy.heaviest("work", memory_per_slot=3 * G) == 2
    assert policy.counted(MACHINE, 3, None, 2 * G) == 6 * G


# --- the line -----------------------------------------------------------------------------------


def test_a_waiting_range_says_its_least_and_its_most():
    state = State(held=(Held("work", MACHINE, "x", 5),), waiting=(item("a", 2, 4),))
    assert blocker(state, one_pool(6), ("s", "a")) == (
        "pool work holds 5 of 6; this needs at least 2 (asks up to 4)"
    )
    state = State(held=(Held("work", MACHINE, "x", 5),), waiting=(item("a", 2),))
    assert blocker(state, one_pool(6), ("s", "a")) == "pool work holds 5 of 6; this needs 2"


# --- properties ---------------------------------------------------------------------------------


@pytest.mark.parametrize("block", range(4))
def test_with_every_most_its_least_admission_is_as_before_ranges(block):
    """Items whose `max_weight` is their `weight` are admitted exactly as before ranges were
    asked for, each at its weight, with the same lines and blockers."""
    for seed in range(block * 200, block * 200 + 200):
        rng = random.Random(seed)
        pool_set, held, waiting, served, closed, capped, standing = _random_case(rng)
        fixed = tuple(
            Waiting(**(vars(w) | {"max_weight": rng.choice([None, w.weight])})) for w in waiting
        )
        old_policy = before.AdmissionPolicy.from_pools(
            pool_set, closed=closed, standing=standing, capped=capped
        )
        old_state = before.State(
            held=tuple(_old(before.Held, h) for h in held),
            waiting=tuple(_old(before.Waiting, w) for w in waiting),
            served=served,
        )
        policy = AdmissionPolicy.from_pools(
            pool_set, closed=closed, standing=standing, capped=capped
        )
        state = State(held=held, waiting=fixed, served=served)
        weight = {w.key: w.weight for w in waiting}
        got = grants(state, policy)
        assert [g.key for g in got] == before.decide(old_state, old_policy)
        assert all(g.weight == weight[g.key] for g in got)
        assert positions(state, policy) == before.positions(old_state, old_policy)
        for w in waiting:
            assert blocker(state, policy, w.key) == before.blocker(old_state, old_policy, w.key)


@pytest.mark.parametrize("block", range(4))
def test_ranges_change_only_the_sizes_and_never_overfill(block):
    """With any ranges, the items admitted, and their order, are those admitted at the least
    alone; each is granted from its least to its most; no pool passes its top and no host its
    slots (unless already past them), and no reservation is taken."""
    for seed in range(block * 200, block * 200 + 200):
        rng = random.Random(seed)
        pool_set, held, waiting, served, closed, capped, standing = _random_case(rng)
        ranged = tuple(
            Waiting(**(vars(w) | {"max_weight": w.weight + rng.randint(0, 4)})) for w in waiting
        )
        policy = AdmissionPolicy.from_pools(
            pool_set, closed=closed, standing=standing, capped=capped
        )
        least = State(held=held, waiting=waiting, served=served)
        state = State(held=held, waiting=ranged, served=served)
        got = grants(state, policy)
        assert [g.key for g in got] == decide(least, policy)
        by_key = {w.key: w for w in ranged}
        assert all(by_key[g.key].weight <= g.weight <= by_key[g.key].most for g in got)
        tally = _Tally.of(State(held=held), policy)
        for g in got:
            w = by_key[g.key]
            grown = g.weight - w.weight
            if grown:
                tally.add(w.pool, MACHINE, w.user, w.session, w.weight)
                for _ in range(grown):
                    tally.grow(w.pool, MACHINE, 0)
                assert tally.used[w.pool] <= top(policy, w.pool)
                owed = sum(
                    max(0, q.reserved - tally.used.get(n, 0))
                    for n, q in policy.pools.items()
                    if n != w.pool
                )
                assert tally.host_used[MACHINE] + owed <= pool_set.slots
            else:
                tally.add(w.pool, MACHINE, w.user, w.session, w.weight)


def _memory_case(rng: random.Random):
    """`_random_case` with ranges, memory declared in all, per slot or assumed, a budget, a
    limit on one lease's memory, sometimes an observed pool, and sometimes the guard's
    allowance or its pressure."""
    pool_set, held, waiting, served, closed, capped, standing = _random_case(rng)
    # Mostly open and uncapped, so that extra slots are often dealt.
    closed = closed if rng.random() < 0.2 else frozenset()
    capped = capped if rng.random() < 0.2 else frozenset()
    pools = {
        name: Pool(**(vars(pool) | {"headroom": rng.choice([0, 0, 1, 3, "auto"])}))
        for name, pool in pool_set.pools.items()
    }
    pool_set = PoolSet(slots=pool_set.slots, pools=pools)
    waiting = waiting[: rng.randint(0, 4)]

    def memory() -> dict:
        kind = rng.choice(["none", "total", "per_slot"])
        if kind == "total":
            return {"memory": rng.randint(0, 6) * G}
        if kind == "per_slot":
            return {"memory_per_slot": rng.randint(0, 3) * G}
        return {}

    held = tuple(Held(**(vars(h) | memory())) for h in held)
    waiting = tuple(
        Waiting(**(vars(w) | memory() | {"max_weight": w.weight + rng.randint(0, 6)}))
        for w in waiting
    )
    observed = frozenset(rng.sample(sorted(pools), rng.randint(0, 1)))
    policy = AdmissionPolicy.from_pools(
        pool_set,
        closed=closed,
        standing=standing,
        capped=capped,
        memory=rng.randint(4, 64) * G,
        assumed_memory=rng.randint(0, 2) * G,
        memory_free=rng.choice([None] * 6 + [rng.randint(0, 12) * G]),
        memory_pressing=rng.random() < 0.1,
        lease_memory=rng.choice([None, rng.randint(1, 12) * G]),
        observed=observed,
    )
    return policy, State(held=held, waiting=waiting, served=served)


def _held(items: list[tuple[Waiting, int]]) -> tuple[Held, ...]:
    return tuple(
        Held(w.pool, MACHINE, w.user, weight, w.session, w.memory, w.memory_per_slot)
        for w, weight in items
    )


def _all_fit(policy: AdmissionPolicy, held: tuple[Held, ...], items: list[Waiting]) -> bool:
    """Whether `items` fit one after another beside `held`, by the capacity rules alone."""
    tally = _Tally.of(State(held=held), policy)
    for w in items:
        if not fits(tally, policy, w.pool, w.weight, w.memory, w.memory_per_slot):
            return False
        need = policy.counted(MACHINE, w.weight, w.memory, w.memory_per_slot)
        tally.add(w.pool, MACHINE, w.user, w.session, w.weight, need)
    return True


@pytest.mark.parametrize("block", range(4))
def test_extra_slots_never_take_what_a_kept_for_item_needs(block):
    """With memory in play: the items admitted are those admitted at the least; each is
    granted from its least to its most, and no more than one lease's memory allows; no extra
    slot is granted while memory is short or pressing. Where any extra slot is granted, an item
    whose turn came but which was not admitted, once the leases of the other pools end, fits
    beside the sizes granted whenever it fits beside the leasts (and so do all of them
    together, once the pools without such an item empty); and with nothing kept for, the host
    still has its largest open pool's headroom free, in slots and in assumed memory."""
    for seed in range(block * 2000, block * 2000 + 2000):
        rng = random.Random(seed)
        policy, state = _memory_case(rng)
        got = grants(state, policy)
        least = State(
            held=state.held,
            waiting=tuple(Waiting(**(vars(w) | {"max_weight": None})) for w in state.waiting),
            served=state.served,
        )
        assert [g.key for g in got] == decide(least, policy)
        by_key = {w.key: w for w in state.waiting}
        assert all(by_key[g.key].weight <= g.weight <= by_key[g.key].most for g in got)
        limit = policy.lease_memory.get(MACHINE)
        for g in got:
            w = by_key[g.key]
            if limit is not None and w.memory_per_slot and g.weight > w.weight:
                assert g.weight * w.memory_per_slot <= limit
        if all(g.weight == by_key[g.key].weight for g in got):
            continue
        assert MACHINE not in policy.memory_free and MACHINE not in policy.memory_pressing
        _, stopped = _admit_least(state, policy, _Tally.of(state, policy))
        kept = [w for items in stopped.values() for w in items]
        granted = [(by_key[g.key], g.weight) for g in got]
        leasts = [(by_key[g.key], by_key[g.key].weight) for g in got]
        for k in kept:
            old = tuple(h for h in state.held if h.pool == k.pool)
            if _all_fit(policy, old + _held(leasts), [k]):
                assert _all_fit(policy, old + _held(granted), [k]), (seed, k)
        if kept:
            busy = {k.pool for k in kept}
            old = tuple(h for h in state.held if h.pool in busy)
            if _all_fit(policy, old + _held(leasts), kept):
                assert _all_fit(policy, old + _held(granted), kept), seed
            continue
        open_ = [
            q.headroom
            for name, q in policy.pools.items()
            if name not in policy.closed | policy.capped | policy.observed
            and q.headroom < top(policy, name)
        ]
        room = max(open_, default=0)
        after = _Tally.of(State(held=state.held + _held(granted)), policy)
        assert policy.slots[MACHINE] - after.host_used.get(MACHINE, 0) >= room, seed
        spare = policy.memory[MACHINE] - after.memory.get(MACHINE, 0)
        assert spare >= room * policy.assumed_memory.get(MACHINE, 0), seed
