"""What `GET /v1/status` answers: the machine, its pools, its load and its leases, as one JSON
object. A pure function of the agent's state, so it can be tested without a daemon.

`schema` versions the shape: it is bumped only when a field is removed or changes meaning (adding
a field is not a change). `docs/reference.md` lists every field.
"""

from __future__ import annotations

from leasehold.admission import AdmissionPolicy
from leasehold.machine import MachineConfig, MachineLimits, MemoryBudget
from leasehold.model import MACHINE
from leasehold.units import memory_to_bytes

#: The version of the shape.
SCHEMA = 2


def public(row: dict, policy: AdmissionPolicy | None = None) -> dict:
    """A ledger row as the API shows it. A lease's `weight` is the size it holds now (granted,
    or moved to since by an elastic lease), and its `target` the size it is asked to move to
    (its `weight` unless it is `elastic`). `giving_back` is how far the target is below the
    weight: slots the holder is asked to give back, and will once its jobs finish. A waiting
    item's `weight` is the least it needs.
    `min_weight` and `max_weight` are the lease's range (both `weight` for a row from before
    ranges). A waiting item's `max_weight` is the most it asked for, cut as admission would cut
    it now under `policy` (`AdmissionPolicy.most`) when one is given."""
    weight = row["weight"]
    most = _or(row.get("max_weight"), weight)
    if policy is not None and "acquired_at" not in row and row["pool"] in policy.pools:
        most = policy.most(row["pool"], weight, most, row.get("memory"), row.get("memory_per_slot"))
    out = {
        "lease_id": row["item_id"],
        "kind": row["kind"],
        "pool": row["pool"],
        "weight": weight,
        "min_weight": _or(row.get("min_weight"), weight),
        "max_weight": most,
        "user": row["user"],
        "session": row["session"],
        "labels": row["labels"],
        "ttl_s": row["ttl_s"],
        "expires_at": row["expires_at"],
        "memory_bytes": row.get("memory"),
        "memory_per_slot_bytes": row.get("memory_per_slot"),
    }
    out["elastic"] = bool(row.get("elastic"))
    if "acquired_at" in row:
        out |= {
            "target": _or(row.get("target"), weight),
            "giving_back": max(0, weight - _or(row.get("target"), weight)),
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


def _or(value: int | None, default: int) -> int:
    return default if value is None else value


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


def counted(policy: AdmissionPolicy, item: dict) -> int:
    """The memory, in bytes, admission counts for a lease or queued item (0 without a budget)."""
    shaped = policy.pools.get(item["pool"])
    host = shaped.host if shaped is not None else MACHINE
    return policy.counted(
        host, item["weight"], item.get("memory_bytes"), item.get("memory_per_slot_bytes")
    )


def with_counted(policy: AdmissionPolicy, items: list[dict]) -> list[dict]:
    """`items` (already `public`), each with the memory admission counts for it."""
    return [x | {"memory_counted_bytes": counted(policy, x)} for x in items]


def memory(
    budget: MemoryBudget | None,
    policy: AdmissionPolicy,
    leases: list[dict],
    machine_bytes: int,
    limit: dict | None = None,
) -> dict:
    """The memory budget as status reports it: `counted` false (and the budget's fields None)
    when the config has no `[memory]` table. `limit_bytes` and `limit_cgroup` are the cgroup
    memory limit the daemon's work was held to when it started, None without one."""
    bound = {
        "limit_bytes": None if limit is None else limit["limit_bytes"],
        "limit_cgroup": None if limit is None else limit["cgroup"],
    }
    if budget is None:
        return {
            "counted": False,
            "machine_bytes": machine_bytes,
            **bound,
            "budget_bytes": None,
            "kept_bytes": None,
            "assumed_per_slot_bytes": None,
            "held_bytes": None,
            "free_now_bytes": None,
        }
    standing = sum(policy.standing_memory.values())
    return {
        "counted": True,
        "machine_bytes": machine_bytes,
        **bound,
        "budget_bytes": budget.budget,
        "kept_bytes": budget.kept,
        "assumed_per_slot_bytes": budget.assumed_per_slot,
        "held_bytes": standing + sum(counted(policy, x) for x in leases),
        "free_now_bytes": policy.memory_free.get(MACHINE),
    }


def pools(
    config: MachineConfig, policy: AdmissionPolicy, leases: list[dict], queue: list[dict]
) -> dict[str, dict]:
    """Per pool: its shape (with any override applied) and what it holds and has waiting.
    `held_weight` includes standing weight; `running`, `revoked` and `queued` count leases;
    `memory_counted_bytes` is the memory admission counts there (standing memory included)."""
    held_standing = config.standing_held()
    standing_memory = policy.standing_memory
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
            "headroom": shaped.headroom,
            "preemptible": name in lend,
            "observed": name == config.observed_pool,
            "closed": name in policy.closed,
            "held_weight": held_weight,
            "overcommitted": max(0, held_weight - shaped.max),
            "running": len(held),
            "revoked": sum(1 for x in held if x["revoked"]),
            "queued": sum(1 for x in queue if x["pool"] == name),
            "memory_counted_bytes": sum(counted(policy, x) for x in held)
            + (standing_memory.get(name, 0) if policy.memory else 0),
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
    budget: MemoryBudget | None = None,
    memory_limit: dict | None = None,
    queue: list[dict],
    maintenance: dict | None = None,
    memory_net: dict | None = None,
    now: float,
    version: str,
) -> dict:
    """The whole status object. `leases` and `queue` are already `public`; `maintenance` is
    the maintenance lease waiters are told about (`leasehold.maintenance.describe`), or None;
    `memory_net` the memory net's own status (`leasehold.net.MemoryNet.status`), or None when
    it is off."""
    leases = with_counted(policy, leases)
    queue = with_counted(policy, queue)
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
        "memory": memory(
            budget,
            policy,
            leases,
            facts.get("memory_bytes") or limits.memory_bytes,
            memory_limit,
        ),
        "contain": config.contain.model_dump(),
        "standing": standing(config),
        "observe": observing(config, leases),
        "leases": leases,
        "queue": queue,
        "maintenance": maintenance,
        "memory_net": memory_net,
    }
