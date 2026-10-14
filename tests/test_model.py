"""Pools and their validation (leasehold.model): a `PoolSet` is valid only when every pool's
reservation fits its own `max`, all reservations fit the machine's slots and there is at least
one pool; every problem is reported at once; pools are served by priority, then name."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from leasehold.model import Pool, PoolSet


def pool_set(slots: int = 4, **pools: dict) -> PoolSet:
    return PoolSet.model_validate({"slots": slots, "pools": pools})


def test_a_reservation_filling_the_slots_and_its_own_max_is_valid():
    pools = pool_set(4, work={"reserved": 3, "max": 3}, ci={"reserved": 1, "max": 1})
    assert pools.pools["work"] == Pool(reserved=3, max=3)
    assert sum(p.reserved for p in pools.pools.values()) == pools.slots


def test_a_pool_may_allow_more_than_the_slots_since_the_slots_still_cap_it():
    assert pool_set(2, work={"max": 8}).pools["work"].max == 8


@pytest.mark.parametrize(
    ("pools", "message"),
    [
        ({"work": {"reserved": 3, "max": 2}}, "reserved 3 is more than max 2"),
        (
            {"work": {"reserved": 3, "max": 4}, "ci": {"reserved": 2, "max": 4}},
            "pools reserve 5 slots, more than the machine's 4",
        ),
        ({}, "at least one pool is needed"),
    ],
    ids=["reserved over its max", "reservations over the slots", "no pools"],
)
def test_an_inconsistent_pool_set_is_refused(pools, message):
    with pytest.raises(ValidationError, match=message):
        pool_set(4, **pools)


def test_every_problem_is_reported_at_once():
    with pytest.raises(ValidationError) as info:
        pool_set(2, work={"reserved": 3, "max": 1}, ci={"reserved": 2, "max": 1})
    text = str(info.value)
    assert "pools.work: reserved 3" in text
    assert "pools.ci: reserved 2" in text
    assert "more than the machine's 2" in text


@pytest.mark.parametrize(
    "field",
    [
        {"max": 0},
        {"max": 1, "reserved": -1},
        {"max": 1, "priority": -1},
        {"max": 1, "queue_timeout_s": 0},
        {"max": 1, "per_user_max_running": 0},
        {"max": 1, "per_user_max_queued": -1},
        {"max": 1, "create_rate_per_min": 0},
        {"max": 1, "burst": 2},
        {"reserved": 0},
    ],
    ids=lambda f: ",".join(f),
)
def test_a_pool_field_out_of_range_unknown_or_missing_is_refused(field):
    with pytest.raises(ValidationError):
        Pool.model_validate(field)


def test_a_per_user_queued_limit_of_zero_is_allowed():
    assert Pool(max=1, per_user_max_queued=0).per_user_max_queued == 0


@pytest.mark.parametrize("name", ["Work", "-ci", "a b", "x" * 65, ""])
def test_a_pool_name_is_lowercase_letters_digits_dashes_and_underscores(name):
    with pytest.raises(ValidationError):
        pool_set(4, **{name: {"max": 1}})


def test_unknown_top_level_fields_are_refused():
    with pytest.raises(ValidationError):
        PoolSet.model_validate({"slots": 1, "pools": {"work": {"max": 1}}, "hosts": {}})


def test_pools_are_served_by_priority_then_name():
    pools = pool_set(
        4,
        lend={"max": 1, "priority": 2},
        ci={"max": 1, "priority": 1},
        agents={"max": 1, "priority": 1},
        work={"max": 1},
    )
    assert pools.in_order() == ["work", "agents", "ci", "lend"]
