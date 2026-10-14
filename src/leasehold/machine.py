"""A machine's own configuration: who it is, what it may give at most, and how it splits that.

One small TOML file (`leasehold.paths` says where it is looked for in each mode), or none at all:
a daemon without one gets `MachineConfig.defaults`. `docs/reference.md` has the format.
Everything here is local to the machine: the ceiling is its self-cap, and nothing a client asks
for may exceed it. `resolve` fills in the paths the file leaves to the mode.

The pools are a `leasehold.model.PoolSet`. A machine that names no pools gets `default_pools`:
`work`, `external`, `lend` and `observed`.

`[[standing]]` names capacity that long-lived work outside leasehold already occupies (a VM, a
service). Its weight is counted as held (`AdmissionPolicy.standing`) without being a lease.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leasehold.admission import AdmissionPolicy
from leasehold.commands import CommandSettings
from leasehold.kinds import KINDS, PREEMPTIBLE
from leasehold.ledger import FILENAME
from leasehold.model import Name, Pool, PoolSet
from leasehold.observe import ObserveRule
from leasehold.paths import (
    SOCKET_MODES,
    Mode,
    check_mode,
    default_ledger_dir,
    default_socket,
)
from leasehold.units import memory_to_bytes

_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _size(value: str | None) -> str | None:
    if value is not None:
        memory_to_bytes(value)
    return value


class MachineCeiling(_Strict):
    """The self-cap. `slots` is what the pools share; `cpus` and `memory` (a size such as
    `56g`) are the most any one lease may be given, by default the whole machine."""

    slots: int = Field(ge=1)
    cpus: float | None = Field(None, gt=0)
    memory: str | None = None

    _memory = field_validator("memory")(_size)


class LendSettings(_Strict):
    """When `lend` leases are granted and when they are revoked (see `leasehold.lend`)."""

    #: The pools only `lend` leases go in (and `lend` leases go nowhere else).
    pools: list[Name] = Field(default_factory=lambda: ["lend"])
    #: How long the machine must have been idle before a lend lease is granted.
    quiet_s: float = Field(600.0, ge=0)
    #: Idle means CPU and memory pressure (PSI `some avg10`, a percentage) at most these.
    cpu_pressure_max: float = Field(10.0, ge=0)
    memory_pressure_max: float = Field(5.0, ge=0)
    #: Without PSI: idle means the 1-minute load average per logical CPU at most this.
    load_per_cpu_max: float = Field(0.25, ge=0)
    #: Lend only inside this local-time window, `HH:MM-HH:MM` (may wrap midnight).
    window: str | None = None
    #: How long a revoked lease keeps its weight while its holder stops it.
    revoke_grace_s: float = Field(30.0, ge=0)
    #: Measure pressure as the highest over these cgroups (paths under the cgroup root, such as
    #: `work.slice`) instead of the whole machine: lent work's own pressure then never revokes
    #: it. Empty: the machine's `/proc/pressure`.
    pressure_cgroups: list[str] = Field(default_factory=list)

    @field_validator("window")
    @classmethod
    def _window(cls, value: str | None) -> str | None:
        if value is not None and not _WINDOW.match(value):
            raise ValueError(f"window {value!r} is not HH:MM-HH:MM")
        return value

    def window_minutes(self) -> tuple[int, int] | None:
        """(start, end) minutes of the day, or None for always."""
        if self.window is None:
            return None
        m = _WINDOW.match(self.window)
        assert m is not None
        a, b, c, d = (int(g) for g in m.groups())
        return a * 60 + b, c * 60 + d


class LoadSettings(_Strict):
    #: The systemd slices whose CPU and memory use `status` reports (`work.slice`, ...).
    slices: list[str] = Field(default_factory=list)


class JobSettings(_Strict):
    """How many parallel jobs a lease's weight means, for `hold` to size its command to."""

    #: Jobs per slot of weight: a weight-2 lease runs `2 * per_slot` jobs (at least 1).
    per_slot: float = Field(1.0, gt=0)

    def for_weight(self, weight: int) -> int:
        return max(1, int(weight * self.per_slot))


class GuardSettings(_Strict):
    """When new work is held back for memory (see `leasehold.guard`)."""

    #: None: on for a user's own daemon, off for the machine's.
    enabled: bool | None = None
    #: Memory PSI `some avg10` (a percentage) above which the guard goes on...
    memory_pressure_on: float = Field(20.0, ge=0)
    #: ...and at most which, for `settle_s`, it goes off again.
    memory_pressure_off: float = Field(5.0, ge=0)
    #: Available memory (a size such as `2g`) under which it goes on. None: 5% of the machine's
    #: memory, at least 512 MiB.
    min_available: str | None = None
    settle_s: float = Field(30.0, ge=0)

    _min = field_validator("min_available")(_size)

    @model_validator(mode="after")
    def _ordered(self) -> GuardSettings:
        if self.memory_pressure_off > self.memory_pressure_on:
            raise ValueError("guard: memory_pressure_off is above memory_pressure_on")
        return self

    def floor(self, memory_bytes: int) -> int:
        """`min_available` in bytes, for a machine of `memory_bytes`."""
        if self.min_available is not None:
            return memory_to_bytes(self.min_available)
        return max(512 * 2**20, memory_bytes // 20)


class ContainSettings(_Strict):
    """Whether `hold`, the shims and `run` put their commands in systemd user scopes sized to
    the lease (`leasehold.contain`)."""

    #: `auto`: wherever the caller's systemd user manager can take them; `off`: never.
    mode: Literal["auto", "off"] = "auto"
    #: Also cap a scope at `weight` CPUs (`CPUQuota`), not only weigh it when CPUs are short.
    hard_cpu: bool = False


class Standing(_Strict):
    """Capacity a named long-lived workload outside leasehold occupies permanently.

    Configuration, not a lease: nothing here is written to the ledger. `weight` is what
    admission counts as already held in `pool`. `cpus`, `memory` and `note` describe the
    workload; only `weight` changes what fits.
    """

    name: str
    kind: str = "workload"
    pool: str
    weight: int = Field(ge=1)
    cpus: float | None = Field(None, gt=0)
    memory: str | None = None
    note: str | None = None

    _memory = field_validator("memory")(_size)


@dataclass(frozen=True)
class MachineLimits:
    """The ceiling with the machine's own size filled in where the config leaves it out."""

    slots: int
    cpus: float
    memory: str
    memory_bytes: int


class MachineConfig(_Strict):
    name: str = Field(default_factory=lambda: os.uname().nodename)
    labels: dict[str, str] = Field(default_factory=dict)
    ceiling: MachineCeiling
    #: Which daemon this config is for, `system` or `user`; `serve --system|--user` decides when
    #: it is unset (default `user`).
    mode: Mode | None = None
    #: Where the ledger lives; None: the mode's (`leasehold.paths`).
    ledger_dir: Path | None = None
    #: The daemon's socket; None: the mode's.
    socket: Path | None = None
    #: The group the socket is given (mode `socket_mode`); None leaves the daemon's own.
    socket_group: str | None = None
    #: The socket's file mode; None: `0660` for the system daemon, `0600` for a user's.
    socket_mode: str | None = None
    #: Members of this group may switch lending and change pool limits (`leasehold lend`,
    #: `leasehold pool`), as root and the daemon's own user always may. None: only those two.
    admin_group: str | None = None
    #: How often the daemon reclaims, measures and decides on lending.
    tick_s: float = Field(2.0, gt=0)
    #: A lease's TTL: a holder that does not renew within it loses the lease.
    lease_ttl_s: float = Field(15.0, gt=0)
    #: The pools, by name. None: `default_pools(ceiling.slots)`.
    pools: dict[Name, Pool] | None = None
    #: Which pool each kind goes to when a request names none.
    kinds: dict[str, Name] = Field(
        default_factory=lambda: {
            "work": "work",
            "external": "external",
            "lend": "lend",
            "observed": "observed",
        }
    )
    lend: LendSettings = Field(default_factory=LendSettings)
    load: LoadSettings = Field(default_factory=LoadSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    #: Long-lived work outside leasehold that already occupies capacity.
    standing: list[Standing] = Field(default_factory=list)
    #: Known programs started outside leasehold, charged as they run (`leasehold.observe`).
    observe: list[ObserveRule] = Field(default_factory=list)
    #: Which commands agents and PATH shims take a lease for (`leasehold.commands`).
    commands: CommandSettings = Field(default_factory=CommandSettings)
    #: How commands leasehold starts are contained (`leasehold.contain`).
    contain: ContainSettings = Field(default_factory=ContainSettings)
    #: When new work is held back for memory (`leasehold.guard`).
    guard: GuardSettings = Field(default_factory=GuardSettings)

    @field_validator("socket_mode")
    @classmethod
    def _mode(cls, value: str | None) -> str | None:
        if value is not None:
            int(value, 8)
        return value

    @classmethod
    def defaults(cls, *, slots: int) -> MachineConfig:
        """The config a daemon runs on when it has no file: `slots` slots, the default pools."""
        return cls(ceiling=MachineCeiling(slots=max(1, slots)))

    @model_validator(mode="after")
    def _consistent(self) -> MachineConfig:
        pool_set = self.pool_set()  # raises on bad pools
        pools = pool_set.pools
        if "observed" not in self.kinds and "observed" in pools:
            # A [kinds] that leaves observed out still has the pool of that name observed, so
            # no client may ask for it.
            self.kinds = self.kinds | {"observed": "observed"}
        if "observed" in self.kinds and self.kinds["observed"] not in pools:
            # Observing is optional: without its pool, nothing is observed.
            self.kinds = {k: v for k, v in self.kinds.items() if k != "observed"}
        pools = self.pool_set().pools  # clamped now that the observed pool is known
        errors = []
        for kind, pool in self.kinds.items():
            if kind not in KINDS:
                errors.append(f"kinds.{kind}: not a lease kind ({', '.join(KINDS)})")
            elif pool not in pools:
                errors.append(f"kinds.{kind}: pool {pool!r} is not under pools")
            elif (kind in PREEMPTIBLE) != (pool in self.lend.pools):
                errors.append(
                    f"kinds.{kind}: pool {pool!r} "
                    + ("is not" if kind in PREEMPTIBLE else "is")
                    + " a lend pool; only lend leases go in lend pools"
                )
        observed = self.observed_pool
        if observed is not None:
            for kind, pool in self.kinds.items():
                if kind != "observed" and pool == observed:
                    errors.append(
                        f"kinds.{kind}: pool {pool!r} is the observed pool, which only "
                        "observed processes go in"
                    )
        for pool in self.lend.pools:
            if pool not in pools:
                errors.append(f"lend.pools: {pool!r} is not under pools")
            elif pools[pool].reserved:
                errors.append(f"pools.{pool}: a lend pool reserves nothing")
        errors += self._standing_errors(pools)
        errors += self._observe_errors()
        for n, rule in enumerate(self.commands.rule):
            if rule.pool is not None and rule.pool not in pools:
                errors.append(f"commands.rule[{n}]: pool {rule.pool!r} is not under pools")
            elif rule.pool == self.observed_pool:
                errors.append(f"commands.rule[{n}]: only observed programs go in {rule.pool!r}")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def _observe_errors(self) -> list[str]:
        errors = []
        names = [rule.name for rule in self.observe]
        for name in sorted({n for n in names if names.count(n) > 1}):
            errors.append(f"observe: {name!r} is named twice")
        observed = self.observed_pool
        for rule in self.observe:
            if observed is None:
                errors.append(f"observe.{rule.name}: there is no observed pool to charge it to")
            elif rule.pool not in (None, observed):
                errors.append(
                    f"observe.{rule.name}: pool {rule.pool!r} is not the observed pool "
                    f"{observed!r}; observed programs are charged there"
                )
        return errors

    def _standing_errors(self, pools: dict[str, Pool]) -> list[str]:
        errors = []
        seen: set[str] = set()
        held_by_pool: dict[str, int] = {}
        for item in self.standing:
            if item.name in seen:
                errors.append(f"standing: duplicate name {item.name!r}")
            seen.add(item.name)
            pool = pools.get(item.pool)
            if pool is None:
                errors.append(f"standing.{item.name}: pool {item.pool!r} is not under pools")
            elif item.weight > pool.max:
                errors.append(
                    f"standing.{item.name}: weight {item.weight} is more than pool "
                    f"{item.pool!r}'s max {pool.max}"
                )
            held_by_pool[item.pool] = held_by_pool.get(item.pool, 0) + item.weight
        for pool, total in held_by_pool.items():
            if pool in pools and total > pools[pool].max:
                errors.append(
                    f"standing: pool {pool!r} holds weight {total}, more than its max "
                    f"{pools[pool].max}"
                )
        held_total = sum(item.weight for item in self.standing)
        if held_total > self.ceiling.slots:
            errors.append(
                f"standing: weight {held_total} is more than the machine's "
                f"{self.ceiling.slots} slots"
            )
        elif not errors:
            # Each pool's reservation must stay whole beside the standing work: a pool's
            # standing weight counts towards its own reservation, never another's.
            owed = sum(
                max(0, pool.reserved - held_by_pool.get(name, 0)) for name, pool in pools.items()
            )
            if held_total + owed > self.ceiling.slots:
                errors.append(
                    f"standing: weight {held_total} and the {owed} slots reserved for pools "
                    f"beyond it are more than the machine's {self.ceiling.slots} slots"
                )
        return errors

    @property
    def observed_pool(self) -> str | None:
        """The pool observed processes are charged to, or None when nothing is observed."""
        return self.kinds.get("observed")

    def pool_set(self) -> PoolSet:
        """The machine's slots and pools, each pool's `max` clamped to the most it could ever
        hold: the slots less the other pools' reservations, which are never lent to it (never
        below its own reservation). The observed pool, which nothing is ever admitted to, is
        clamped to the slots only."""
        slots = self.ceiling.slots
        pools = self.pools if self.pools is not None else default_pools(slots)
        reserved = sum(pool.reserved for pool in pools.values())
        clamped = {}
        for name, pool in pools.items():
            top = slots if name == self.observed_pool else slots - (reserved - pool.reserved)
            top = max(top, pool.reserved)
            clamped[name] = pool if pool.max <= top else pool.model_copy(update={"max": top})
        return PoolSet(slots=slots, pools=clamped)

    def limits(self, *, cpus: float, memory_bytes: int) -> MachineLimits:
        """The ceiling, with `cpus` and `memory_bytes` (the machine's own) where it sets none."""
        memory = self.ceiling.memory or f"{memory_bytes}b"
        return MachineLimits(
            slots=self.ceiling.slots,
            cpus=self.ceiling.cpus if self.ceiling.cpus is not None else float(cpus),
            memory=memory,
            memory_bytes=memory_to_bytes(memory),
        )

    def standing_held(self) -> dict[str, int]:
        """Pool name to the total weight `[[standing]]` holds there."""
        totals: dict[str, int] = {}
        for item in self.standing:
            totals[item.pool] = totals.get(item.pool, 0) + item.weight
        return totals

    @property
    def guarded(self) -> bool:
        """Whether the memory guard runs: as configured, else for a user's own daemon."""
        enabled = self.guard.enabled
        return (self.mode or "user") == "user" if enabled is None else enabled

    def policy(
        self,
        *,
        closed: frozenset[str] = frozenset(),
        max_overrides: dict[str, int] | None = None,
        capped: bool = False,
    ) -> AdmissionPolicy:
        """The admission policy: the pools, with `closed` shut, each pool in `max_overrides`
        given that `max` instead of its configured one, `[[standing]]` weight held, and with
        `capped` every pool held to its reservation."""
        pool_set = self.pool_set()
        if max_overrides:
            pools = {
                name: pool.model_copy(update={"max": max_overrides.get(name, pool.max)})
                for name, pool in pool_set.pools.items()
            }
            pool_set = PoolSet.model_construct(slots=pool_set.slots, pools=pools)
        return AdmissionPolicy.from_pools(
            pool_set,
            closed=closed,
            standing=self.standing_held(),
            capped=frozenset(pool_set.pools) if capped else frozenset(),
        )

    @property
    def ledger_path(self) -> Path:
        if self.ledger_dir is None:
            raise ValueError("ledger_dir is unset: resolve() the config for a mode first")
        return self.ledger_dir / FILENAME


def default_pools(slots: int) -> dict[str, Pool]:
    """The neutral split of `slots` for a machine with nothing else on it:

    * `work`, served first, reserves half (at least 1 once there are 2) and may use all;
    * `external` (CI runners, test gates) reserves nothing and may use what `work` leaves;
    * `lend`, served after them, reserves nothing, may use what `work` leaves, and is the only
      pool ever preempted;
    * `observed`, served last, holds only programs seen running that never asked, and may hold
      the whole machine: it is never asked, so it is never refused."""
    reserved = max(1, slots // 2) if slots >= 2 else 0
    rest = max(1, slots - reserved)
    return {
        "work": Pool(reserved=reserved, max=slots, priority=0),
        "external": Pool(reserved=0, max=rest, priority=1),
        "lend": Pool(reserved=0, max=rest, priority=2),
        "observed": Pool(reserved=0, max=max(1, slots), priority=3),
    }


def resolve(config: MachineConfig, mode: Mode | None, env: Mapping[str, str]) -> MachineConfig:
    """`config` with its mode chosen (`mode`, else the config's, else `user`) and every path it
    leaves out filled in for that mode. Refuses paths that belong to the other mode."""
    chosen: Mode = mode or config.mode or "user"
    if mode is not None and config.mode is not None and mode != config.mode:
        raise ValueError(f"the config is for the {config.mode} daemon, not --{mode}")
    socket = Path(config.socket) if config.socket else default_socket(chosen, env)
    ledger_dir = Path(config.ledger_dir) if config.ledger_dir else default_ledger_dir(chosen, env)
    check_mode(chosen, socket, ledger_dir, env)
    return config.model_copy(
        update={
            "mode": chosen,
            "socket": socket,
            "ledger_dir": ledger_dir,
            "socket_mode": config.socket_mode or SOCKET_MODES[chosen],
        }
    )


def load(path: Path) -> MachineConfig:
    """The config at `path`. A missing file is an error naming it."""
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"no machine config at {path}") from None
    return MachineConfig.model_validate(raw)
