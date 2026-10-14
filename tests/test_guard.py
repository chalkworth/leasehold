"""The memory guard (`leasehold.guard`) and what it does to admission: it goes on past either
threshold and off only after settling below the lower one; while it is on every pool admits only
its reserved slots, running leases keep running, and a waiting item says why; the machine's own
daemon runs without it unless configured."""

from __future__ import annotations

import pytest
from support import make_agent
from test_admission import names, waiting

from leasehold.admission import AdmissionPolicy, PoolPolicy, State, decide
from leasehold.agent import AcquireRequest
from leasehold.guard import Guard, Reading
from leasehold.machine import GuardSettings
from leasehold.model import MACHINE

GIB = 2**30


def guard(**kw) -> Guard:
    return Guard(GuardSettings(**kw), min_available=2 * GIB)


def test_it_goes_on_past_the_pressure_threshold_and_off_only_after_settling():
    g = guard(memory_pressure_on=20, memory_pressure_off=5, settle_s=30)
    assert not g.observe(Reading(0, 19.0, 8 * GIB))
    assert g.observe(Reading(1, 21.0, 8 * GIB))
    assert g.reason == "memory pressure 21% over 20%" and g.since == 1
    assert g.observe(Reading(2, 10.0, 8 * GIB))  # between the thresholds: stays on
    assert g.observe(Reading(3, 4.0, 8 * GIB))  # clear, settling from here
    assert g.observe(Reading(20, 3.0, 8 * GIB))
    assert g.observe(Reading(25, 8.0, 8 * GIB))  # not clear: settling starts over
    assert g.observe(Reading(26, 1.0, 8 * GIB))
    assert not g.observe(Reading(56, 1.0, 8 * GIB))
    assert g.reason is None and g.since is None


def test_it_goes_on_when_available_memory_drops_under_the_floor():
    g = guard(settle_s=0)
    assert g.observe(Reading(0, None, 1 * GIB))
    assert g.reason == "1.0 GiB of memory available, under 2.0 GiB"
    assert not g.observe(Reading(1, None, 3 * GIB))


def test_without_readings_it_stays_off():
    assert not guard().observe(Reading(0, None, None))


def test_the_floor_is_five_percent_of_memory_at_least_512_mib_unless_set():
    assert GuardSettings().floor(64 * GIB) == 64 * GIB // 20
    assert GuardSettings().floor(4 * GIB) == 512 * 2**20
    assert GuardSettings(min_available="3g").floor(64 * GIB) == 3 * GIB


def test_off_must_not_be_above_on():
    with pytest.raises(ValueError, match="memory_pressure_off is above"):
        GuardSettings(memory_pressure_on=5, memory_pressure_off=10)


def _policy(capped: frozenset[str]) -> AdmissionPolicy:
    return AdmissionPolicy(
        slots={MACHINE: 8},
        pools={
            "work": PoolPolicy(host=MACHINE, reserved=4, max=8, priority=0),
            "bg": PoolPolicy(host=MACHINE, reserved=0, max=4, priority=1),
        },
        capped=capped,
    )


def test_a_capped_pool_admits_only_up_to_its_reservation():
    state = State(waiting=tuple(waiting(6, "work", "ann") + waiting(1, "bg", "bob")))
    assert len(decide(state, _policy(frozenset()))) == 7
    admits = names(decide(state, _policy(frozenset({"work", "bg"}))))
    assert admits == ["work-ann-0", "work-ann-1", "work-ann-2", "work-ann-3"]


def test_the_blocker_says_the_pool_is_held_to_its_reservation(tmp_path):
    agent, _, load = make_agent(tmp_path, guard={"settle_s": 0})
    load.set(memory=50.0)
    agent.tick()
    first = agent.acquire(AcquireRequest(kind="work", lease_id="A"), user="ann")
    second = agent.acquire(AcquireRequest(kind="work", lease_id="B"), user="ann")
    assert first["status"] == "granted"  # work reserves 1 of the 2 slots
    assert second["status"] == "queued"
    assert second["blocked"].startswith("the machine is under pressure: pool work admits only")
    status = agent.status()["guard"]
    assert status["on"] and status["enabled"] and "memory pressure 50%" in status["reason"]
    load.set(memory=0.0)
    agent.tick()
    agent.tick()
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["A", "B"]


def test_the_machines_own_daemon_runs_without_it_unless_configured(tmp_path):
    agent, _, load = make_agent(tmp_path, mode="system")
    load.set(memory=50.0)
    agent.tick()
    assert agent.status()["guard"]["enabled"] is False
    assert (
        agent.acquire(AcquireRequest(kind="work", lease_id="A"), user="ann")["status"] == "granted"
    )
    assert (
        agent.acquire(AcquireRequest(kind="work", lease_id="B"), user="ann")["status"] == "granted"
    )
