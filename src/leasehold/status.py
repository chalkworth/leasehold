"""What `GET /v1/status` answers: the machine, its pools, its load and its leases, as one JSON
object. A pure function of the agent's state, so it can be tested without a daemon.

`schema` versions the shape: it is bumped only when a field is removed or changes meaning (adding
a field is not a change). `docs/reference.md` lists every field.
"""

from __future__ import annotations

from leasehold.admission import AdmissionPolicy
from leasehold.machine import MachineConfig, MachineLimits
from leasehold.units import memory_to_bytes

#: The version of the shape.
SCHEMA = 2


def public(row: dict) -> dict:
    """A ledger row as the API shows it."""
    out = {
        "lease_id": row["item_id"],
        "kind": row["kind"],
        "pool": row["pool"],
        "weight": row["weight"],
        "user": row["user"],
        "session": row["session"],
        "labels": row["labels"],
        "ttl_s": row["ttl_s"],
        "expires_at": row["expires_at"],
    }
    if "acquired_at" in row:
        out |= {
            "acquired_at": row["acquired_at"],
            "grant": row["grant_no"],
            "revoked": row["revoked_at"] is not None,
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
            "revoke_deadline": row["revoke_deadline"],
        }
    else:
        out["queued_at"] = row["queued_at"]
    return out


def standing(config: MachineConfig) -> list[dict]:
    """`[[standing]]` as status reports it: `memory` as `memory_bytes`."""
    return [
        {
            "name": item.name,
            "kind": item.kind,
            "pool": item.pool,
            "weight": item.weight,
            "cpus": item.cpus,
            "memory_bytes": None if item.memory is None else memory_to_bytes(item.memory),
            "note": item.note,
        }
        for item in config.standing
    ]


def observing(config: MachineConfig, leases: list[dict]) -> list[dict]:
    """Each `[[observe]]` rule and how many processes it counts now."""
    counts: dict[str, int] = {}
    for lease in leases:
        if lease.get("kind") == "observed":
            rule = (lease.get("labels") or {}).get("rule")
            counts[rule] = counts.get(rule, 0) + 1
    return [
        {
            "name": rule.name,
            "weight": rule.weight,
            "pool": rule.pool or config.observed_pool,
            "max_count": rule.max_count,
            "running": counts.get(rule.name, 0),
        }
        for rule in config.observe
    ]


def pools(
    config: MachineConfig, policy: AdmissionPolicy, leases: list[dict], queue: list[dict]
) -> dict[str, dict]:
    """Per pool: its shape (with any override applied) and what it holds and has waiting.
    `held_weight` includes standing weight; `running`, `revoked` and `queued` count leases."""
    held_standing = config.standing_held()
    lend = set(config.lend.pools)
    out = {}
    for name, pool in config.pool_set().pools.items():
        held = [x for x in leases if x["pool"] == name]
        shaped = policy.pools[name]
        held_weight = sum(x["weight"] for x in held) + held_standing.get(name, 0)
        out[name] = {
            "reserved": pool.reserved,
            "max": shaped.max,
            "priority": pool.priority,
            "preemptible": name in lend,
            "observed": name == config.observed_pool,
            "closed": name in policy.closed,
            "held_weight": held_weight,
            "overcommitted": max(0, held_weight - shaped.max),
            "running": len(held),
            "revoked": sum(1 for x in held if x["revoked"]),
            "queued": sum(1 for x in queue if x["pool"] == name),
        }
    return out


def report(
    *,
    config: MachineConfig,
    limits: MachineLimits,
    policy: AdmissionPolicy,
    facts: dict,
    load: dict | None,
    lend: dict,
    leases: list[dict],
    guard: dict | None = None,
    queue: list[dict],
    now: float,
    version: str,
) -> dict:
    """The whole status object. `leases` and `queue` are already `public`."""
    by_pool = pools(config, policy, leases, queue)
    # Leases left in a pool the config no longer has still hold their slots.
    stray = sum(lease["weight"] for lease in leases if lease["pool"] not in by_pool)
    held = sum(p["held_weight"] for p in by_pool.values()) + stray
    return {
        "schema": SCHEMA,
        "version": version,
        "time": now,
        "machine": {"name": config.name, "labels": config.labels},
        "mode": config.mode,
        "facts": facts,
        "ceiling": {
            "slots": limits.slots,
            "cpus": limits.cpus,
            "memory": limits.memory,
            "memory_bytes": limits.memory_bytes,
        },
        "slots": limits.slots,
        "held": held,
        "overcommitted": max(0, held - limits.slots),
        "pools": by_pool,
        "load": load,
        "lend": lend,
        "guard": guard,
        "contain": config.contain.model_dump(),
        "standing": standing(config),
        "observe": observing(config, leases),
        "leases": leases,
        "queue": queue,
    }
