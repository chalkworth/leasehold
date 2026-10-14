"""Pools: how a machine's slots are split, and the limits each pool sets per user.

A machine has some number of **slots**, an abstract unit of capacity (often about one CPU's
worth). Work holds a **weight** in slots while it runs. The slots are split into named
**pools**:

* `reserved`: slots the pool is guaranteed. Another pool never takes them, even while they
  sit idle.
* `max`: the most the pool may hold at once.
* `priority`: pools are served in this order, lower first.
* per-user limits: how many items one user may run (`per_user_max_running`) and have waiting
  (`per_user_max_queued`) in the pool, and how many they may submit per minute
  (`create_rate_per_min`).

`PoolSet` is one machine's slots and pools, validated together. Pure: nothing here reads or
writes a file.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

#: The name the admission decision files a single machine's pools under. Admission can split
#: slots between several such groups (`leasehold.admission.PoolPolicy.host`); a `PoolSet` is
#: always one.
MACHINE = "machine"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", defer_build=True)


class Pool(_Strict):
    reserved: int = Field(0, ge=0)
    max: int = Field(ge=1)
    #: How long an item may wait in this pool before its client gives up on it; leasehold
    #: stores it for clients and never times an item out itself. None waits forever.
    queue_timeout_s: int | None = Field(None, ge=1)
    priority: int = Field(0, ge=0)
    #: How many items one user may have admitted in this pool at once; None is no per-user
    #: limit beyond `max`.
    per_user_max_running: int | None = Field(None, ge=1)
    #: How many items one user may have waiting in this pool; a submit past it is refused
    #: (`leasehold.quota`, `quota_exceeded`) rather than queued. None is unlimited.
    per_user_max_queued: int | None = Field(None, ge=0)
    #: How many items one user may submit into this pool per minute (a sliding window the
    #: ledger keeps); None is unlimited.
    create_rate_per_min: int | None = Field(None, ge=1)


class PoolSet(_Strict):
    """A machine's `slots` and its `pools`. Valid when every pool's reservation fits its own
    `max` and all reservations together fit the slots."""

    slots: int = Field(ge=0)
    pools: dict[Name, Pool]

    @model_validator(mode="after")
    def _consistent(self) -> PoolSet:
        errors: list[str] = []
        if not self.pools:
            errors.append("pools: at least one pool is needed")
        for name, pool in self.pools.items():
            if pool.reserved > pool.max:
                errors.append(f"pools.{name}: reserved {pool.reserved} is more than max {pool.max}")
        total = sum(pool.reserved for pool in self.pools.values())
        if total > self.slots:
            errors.append(f"pools reserve {total} slots, more than the machine's {self.slots}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def in_order(self) -> list[str]:
        """Pool names in the order they are served: priority, then name."""
        return sorted(self.pools, key=lambda name: (self.pools[name].priority, name))
