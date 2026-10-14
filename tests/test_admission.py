"""The pure admission decision (leasehold.admission): reservations are never lent, no pool
passes its max, no user passes their per-user limit, users take turns and so do each user's
sessions, one session's items stay FIFO, a blocked head is never backfilled around, and
`blocker` says in words why a waiting item is not admitted."""

from __future__ import annotations

import random

import pytest

from leasehold.admission import (
    AdmissionPolicy,
    Held,
    PoolPolicy,
    State,
    Waiting,
    blocker,
    decide,
    positions,
)
from leasehold.model import MACHINE, PoolSet

SCOPE = "ci"


def waiting(
    n: int,
    pool: str,
    user: str | None,
    weight: int = 1,
    start: int = 0,
    prefix: str | None = None,
    session: str | None = None,
) -> list[Waiting]:
    """`n` items queued one after another from `start`, keyed `<prefix>-<i>` (by default
    `<pool>-<user>`, plus `-<session>` when there is one)."""
    tag = prefix or "-".join(str(x) for x in (pool, user, session) if x is not None)
    return [
        Waiting(
            key=(SCOPE, f"{tag}-{i}"),
            pool=pool,
            user=user,
            weight=weight,
            order=(float(start + i), start + i),
            session=session,
        )
        for i in range(n)
    ]


def names(admits) -> list[str]:
    return [item_id for _scope, item_id in admits]


def held_of(admits, state: State, policy: AdmissionPolicy) -> tuple[Held, ...]:
    """`state`'s leases plus those `admits` would make."""
    by_key = {w.key: w for w in state.waiting}
    return state.held + tuple(
        Held(
            pool=by_key[k].pool,
            host=policy.pools[by_key[k].pool].host,
            user=by_key[k].user,
            weight=by_key[k].weight,
            session=by_key[k].session,
        )
        for k in admits
    )


def used(held, pool=None) -> int:
    return sum(h.weight for h in held if pool is None or h.pool == pool)


def reservations_free(held, policy: AdmissionPolicy) -> bool:
    """Every pool's unused reservation is still free on the machine (the invariant `decide`
    preserves: no pool ever takes what another reserved)."""
    free = policy.slots[MACHINE] - used(held)
    owed = sum(max(0, p.reserved - used(held, name)) for name, p in policy.pools.items())
    return free >= owed


def one_pool(slots: int, **pool) -> AdmissionPolicy:
    """A machine of `slots` with a single pool `ci` that may use all of them."""
    return AdmissionPolicy(
        slots={MACHINE: slots},
        pools={"ci": PoolPolicy(host=MACHINE, reserved=0, max=slots, **pool)},
    )


def two_pools(slots: int, work: dict, ci: dict) -> AdmissionPolicy:
    return AdmissionPolicy(
        slots={MACHINE: slots},
        pools={
            "work": PoolPolicy(MACHINE, **({"reserved": 0, "max": slots, "priority": 0} | work)),
            "ci": PoolPolicy(MACHINE, **({"reserved": 0, "max": slots, "priority": 2} | ci)),
        },
    )


# --- the property loop --------------------------------------------------------------------------

USERS = ["ann", "bob", "cy", None]
SESSIONS = [None, "s1", "s2", "s3"]


def _random_policy(rng: random.Random) -> AdmissionPolicy:
    slots = rng.randint(1, 10)
    names_ = rng.sample(["work", "agents", "ci", "bench"], rng.randint(1, 4))
    left = slots
    pools = {}
    for name in names_:
        reserved = rng.randint(0, left) if rng.random() < 0.6 else 0
        left -= reserved
        pools[name] = PoolPolicy(
            host=MACHINE,
            reserved=reserved,
            max=rng.randint(max(1, reserved), slots),
            priority=rng.randint(0, 3),
            per_user_max_running=rng.choice([None, None, 1, 2, 3]),
        )
    return AdmissionPolicy(slots={MACHINE: slots}, pools=pools)


def _random_waiting(
    rng: random.Random, policy: AdmissionPolicy, tag: str, start: int, sessions: bool
) -> list[Waiting]:
    out = []
    for i in range(rng.randint(0, 12)):
        pool = rng.choice(sorted(policy.pools))
        out.append(
            Waiting(
                key=(SCOPE, f"{tag}{i}"),
                pool=pool,
                user=rng.choice(USERS),
                weight=rng.randint(1, policy.pools[pool].max),
                order=(float(rng.randint(0, 5)), start + i),
                session=rng.choice(SESSIONS) if sessions else None,
            )
        )
    return out


def _check_invariants(rng: random.Random, case: int, *, sessions: bool) -> bool:
    """One random reachable state: `decide` keeps every invariant. Whether it admitted any."""
    policy = _random_policy(rng)
    # A reachable held state: admit from empty, then let a random subset finish.
    first = State(waiting=tuple(_random_waiting(rng, policy, "a", 0, sessions)))
    held = held_of(decide(first, policy), first, policy)
    held = tuple(h for h in held if rng.random() < 0.7)
    assert reservations_free(held, policy), case
    state = State(
        held=held,
        waiting=tuple(_random_waiting(rng, policy, "b", 100, sessions)),
        served={
            (p, u): rng.randint(1, 50)
            for p in policy.pools
            for u in ("ann", "bob")
            if rng.random() < 0.3
        },
        session_served={
            (p, u, s): rng.randint(1, 50)
            for p in policy.pools
            for u in ("ann", "bob")
            for s in SESSIONS
            if sessions and rng.random() < 0.3
        },
    )
    admits = decide(state, policy)
    after = held_of(admits, state, policy)
    by_key = {w.key: w for w in state.waiting}

    assert len(set(admits)) == len(admits), case
    assert reservations_free(after, policy), case
    assert used(after) <= policy.slots[MACHINE], case
    for name, pool in policy.pools.items():
        assert used(after, name) <= pool.max, case
        if pool.per_user_max_running is not None:
            for user in {h.user for h in after if h.pool == name}:
                before = sum(1 for h in held if h.pool == name and h.user == user)
                now = sum(1 for h in after if h.pool == name and h.user == user)
                assert now <= max(before, pool.per_user_max_running), case
    # FIFO within a session: in each pool, a session's admitted items are exactly the oldest
    # of its own, in order -- nothing skips ahead of its own session's earlier item.
    for name in policy.pools:
        for user, session in {(w.user, w.session) for w in state.waiting if w.pool == name}:
            line = sorted(
                (w for w in state.waiting if (w.pool, w.user, w.session) == (name, user, session)),
                key=lambda w: w.order,
            )
            got = [
                k
                for k in admits
                if (by_key[k].pool, by_key[k].user, by_key[k].session) == (name, user, session)
            ]
            assert got == [w.key for w in line[: len(got)]], case
    # Positions number each pool's line 1..n.
    places = positions(state, policy)
    for name in {w.pool for w in state.waiting}:
        count = sum(1 for w in state.waiting if w.pool == name)
        assert sorted(places[w.key] for w in state.waiting if w.pool == name) == list(
            range(1, count + 1)
        ), case
    return bool(admits)


@pytest.mark.parametrize("sessions", [False, True], ids=["no sessions", "sessions"])
def test_admission_keeps_its_invariants_over_random_states(sessions):
    """Over a thousand random reachable states, with and without sessions: reservations are
    never lent, no pool passes its max nor the machine its slots, no user passes the per-user
    limit, each session stays FIFO, and positions number each pool's line from 1."""
    rng = random.Random(20260926)
    admitted_any = sum(_check_invariants(rng, case, sessions=sessions) for case in range(1000))
    assert admitted_any > 500  # the sample exercises admission, not just refusals


# --- the rules, one at a time -------------------------------------------------------------------


def test_a_user_with_one_item_is_admitted_within_one_rotation_of_another_with_fifty():
    policy = one_pool(1)
    pending = waiting(50, "ci", "ann") + waiting(1, "ci", "bob", start=50)
    served: dict = {}
    admitted = []
    for _ in range(3):  # one slot: admit, finish, admit ...
        [key] = decide(State(waiting=tuple(pending), served=served), policy)
        admitted.append(key[1])
        user = "ann" if "ann" in key[1] else "bob"
        served[("ci", user)] = len(admitted)
        pending = [w for w in pending if w.key != key]
    assert admitted == ["ci-ann-0", "ci-bob-0", "ci-ann-1"]


def test_within_one_call_users_alternate_and_each_stays_fifo():
    policy = one_pool(4)
    state = State(waiting=tuple(waiting(5, "ci", "ann") + waiting(5, "ci", "bob", start=5)))
    assert names(decide(state, policy)) == ["ci-ann-0", "ci-bob-0", "ci-ann-1", "ci-bob-1"]
    assert positions(state, policy)[(SCOPE, "ci-bob-0")] == 2


def test_per_user_max_running_skips_that_user_but_not_the_pool():
    policy = one_pool(4, per_user_max_running=1)
    state = State(
        held=(Held("ci", MACHINE, "ann", 1),),
        waiting=tuple(waiting(3, "ci", "ann") + waiting(3, "ci", "bob", start=3)),
    )
    assert names(decide(state, policy)) == ["ci-bob-0"]


def test_a_blocked_head_is_not_backfilled_around():
    policy = one_pool(3)
    state = State(
        held=(Held("ci", MACHINE, "cy", 2),),
        waiting=(
            *waiting(1, "ci", "ann", weight=2),
            *waiting(1, "ci", "ann", start=1, prefix="small"),
            *waiting(1, "ci", "bob", start=2),
        ),
    )
    assert decide(state, policy) == []  # ann's weight-2 head does not fit: nothing goes round it


def test_a_reservation_is_never_lent_and_nothing_is_preempted():
    policy = two_pools(4, work={"reserved": 2}, ci={})
    idle_work = State(waiting=tuple(waiting(4, "ci", "ann")))
    assert len(decide(idle_work, policy)) == 2  # work's two reserved slots stay free, however idle
    busy = State(
        held=(Held("ci", MACHINE, "ann", 1), Held("ci", MACHINE, "ann", 1)),
        waiting=tuple(waiting(3, "work", "bob")),
    )
    assert len(decide(busy, policy)) == 2  # work gets its reservation, ci keeps its two


def test_pools_are_served_in_priority_order():
    policy = two_pools(1, work={"max": 1}, ci={"max": 1})
    state = State(waiting=(*waiting(1, "ci", "ann"), *waiting(1, "work", "ann", start=1)))
    assert names(decide(state, policy)) == ["work-ann-0"]


def test_a_closed_pool_admits_nothing_and_does_not_block_another():
    open_ = two_pools(2, work={"max": 1}, ci={"max": 1})
    closed = AdmissionPolicy(open_.slots, open_.pools, closed=frozenset({"work"}))
    state = State(waiting=(*waiting(1, "work", "ann"), *waiting(1, "ci", "ann", start=1)))
    assert names(decide(state, closed)) == ["ci-ann-0"]
    assert positions(state, closed)[(SCOPE, "work-ann-0")] == 1  # its place is kept


def test_weight_held_beyond_a_max_blocks_that_pool_and_the_machine_until_it_ends():
    """An imposed lease may exceed its pool's max and the slots: nothing fits beside it."""
    policy = two_pools(2, work={"max": 1}, ci={"max": 2})
    over = State(
        held=(Held("work", MACHINE, None, 3),),
        waiting=(*waiting(1, "work", "ann"), *waiting(1, "ci", "bob", start=1)),
    )
    assert decide(over, policy) == []
    assert names(decide(State(waiting=over.waiting), policy)) == ["work-ann-0", "ci-bob-0"]


# --- standing weight ----------------------------------------------------------------------------


def test_standing_weight_leaves_one_slot_and_does_not_count_as_running():
    """Weight 2 standing in a 3-slot pool leaves one slot, for real leases only."""
    policy = AdmissionPolicy(
        slots={MACHINE: 3},
        pools={"work": PoolPolicy(MACHINE, reserved=1, max=3)},
        standing={"work": 2},
    )
    assert policy.pools["work"].reserved == 1  # the pool's guarantee is unchanged
    state = State(waiting=tuple(waiting(1, "work", "ann") + waiting(1, "work", "bob", start=1)))
    assert names(decide(state, policy)) == ["work-ann-0"]
    assert policy.refusal("work", 1) is None
    assert "held by standing work" in policy.refusal("work", 2)


def test_standing_weight_is_not_anyones_running_item():
    """Under a per-user limit of one, ann still gets her turn beside standing weight."""
    policy = AdmissionPolicy(
        slots={MACHINE: 3},
        pools={"work": PoolPolicy(MACHINE, reserved=0, max=3, per_user_max_running=1)},
        standing={"work": 1},
    )
    both = State(waiting=tuple(waiting(3, "work", "ann") + waiting(1, "work", "bob", start=3)))
    assert names(decide(both, policy)) == ["work-ann-0", "work-bob-0"]


def test_no_standing_weight_decides_like_none_at_all():
    bare = one_pool(3)
    same = AdmissionPolicy(slots=bare.slots, pools=bare.pools, standing={})
    pending = State(waiting=tuple(waiting(4, "ci", "ann")))
    assert decide(pending, bare) == decide(pending, same)


# --- the policy from a pool set -----------------------------------------------------------------


def test_the_policy_comes_from_a_pool_set_and_refuses_the_impossible():
    pools = PoolSet.model_validate(
        {
            "slots": 6,
            "pools": {
                "work": {"reserved": 3, "max": 6, "per_user_max_running": 2},
                "ci": {"max": 2, "priority": 1},
            },
        }
    )
    policy = AdmissionPolicy.from_pools(pools, closed=frozenset({"ci"}), standing={"work": 1})
    assert policy.slots == {MACHINE: 6}
    assert policy.pools["work"] == PoolPolicy(MACHINE, 3, 6, 0, 2)
    assert policy.pools["ci"] == PoolPolicy(MACHINE, 0, 2, 1, None)
    assert policy.closed == frozenset({"ci"}) and policy.standing == {"work": 1}
    assert policy.refusal("ci", 2) is None
    assert policy.refusal("ci", 3) == "weight 3 is more than pool 'ci''s max 2"
    assert policy.refusal("gone", 1) == "there is no pool 'gone'"
    big = AdmissionPolicy.from_pools(
        PoolSet.model_validate({"slots": 2, "pools": {"work": {"max": 8}}})
    )
    assert big.refusal("work", 3) == "weight 3 is more than the 2 slots there are"


# --- sessions -----------------------------------------------------------------------------------


def test_one_users_sessions_take_turns():
    policy = one_pool(4)
    state = State(
        waiting=(
            *waiting(3, "ci", "ann", session="a"),
            *waiting(3, "ci", "ann", start=3, session="b"),
        )
    )
    assert names(decide(state, policy)) == ["ci-ann-a-0", "ci-ann-b-0", "ci-ann-a-1", "ci-ann-b-1"]


def test_splitting_into_many_sessions_wins_no_turns_from_another_user():
    """Ann's five sessions share ann's turns; bob, with one, gets as many as she does."""
    policy = one_pool(4)
    ann = [w for s in range(5) for w in waiting(1, "ci", "ann", start=s, session=f"s{s}")]
    state = State(waiting=(*ann, *waiting(5, "ci", "bob", start=5)))
    admitted = decide(state, policy)
    assert sum("ann" in item_id for _, item_id in admitted) == 2
    assert sum("bob" in item_id for _, item_id in admitted) == 2


def test_the_rotation_between_sessions_carries_over_between_calls():
    """With one slot, the session served last waits behind the other, even with older items."""
    policy = one_pool(1)
    state = State(
        waiting=(
            *waiting(2, "ci", "ann", session="a"),
            *waiting(1, "ci", "ann", start=2, session="b"),
        ),
        served={("ci", "ann"): 1},
        session_served={("ci", "ann", "a"): 1},
    )
    assert names(decide(state, policy)) == ["ci-ann-b-0"]


def test_a_sessions_items_stay_fifo():
    policy = one_pool(3)
    state = State(waiting=tuple(reversed(waiting(3, "ci", "ann", session="a"))))
    assert names(decide(state, policy)) == ["ci-ann-a-0", "ci-ann-a-1", "ci-ann-a-2"]


def test_items_without_a_session_share_one():
    """Ann's session-less items take one turn between them, not one each: with two slots, her
    named session gets the second."""
    policy = one_pool(2)
    state = State(
        waiting=(
            *waiting(3, "ci", "ann"),
            *waiting(1, "ci", "ann", start=3, session="x"),
        )
    )
    assert names(decide(state, policy)) == ["ci-ann-0", "ci-ann-x-0"]


def test_a_session_running_more_waits_behind_one_running_less():
    policy = one_pool(3)
    state = State(
        held=(Held("ci", MACHINE, "ann", 1, session="a"),),
        waiting=(
            *waiting(1, "ci", "ann", session="a"),
            *waiting(1, "ci", "ann", start=1, session="b"),
        ),
    )
    assert names(decide(state, policy)) == ["ci-ann-b-0", "ci-ann-a-0"]


# --- why an item waits --------------------------------------------------------------------------


def item_key(name: str) -> tuple[str, str]:
    return (SCOPE, name)


def test_blocker_names_a_closed_pool():
    policy = AdmissionPolicy(one_pool(1).slots, one_pool(1).pools, closed=frozenset({"ci"}))
    state = State(waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) == "pool ci is closed"


def test_blocker_counts_the_items_ahead():
    policy = one_pool(1)
    state = State(
        held=(Held("ci", MACHINE, "cy", 1),),
        waiting=(*waiting(2, "ci", "ann"), *waiting(1, "ci", "bob", start=2)),
    )
    # bob's turn comes before ann's second item: ann-1 has two ahead of it.
    assert blocker(state, policy, item_key("ci-ann-1")) == "2 ahead of it in pool ci"
    assert blocker(state, policy, item_key("ci-bob-0")) == "1 ahead of it in pool ci"


def test_blocker_names_the_per_user_limit():
    policy = one_pool(3, per_user_max_running=1)
    state = State(held=(Held("ci", MACHINE, "ann", 1),), waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) == (
        "its user already runs 1 in pool ci, the per-user limit"
    )


def test_blocker_names_a_full_pool():
    policy = two_pools(4, work={}, ci={"max": 2})
    state = State(held=(Held("ci", MACHINE, "bob", 2),), waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) == "pool ci holds 2 of 2; this needs 1"


def test_blocker_names_slots_reserved_for_other_pools():
    policy = two_pools(3, work={"reserved": 2}, ci={})
    state = State(held=(Held("ci", MACHINE, "bob", 1),), waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) == (
        "2 of 3 slots are free, but 2 are reserved for other pools"
    )


def test_blocker_names_a_machine_with_no_free_slots():
    policy = two_pools(2, work={}, ci={})
    state = State(held=(Held("work", MACHINE, "bob", 2),), waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) == "0 of 2 slots are free; this needs 1"


def test_blocker_is_none_for_an_item_that_fits_or_is_not_waiting():
    policy = one_pool(2)
    state = State(waiting=tuple(waiting(1, "ci", "ann")))
    assert blocker(state, policy, item_key("ci-ann-0")) is None
    assert blocker(state, policy, item_key("nobody")) is None


def test_standing_weight_elsewhere_refuses_what_could_never_fit():
    """Standing weight in one pool shrinks what any pool can take: an item that could not fit
    even on an otherwise idle machine is refused, not queued to block its pool forever."""
    policy = AdmissionPolicy(
        slots={MACHINE: 3},
        pools={
            "work": PoolPolicy(MACHINE, reserved=0, max=3),
            "ci": PoolPolicy(MACHINE, reserved=0, max=3),
        },
        standing={"work": 2},
    )
    assert policy.refusal("ci", 2) is not None
    assert policy.refusal("ci", 1) is None
