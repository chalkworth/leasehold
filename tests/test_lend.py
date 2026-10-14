"""Lending (leasehold.lend and the machine agent's lend pools), on an injected clock: a lend
lease is granted only once the machine has been idle for the quiet period, inside its window,
and revoked as soon as anything else waits or pressure rises; work is never revoked."""

from __future__ import annotations

import pytest
from support import lease_ids, make_agent

from leasehold.agent import AcquireRequest
from leasehold.lend import LendGate, Observation, in_window
from leasehold.machine import LendSettings

# --- the gate -----------------------------------------------------------------------------------


def obs(now=0.0, minute=0, queued=0, cpu=0.0, memory=0.0, load=None) -> Observation:
    return Observation(now, minute, queued, cpu, memory, load)


def test_the_gate_opens_only_after_the_quiet_period_and_any_busy_moment_restarts_it():
    gate = LendGate(LendSettings())  # 600 s quiet
    assert not gate.observe(obs(0)).eligible
    assert not gate.observe(obs(599)).eligible
    assert gate.observe(obs(600)).eligible
    verdict = gate.observe(obs(601, cpu=50))
    assert not verdict.eligible and "cpu pressure" in verdict.revoke
    assert not gate.observe(obs(602)).eligible
    assert gate.observe(obs(1202)).eligible
    assert "non-lend" in gate.observe(obs(1203, queued=1)).revoke
    assert "memory" in gate.observe(obs(1204, memory=9)).revoke


def test_without_psi_the_gate_falls_back_to_load_average_and_unknown_is_busy():
    gate = LendGate(LendSettings(quiet_s=0))
    assert gate.observe(obs(cpu=None, memory=None, load=0.1)).eligible
    assert "load 0.90" in gate.observe(obs(cpu=None, memory=None, load=0.9)).revoke
    assert "unknown" in gate.observe(obs(cpu=None, memory=None, load=None)).revoke


@pytest.mark.parametrize(
    ("window", "minute", "inside"),
    [
        ((0, 420), 0, True),
        ((0, 420), 419, True),
        ((0, 420), 420, False),
        ((1380, 360), 1430, True),
        ((1380, 360), 100, True),
        ((1380, 360), 600, False),
    ],
)
def test_the_window_may_wrap_midnight(window, minute, inside):
    assert in_window(window, minute) is inside


def test_a_window_must_be_hh_mm_hh_mm():
    assert LendSettings(window="22:30-06:00").window_minutes() == (1350, 360)
    with pytest.raises(ValueError, match="is not HH:MM-HH:MM"):
        LendSettings(window="22:30-6:00")


# --- the agent ----------------------------------------------------------------------------------


def lend(**kw) -> AcquireRequest:
    return AcquireRequest(kind="lend", **kw)


def work(**kw) -> AcquireRequest:
    return AcquireRequest(kind="work", **kw)


def test_lend_waits_out_the_quiet_period_then_is_granted(tmp_path):
    agent, clock, _ = make_agent(tmp_path, lend={"quiet_s": 600})
    agent.tick()
    # (a TTL long enough for its place in line to outlast the jump without polling)
    answer = agent.acquire(lend(lease_id="L", ttl_s=3600), user="ann")
    assert (answer["status"], answer["blocked"]) == ("queued", "pool lend is closed")
    assert agent.status()["pools"]["lend"]["closed"] is True
    clock.now += 600
    assert agent.tick()["admitted"] == [("leasehold", "L")]
    assert agent.acquire(lend(lease_id="L", ttl_s=3600), user="ann")["status"] == "granted"


def test_queued_work_revokes_lend_at_once_and_gets_the_slot_on_release(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    assert agent.acquire(lend(lease_id="L"), user="ann")["status"] == "granted"
    # work may use both slots, but the lend lease holds one: the work waits, lending stops.
    assert agent.acquire(work(weight=2, lease_id="W"), user="bob")["status"] == "queued"
    renewal = agent.renew("L", user="ann")
    assert renewal["status"] == "revoked" and "non-lend" in renewal["reason"]
    assert agent.acquire(lend(lease_id="L2"), user="ann")["status"] == "queued"
    agent.release("L", user="ann")
    assert agent.acquire(work(weight=2, lease_id="W"), user="bob")["status"] == "granted"
    assert agent.tick()["revoked"] == []  # a tick never revokes work
    assert lease_ids(agent) == [("W", "work", False)]


def test_pressure_over_the_threshold_revokes_lend_but_never_work(tmp_path):
    agent, _, load = make_agent(tmp_path, ceiling={"slots": 4})
    agent.tick()
    assert agent.acquire(lend(lease_id="L"), user="ann")["status"] == "granted"
    assert agent.acquire(work(lease_id="W"), user="ann")["status"] == "granted"
    load.set(cpu=80)
    assert agent.tick()["revoked"] == [("leasehold", "L")]
    assert sorted(lease_ids(agent)) == [("L", "lend", True), ("W", "work", False)]


def test_pressure_can_be_measured_over_chosen_cgroups_so_lent_work_does_not_revoke_itself(
    tmp_path,
):
    agent, _, load = make_agent(tmp_path, lend={"pressure_cgroups": ["work.slice"]})
    load.set(cpu=95)  # the whole machine: the lent work itself
    load.by_cgroup["work.slice"] = {"cpu": 1.0, "memory": 0.0}
    agent.tick()
    assert agent.acquire(lend(), user="ann")["status"] == "granted"
    load.by_cgroup["work.slice"]["cpu"] = 40.0
    assert len(agent.tick()["revoked"]) == 1


def test_lend_is_held_shut_outside_its_window(tmp_path):
    minute = {"now": 8 * 60}
    agent, _, _ = make_agent(
        tmp_path, minute=lambda _n: minute["now"], lend={"window": "00:00-07:00"}
    )
    assert agent.tick()["lend"]["eligible"] is False
    assert agent.acquire(lend(lease_id="L"), user="ann")["status"] == "queued"
    minute["now"] = 60
    assert agent.tick()["admitted"] == [("leasehold", "L")]
    minute["now"] = 7 * 60
    assert agent.tick()["revoked"] == [("leasehold", "L")]


def test_switching_lending_off_closes_it_and_revokes_what_it_holds(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    assert agent.acquire(lend(lease_id="L"), user="ann")["status"] == "granted"
    state = agent.set_lend(False)
    assert state["eligible"] is False and state["switch"] is False
    assert state["reason"] == "lending switched off"
    assert lease_ids(agent) == [("L", "lend", True)]
    agent.tick()  # an idle machine does not reopen it
    assert agent.status()["lend"]["eligible"] is False
    agent.release("L", user="ann")
    assert agent.set_lend(True)["eligible"] is True
    assert agent.acquire(lend(lease_id="L2"), user="ann")["status"] == "granted"
