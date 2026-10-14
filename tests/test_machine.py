"""A machine's own configuration (leasehold.machine): the default pools, which kinds may go in
which pools, `[[standing]]` weight counted as held without being a lease, the ceiling filled in
from the machine's own size."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from support import FACTS, Clock, FakeLoad, machine_config

from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.machine import (
    MachineConfig,
    MachineLimits,
    default_pools,
    load,
)
from leasehold.model import MACHINE, Pool


def req(kind: str, **kw) -> AcquireRequest:
    return AcquireRequest(kind=kind, **kw)


def _machine(**extra) -> dict:
    """A 4-slot machine with `work` (reserving 2) and `external`, and no lending."""
    raw = {
        "name": "buildbox",
        "ceiling": {"slots": 4},
        "pools": {
            "work": {"reserved": 2, "max": 4, "priority": 0},
            "external": {"reserved": 0, "max": 2, "priority": 1},
        },
        "kinds": {"work": "work", "external": "external"},
        "lend": {"pools": []},
    }
    raw.update(extra)
    return raw


# --- pools and kinds ----------------------------------------------------------------------------


def test_the_default_pools_split_the_slots_with_lend_and_observed_last():
    assert default_pools(12) == {
        "work": Pool(reserved=6, max=12, priority=0),
        "external": Pool(reserved=0, max=6, priority=1),
        "lend": Pool(reserved=0, max=6, priority=2),
        "observed": Pool(reserved=0, max=12, priority=3),
    }
    assert default_pools(1)["work"].reserved == 0
    config = MachineConfig.model_validate({"ceiling": {"slots": 12}})
    assert config.pool_set().pools == default_pools(12)
    assert config.observed_pool == "observed"


def test_a_machine_without_an_observed_pool_observes_nothing():
    config = MachineConfig.model_validate(_machine())
    assert config.observed_pool is None and "observed" not in config.kinds


def test_the_config_refuses_a_lend_pool_that_reserves_or_a_kind_in_the_wrong_pool():
    with pytest.raises(ValueError, match="reserves nothing"):
        MachineConfig.model_validate(
            {
                "ceiling": {"slots": 4},
                "pools": {**default_pools(4), "lend": Pool(reserved=1, max=2)},
            }
        )
    with pytest.raises(ValueError, match="only lend leases go in lend pools"):
        MachineConfig.model_validate({"ceiling": {"slots": 4}, "kinds": {"work": "lend"}})
    with pytest.raises(ValueError, match="is the observed pool"):
        MachineConfig.model_validate(
            {"ceiling": {"slots": 4}, "kinds": {"work": "observed", "observed": "observed"}}
        )
    with pytest.raises(ValueError, match="not a lease kind"):
        MachineConfig.model_validate({"ceiling": {"slots": 4}, "kinds": {"service": "work"}})
    with pytest.raises(ValueError, match="reserved 3 is more than max 2"):
        MachineConfig.model_validate(_machine(pools={"work": {"reserved": 3, "max": 2}}))


def test_the_slots_cap_what_the_pools_may_use(tmp_path):
    config = machine_config(
        tmp_path,
        ceiling={"slots": 2},
        pools={"work": {"max": 8}},
        kinds={"work": "work"},
        lend={"pools": []},
    )
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    assert agent.policy().slots == {MACHINE: 2}
    assert agent.acquire(req("work", weight=2), user="ann")["status"] == "granted"
    assert agent.acquire(req("work", weight=1), user="ann")["status"] == "queued"


def test_the_ceiling_is_filled_in_from_the_machines_own_size():
    bare = MachineConfig.model_validate({"ceiling": {"slots": 2}})
    assert bare.limits(cpus=8, memory_bytes=16 << 30) == MachineLimits(
        slots=2, cpus=8.0, memory=f"{16 << 30}b", memory_bytes=16 << 30
    )
    capped = MachineConfig.model_validate({"ceiling": {"slots": 2, "cpus": 3, "memory": "4g"}})
    assert capped.limits(cpus=8, memory_bytes=16 << 30) == MachineLimits(
        slots=2, cpus=3.0, memory="4g", memory_bytes=4 << 30
    )


def test_a_pool_max_override_replaces_only_that_pools_max():
    config = MachineConfig.model_validate(_machine())
    policy = config.policy(closed=frozenset({"external"}), max_overrides={"external": 1})
    assert policy.pools["external"].max == 1 and policy.pools["work"].max == 4
    assert policy.pools["work"].reserved == 2
    assert policy.closed == frozenset({"external"})


def test_unknown_fields_are_refused():
    for removed in ("trust", "parent", "reserved"):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            MachineConfig.model_validate(_machine(**{removed: "x"}))


# --- standing weight ----------------------------------------------------------------------------


def test_standing_parses_from_toml_and_changes_only_what_is_held(tmp_path):
    path = tmp_path / "machine.toml"
    path.write_text(
        f"""
name = "buildbox"
admin_group = "operators"
ledger_dir = "{tmp_path}"
socket = "{tmp_path / "s.sock"}"

[ceiling]
slots = 4

[pools.work]
reserved = 2
max = 4

[pools.external]
max = 2
priority = 1

[kinds]
work = "work"
external = "external"

[lend]
pools = []

[[standing]]
name = "gate"
kind = "vm"
pool = "work"
weight = 2
cpus = 2.0
memory = "3g"
note = "neighbour"

[[standing]]
name = "db"
pool = "external"
weight = 1
"""
    )
    config = load(path)
    assert config.admin_group == "operators"
    assert [
        (s.name, s.kind, s.pool, s.weight, s.cpus, s.memory, s.note) for s in config.standing
    ] == [
        ("gate", "vm", "work", 2, 2.0, "3g", "neighbour"),
        ("db", "workload", "external", 1, None, None, None),
    ]
    assert config.standing_held() == {"work": 2, "external": 1}
    bare = MachineConfig.model_validate(_machine())
    assert config.pool_set() == bare.pool_set()
    policy = config.policy()
    assert policy.standing == {"work": 2, "external": 1}
    assert bare.policy().standing == {}
    assert (bare.policy().slots, bare.policy().pools) == (policy.slots, policy.pools)


def test_no_standing_is_the_same_as_an_empty_list():
    bare = MachineConfig.model_validate(_machine())
    explicit = MachineConfig.model_validate(_machine(standing=[]))
    assert bare.policy() == explicit.policy() and explicit.standing == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            {
                "standing": [
                    {"name": "gate", "pool": "work", "weight": 1},
                    {"name": "gate", "pool": "external", "weight": 1},
                ]
            },
            "duplicate name 'gate'",
        ),
        (
            {"standing": [{"name": "gate", "pool": "nope", "weight": 1}]},
            "pool 'nope' is not under pools",
        ),
        (
            {"standing": [{"name": "gate", "pool": "external", "weight": 3}]},
            "more than pool 'external''s max 2",
        ),
        (
            {
                "ceiling": {"slots": 6},
                "pools": {"work": {"max": 3}, "external": {"max": 3, "priority": 1}},
                "standing": [
                    {"name": "gate", "pool": "work", "weight": 2},
                    {"name": "other", "pool": "work", "weight": 2},
                ],
            },
            "pool 'work' holds weight 4, more than its max 3",
        ),
        (
            {
                "ceiling": {"slots": 3},
                "pools": {"work": {"max": 3}, "external": {"max": 3, "priority": 1}},
                "standing": [
                    {"name": "gate", "pool": "work", "weight": 2},
                    {"name": "db", "pool": "external", "weight": 2},
                ],
            },
            "more than the machine's 3 slots",
        ),
        (
            {"standing": [{"name": "gate", "pool": "work", "weight": 0}]},
            "greater than or equal to 1",
        ),
        (
            {"standing": [{"name": "gate", "pool": "work", "weight": 1, "memory": "8GB"}]},
            "byte count",
        ),
        (
            {"standing": [{"name": "gate", "pool": "work", "weight": 1, "cpus": 0}]},
            "greater than 0",
        ),
        (
            {"standing": [{"name": "gate", "pool": "work", "weight": 1, "bogus": True}]},
            "Extra inputs are not permitted",
        ),
    ],
    ids=[
        "duplicate",
        "unknown pool",
        "over its pool's max",
        "pool total over max",
        "over the slots",
        "zero weight",
        "bad memory",
        "zero cpus",
        "unknown field",
    ],
)
def test_standing_the_config_cannot_hold_is_refused(extra, message):
    with pytest.raises(ValueError, match=message):
        MachineConfig.model_validate(_machine(**extra))


def test_restarting_with_changed_standing_weight_uses_the_same_ledger(tmp_path):
    def build(weight: int) -> MachineConfig:
        return MachineConfig.model_validate(
            {
                "name": "buildbox",
                "ceiling": {"slots": 3},
                "ledger_dir": str(tmp_path),
                "socket": str(tmp_path / "s.sock"),
                "tick_s": 0.05,
                "pools": {"work": {"reserved": 1, "max": 3, "priority": 0}},
                "kinds": {"work": "work"},
                "lend": {"pools": [], "quiet_s": 0},
                "standing": [{"name": "gate", "pool": "work", "weight": weight, "memory": "1g"}],
            }
        )

    def boot(weight: int) -> MachineAgent:
        return MachineAgent(build(weight), facts=FACTS, load=FakeLoad(), clock=Clock())

    agent = boot(2)
    agent.tick()
    assert agent.acquire(req("work", lease_id="W"), user="ann")["status"] == "granted"
    assert agent.acquire(req("work", lease_id="X"), user="bob")["status"] == "queued"
    status = agent.status()
    assert status["standing"] == [
        {
            "name": "gate",
            "kind": "workload",
            "pool": "work",
            "weight": 2,
            "cpus": None,
            "memory_bytes": 1 << 30,
            "note": None,
        }
    ]
    work = status["pools"]["work"]
    assert (work["reserved"], work["held_weight"], work["running"]) == (1, 3, 1)
    assert (work["queued"], work["revoked"]) == (1, 0)
    assert agent.policy().standing == {"work": 2}
    agent.set_pool_max("work", 3)
    assert agent.policy().standing == {"work": 2}  # an override leaves standing weight alone

    # Lowering the weight on a new agent frees a slot; the existing lease stays.
    restarted = boot(1)
    assert restarted.acquire(req("work", lease_id="X"), user="bob")["status"] == "granted"
    assert [row["lease_id"] for row in restarted.leases()["leases"]] == ["W", "X"]

    # Raising it again leaves those leases in place and admits nothing further.
    tightened = boot(2)
    assert tightened.acquire(req("work", lease_id="Y"), user="cy")["status"] == "queued"
    assert [row["lease_id"] for row in tightened.leases()["leases"]] == ["W", "X"]

    # Deleting the ledger does not restore the slots the standing weight occupies.
    path = build(2).ledger_path
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    fresh = boot(2)
    assert fresh.leases()["leases"] == []
    assert fresh.acquire(req("work", lease_id="A"), user="ann")["status"] == "granted"
    assert fresh.acquire(req("work", lease_id="B"), user="bob")["status"] == "queued"

    with sqlite3.connect(fresh.config.ledger_path) as conn:
        blob = " ".join(
            str(value)
            for table in ("leases", "queue", "turns", "session_turns", "submits", "meta")
            for row in conn.execute(f"SELECT * FROM {table}")
            for value in row
        )
    assert "gate" not in blob  # configuration, never written to the ledger


# --- the config file ----------------------------------------------------------------------------


def test_a_missing_config_file_is_an_error_naming_it(tmp_path):
    with pytest.raises(FileNotFoundError, match=str(tmp_path / "absent.toml")):
        load(tmp_path / "absent.toml")


def test_kinds_written_without_observed_keep_the_observed_pool_observed(tmp_path):
    config = machine_config(
        tmp_path, kinds={"work": "work", "external": "external", "lend": "lend"}
    )
    assert config.observed_pool == "observed"
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad(), clock=Clock())
    answer = agent.acquire(req("external", pool="observed"), user="ann")
    assert answer["status"] == "refused"


def test_a_pool_max_above_the_slots_is_clamped_to_them(tmp_path):
    config = machine_config(
        tmp_path,
        pools={
            "work": {"max": 50},
            "external": {"max": 1},
            "lend": {"max": 1},
            "observed": {"max": 2, "priority": 3},
        },
    )
    assert config.pool_set().pools["work"].max == 2
    assert config.policy().pools["work"].max == 2


def test_a_pool_max_below_a_waiting_weight_is_refused(tmp_path):
    """It could never be admitted, and with no backfill it would hold up the pool's line."""
    agent = MachineAgent(machine_config(tmp_path), facts=FACTS, load=FakeLoad(), clock=Clock())
    agent.acquire(req("work", weight=2, lease_id="A"), user="ann")
    assert agent.acquire(req("work", weight=2, lease_id="B"), user="bob")["status"] == "queued"
    with pytest.raises(ValueError, match="weight 2 waiting"):
        agent.set_pool_max("work", 1)
