"""What `GET /v1/status` answers (leasehold.status), built without a daemon: schema 2 names the
machine without any parent or trust, reports standing weight under `standing`, counts what is
held per pool and on the machine, and flags weight held past a pool's max or the slots."""

from __future__ import annotations

from support import make_agent

from leasehold import __version__, status
from leasehold.ledger import Holder, LedgerItem
from leasehold.status import SCHEMA

TOP_LEVEL = {
    "schema",
    "version",
    "time",
    "machine",
    "mode",
    "facts",
    "observe",
    "ceiling",
    "slots",
    "held",
    "overcommitted",
    "pools",
    "load",
    "lend",
    "guard",
    "memory",
    "contain",
    "standing",
    "leases",
    "queue",
}
POOL_FIELDS = {
    "reserved",
    "max",
    "priority",
    "preemptible",
    "observed",
    "closed",
    "held_weight",
    "overcommitted",
    "running",
    "revoked",
    "queued",
    "memory_counted_bytes",
}


def test_the_status_has_the_schema_2_shape(tmp_path):
    agent, clock, _ = make_agent(tmp_path, name="buildbox", labels={"rack": "a1"})
    st = agent.status()
    assert st["schema"] == SCHEMA == 2
    assert set(st) == TOP_LEVEL
    assert not {"node", "trust", "parent", "reserved"} & set(st)
    assert st["version"] == __version__ and st["time"] == clock.now
    assert st["machine"] == {"name": "buildbox", "labels": {"rack": "a1"}}
    assert st["ceiling"] == {
        "slots": 2,
        "cpus": 4.0,
        "memory": f"{16 << 30}b",
        "memory_bytes": 16 << 30,
    }
    assert (st["slots"], st["held"], st["overcommitted"]) == (2, 0, 0)
    assert st["standing"] == [] and st["leases"] == [] and st["queue"] == []
    assert set(st["pools"]) == {"work", "external", "lend", "observed"}
    for pool in st["pools"].values():
        assert set(pool) == POOL_FIELDS
    flags = {name: (p["preemptible"], p["observed"]) for name, p in st["pools"].items()}
    assert flags == {
        "work": (False, False),
        "external": (False, False),
        "lend": (True, False),
        "observed": (False, True),
    }


def test_standing_weight_counts_as_held_but_not_as_running(tmp_path):
    agent, _, _ = make_agent(
        tmp_path,
        ceiling={"slots": 4},
        standing=[{"name": "vm", "pool": "work", "weight": 2, "cpus": 2, "memory": "2g"}],
    )
    st = agent.status()
    assert st["standing"] == [
        {
            "name": "vm",
            "kind": "workload",
            "pool": "work",
            "weight": 2,
            "cpus": 2.0,
            "memory_bytes": 2 << 30,
            "note": None,
        }
    ]
    work = st["pools"]["work"]
    assert (work["held_weight"], work["running"], work["overcommitted"]) == (2, 0, 0)
    assert st["held"] == 2


def test_leases_and_the_queue_are_listed_with_place_and_blocker(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    agent.ledger.try_admit(
        LedgerItem("leasehold", "E", "external", "ann", "external", 1, session="t1"),
        agent.policy(),
        ttl_s=60,
    )
    agent.ledger.try_admit(
        LedgerItem("leasehold", "F", "external", "bob", "external", 1), agent.policy(), ttl_s=60
    )
    st = agent.status()
    [lease] = st["leases"]
    assert (lease["lease_id"], lease["user"], lease["session"]) == ("E", "ann", "t1")
    assert lease["revoked"] is False and lease["grant"] == 1
    [queued] = st["queue"]
    assert (queued["lease_id"], queued["position"]) == ("F", 1)
    assert queued["blocked"] == "pool external holds 1 of 1; this needs 1"
    assert st["pools"]["external"]["queued"] == 1


def test_weight_imposed_past_the_limits_is_reported_as_overcommitted(tmp_path):
    agent, _, _ = make_agent(tmp_path)  # 2 slots; observed may hold 2
    seen = LedgerItem("observer", "build", "observed", None, "observed", 3)
    assert agent.ledger.impose(seen, agent.policy(), holder=Holder.current())
    st = agent.status()
    observed = st["pools"]["observed"]
    assert (observed["held_weight"], observed["max"], observed["overcommitted"]) == (3, 2, 1)
    assert (st["held"], st["overcommitted"]) == (3, 1)
    assert st["pools"]["work"]["overcommitted"] == 0


def test_a_pool_max_override_shows_in_the_pools_max(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.set_pool_max("work", 1)
    assert agent.status()["pools"]["work"]["max"] == 1


def test_a_pool_max_below_its_standing_weight_is_refused(tmp_path):
    import pytest

    agent, _, _ = make_agent(
        tmp_path,
        ceiling={"slots": 4},
        pools={"work": {"reserved": 0, "max": 4, "priority": 0}},
        kinds={"work": "work"},
        lend={"pools": [], "quiet_s": 0},
        standing=[{"name": "vm", "pool": "work", "weight": 2}],
    )
    with pytest.raises(ValueError, match="standing work holds"):
        agent.set_pool_max("work", 1)
    assert agent.set_pool_max("work", 2)["max"] == 2


def test_impose_refuses_an_unknown_pool_and_takes_the_item_out_of_line(tmp_path):
    import pytest

    agent, _, _ = make_agent(tmp_path)
    ghost = LedgerItem("observer", "g", "observed", None, "nowhere", 1)
    with pytest.raises(ValueError, match="no pool 'nowhere'"):
        agent.ledger.impose(ghost, agent.policy(), holder=Holder.current())
    seen = LedgerItem("observer", "s", "observed", None, "observed", 1)
    agent.ledger.try_admit(seen, agent.policy(), ttl_s=60)
    assert agent.ledger.impose(seen, agent.policy(), holder=Holder.current()) in (True, False)
    assert not [r for r in agent.ledger.queued() if r["item_id"] == "s"]


def test_lending_reports_the_limits_it_is_judged_by(tmp_path):
    agent, _, _ = make_agent(tmp_path, lend={"cpu_pressure_max": 7.5})
    thresholds = agent.status()["lend"]["thresholds"]
    assert thresholds == {"cpu": 7.5, "memory": 5.0, "load_per_cpu": 0.25}


def test_a_lease_in_a_pool_the_config_dropped_still_counts_as_held(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    st = agent.status()
    stray = [{"pool": "gone", "weight": 2, "lease_id": "x"}]
    report = status.report(
        config=agent.config,
        limits=agent.limits,
        policy=agent.policy(),
        facts=st["facts"],
        load=st["load"],
        lend=st["lend"],
        leases=stray,
        queue=[],
        now=st["time"],
        version="t",
    )
    assert report["held"] == 2
