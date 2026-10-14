"""The machine agent's requests (leasehold.agent), without a socket: kinds go to their pools and
only there, `observed` is never granted, a lease belongs to the user who took it, every lease
lapses at its TTL unless renewed, a queued answer says why it waits, sessions come from the
request, and an administrator's pool `max` override stays within the machine's slots."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from support import lease_ids, make_agent

from leasehold.agent import SCOPE, AcquireRequest, Forbidden


def req(kind: str, **kw) -> AcquireRequest:
    return AcquireRequest(kind=kind, **kw)


def test_a_lease_not_renewed_within_its_ttl_is_gone_and_frees_its_slot(tmp_path):
    agent, clock, _ = make_agent(tmp_path)
    agent.tick()
    assert agent.acquire(req("external", lease_id="E", ttl_s=10), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F"), user="bob")["status"] == "queued"
    clock.now += 8
    assert agent.renew("E", user="ann")["status"] == "ok"
    clock.now += 11
    assert agent.renew("E", user="ann")["status"] == "unknown"
    assert agent.acquire(req("external", lease_id="F"), user="bob")["status"] == "granted"


def test_a_queued_answer_gives_the_place_and_why_it_waits(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    agent.acquire(req("external", lease_id="E"), user="ann")
    answer = agent.acquire(req("external", lease_id="F"), user="bob")
    assert answer == {
        "status": "queued",
        "lease_id": "F",
        "position": 1,
        "blocked": "pool external holds 1 of 1; this needs 1",
    }
    [queued] = agent.leases()["queue"]
    assert (queued["position"], queued["blocked"]) == (1, answer["blocked"])


def test_the_queue_is_listed_in_line_order_not_arrival_order(tmp_path):
    agent, clock, _ = make_agent(tmp_path)
    agent.tick()
    agent.acquire(req("external", lease_id="E"), user="ann")
    agent.acquire(req("external", lease_id="G"), user="ann")  # ann already runs one
    clock.now += 1
    agent.acquire(req("external", lease_id="F"), user="bob")  # so bob, later, goes first
    queue = agent.leases()["queue"]
    assert [(q["lease_id"], q["position"]) for q in queue] == [("F", 1), ("G", 2)]


def test_kinds_and_pools_must_match(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    assert "only lend" in agent.acquire(req("work", pool="lend"), user="ann")["reason"]
    assert "no lend" in agent.acquire(req("lend", pool="work"), user="ann")["reason"]
    assert "max" in agent.acquire(req("work", weight=3), user="ann")["reason"]
    with pytest.raises(ValidationError):
        req("service")


def test_observed_leases_are_never_granted_to_a_client(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    observed = agent.acquire(req("observed"), user="ann")
    assert observed["status"] == "refused" and "never asked for" in observed["reason"]
    into_pool = agent.acquire(req("work", pool="observed"), user="ann")
    assert into_pool["status"] == "refused" and "only observed processes" in into_pool["reason"]
    assert agent.leases() == {"leases": [], "queue": []}


def test_another_users_lease_cannot_be_renewed_or_released(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    agent.acquire(req("work", lease_id="W"), user="ann")
    with pytest.raises(Forbidden):
        agent.release("W", user="bob")
    with pytest.raises(Forbidden):
        agent.renew("W", user="bob")
    agent.release("W", user="root")
    assert lease_ids(agent) == []


def test_the_session_is_the_requests_or_else_its_session_label(tmp_path):
    agent, _, _ = make_agent(tmp_path, ceiling={"slots": 4})
    agent.tick()
    agent.acquire(req("work", lease_id="a", session="term-1"), user="ann")
    agent.acquire(req("work", lease_id="b", labels={"session": "term-2"}), user="ann")
    agent.acquire(req("work", lease_id="c", labels={"session": "not a name"}), user="ann")
    sessions = {x["lease_id"]: x["session"] for x in agent.leases()["leases"]}
    assert sessions == {"a": "term-1", "b": "term-2", "c": None}
    with pytest.raises(ValidationError):
        req("work", session="not a name")


def test_a_users_sessions_take_turns_for_the_agents_slots(tmp_path):
    """One slot of `external`: ann's second session is served before her first's next item."""
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    for lease_id, session in [("a1", "a"), ("a2", "a"), ("b1", "b")]:
        agent.acquire(req("external", lease_id=lease_id, session=session, ttl_s=600), user="ann")
    agent.release("a1", user="ann")
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["b1"]


def test_a_pool_max_override_stays_within_the_slots_and_resets(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    with pytest.raises(ValueError, match="between 1 and the machine's 2 slots"):
        agent.set_pool_max("work", 3)
    with pytest.raises(ValueError, match="no pool 'gone'"):
        agent.set_pool_max("gone", 1)
    assert agent.set_pool_max("work", 1) == {
        "pool": "work",
        "max": 1,
        "configured_max": 2,
        "overridden": True,
    }
    assert agent.status()["pools"]["work"]["max"] == 1
    assert agent.acquire(req("work", weight=2), user="ann")["status"] == "refused"
    assert agent.set_pool_max("work", None)["overridden"] is False
    assert agent.status()["pools"]["work"]["max"] == 2


def test_raising_a_pool_max_admits_what_now_fits(tmp_path):
    agent, _, _ = make_agent(tmp_path, ceiling={"slots": 4})  # external may use 2
    agent.tick()
    agent.set_pool_max("external", 1)
    assert agent.acquire(req("external", lease_id="E"), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F", ttl_s=600), user="bob")["status"] == "queued"
    agent.set_pool_max("external", None)
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["E", "F"]


def test_every_lease_is_filed_under_the_agents_scope(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    agent.acquire(req("work", lease_id="W"), user="ann")
    assert [row["scope"] for row in agent.ledger.leases()] == [SCOPE]


# --- per-user quotas ---------------------------------------------------------------------------

QUOTA_POOLS = {
    "work": {"reserved": 1, "max": 2},
    "external": {"reserved": 0, "max": 1},
    "lend": {"reserved": 0, "max": 1},
    "observed": {"reserved": 0, "max": 2},
}


def _pools(**external) -> dict:
    return QUOTA_POOLS | {"external": QUOTA_POOLS["external"] | external}


def test_a_user_may_have_only_so_many_waiting(tmp_path):
    agent, _, _ = make_agent(tmp_path, pools=_pools(per_user_max_queued=1))
    agent.tick()
    assert agent.acquire(req("external", lease_id="E"), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F"), user="bob")["status"] == "queued"
    over = agent.acquire(req("external", lease_id="G"), user="bob")
    assert (over["status"], over["code"]) == ("refused", "quota_exceeded")
    assert "limit" in over["reason"]
    assert [q["lease_id"] for q in agent.leases()["queue"]] == ["F"]  # G never waited
    assert agent.acquire(req("external", lease_id="F"), user="bob")["status"] == "queued"
    assert agent.acquire(req("external", lease_id="H"), user="cat")["status"] == "queued"


def test_a_limit_of_none_waiting_still_grants_what_fits(tmp_path):
    agent, _, _ = make_agent(tmp_path, pools=_pools(per_user_max_queued=0))
    agent.tick()
    assert agent.acquire(req("external", lease_id="E"), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F"), user="ann")["code"] == "quota_exceeded"


def test_a_user_may_submit_only_so_many_a_minute(tmp_path):
    agent, clock, _ = make_agent(tmp_path, pools=_pools(create_rate_per_min=2))
    agent.tick()
    assert agent.acquire(req("external", lease_id="E"), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F"), user="ann")["status"] == "queued"
    for _ in range(3):  # asking again about a place in line is not a new submit
        assert agent.acquire(req("external", lease_id="F"), user="ann")["status"] == "queued"
    assert agent.acquire(req("external", lease_id="G"), user="ann")["code"] == "quota_exceeded"
    assert agent.acquire(req("external", lease_id="G"), user="bob")["status"] == "queued"
    clock.now += 61  # the minute has passed
    assert agent.acquire(req("external", lease_id="H"), user="ann")["status"] != "refused"


def test_the_queue_lists_pools_in_the_order_they_are_served(tmp_path):
    pools = QUOTA_POOLS | {
        "work": {"reserved": 0, "max": 1, "priority": 2},
        "external": {"reserved": 0, "max": 1, "priority": 1},
    }
    agent, _, _ = make_agent(tmp_path, pools=pools)
    agent.tick()
    for pool in ("work", "external"):
        agent.acquire(req(pool, lease_id=f"{pool}-held"), user="ann")
        agent.acquire(req(pool, lease_id=f"{pool}-waits"), user="ann")
    assert [q["lease_id"] for q in agent.leases()["queue"]] == ["external-waits", "work-waits"]


def test_a_request_refused_for_waiting_does_not_use_up_the_rate(tmp_path):
    agent, _, _ = make_agent(tmp_path, pools=_pools(per_user_max_queued=1, create_rate_per_min=3))
    agent.tick()
    assert agent.acquire(req("external", lease_id="E"), user="ann")["status"] == "granted"
    assert agent.acquire(req("external", lease_id="F"), user="ann")["status"] == "queued"
    assert agent.acquire(req("external", lease_id="G"), user="ann")["code"] == "quota_exceeded"
    agent.release("F", user="ann")
    assert agent.acquire(req("external", lease_id="H"), user="ann")["status"] == "queued"  # 3rd
    agent.release("H", user="ann")
    assert agent.acquire(req("external", lease_id="I"), user="ann")["code"] == "quota_exceeded"
