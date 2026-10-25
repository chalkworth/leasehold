"""The machine agent's core, free of any transport: what `leasehold serve` answers with.

It holds the machine's configuration, its facts, the ledger and the lend gate, and turns requests
into ledger calls. Every lease it grants is a TTL lease (`Ledger.renew` keeps it alive), so a
holder is whoever renews it, wherever it runs, and a restarted daemon finds its leases still in
the ledger. A periodic `tick` measures the load, decides on lending, revokes `lend` leases when
the machine is no longer idle, and admits whatever waits and now fits.

It answers whether, where and when work may run; it runs nothing itself. Who may make a request
is the transport's question (`leasehold.access`); this module only checks that a lease belongs to
the user who names it.
"""

from __future__ import annotations

import os
import pwd
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leasehold import (
    __version__,
    contain,
    estimate,
    maintenance,
    observe,
    outcomes,
    proctable,
    status,
)
from leasehold import net as memory_net
from leasehold.access import Forbidden
from leasehold.admission import AdmissionPolicy, alone
from leasehold.events import EventLog, EventType, Snapshot, changes
from leasehold.facts import probe
from leasehold.guard import Guard, Reading
from leasehold.kinds import GRANTED, PREEMPTIBLE, WHOLE, LeaseKind
from leasehold.ledger import Admitted, Holder, Ledger, LedgerItem, Queued
from leasehold.lend import LendGate, Observation, Verdict
from leasehold.load import LoadReader, available, some_avg10
from leasehold.machine import MachineConfig, lease_memory_limit
from leasehold.net import MemoryNet
from leasehold.proctable import Proc
from leasehold.units import declared_to_bytes, memory_to_bytes

#: The ledger scope observed processes' leases are filed under; no client may name it.
OBSERVED_SCOPE = "leasehold.observed"

#: The ledger scope every lease this agent grants is filed under.
SCOPE = "leasehold"
#: What a lease id and a session name may be made of.
ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
_ID = re.compile(ID_PATTERN)
_LENDING_OFF = "lending switched off"
#: How long a learned figure is kept before it is worked out again from the ledger, in seconds,
#: unless an outcome for its rule and repository is kept sooner.
LEARNED_TTL_S = 300.0

__all__ = ["SCOPE", "AcquireRequest", "Conflict", "Forbidden", "MachineAgent"]


class Conflict(RuntimeError):
    """A request that cannot be done now, though it may be later: an elastic lease asked to
    grow past what is free, or a lease that is no longer held."""


class AcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: LeaseKind
    pool: str | None = None
    #: The least weight the work needs: what it waits in line for.
    weight: int = Field(1, ge=1)
    #: The most it can use (at least `weight`; None: `weight`). The lease is granted a size
    #: between the two when it is admitted (`leasehold.admission.grants`) and keeps it.
    max_weight: int | None = Field(None, ge=1)
    labels: dict[str, str] = Field(default_factory=dict)
    #: The caller's own id for the lease, so asking again is idempotent; one is made if absent.
    lease_id: str | None = None
    #: Which of the caller's sessions the lease is for: one user's sessions take turns. None
    #: falls back to `labels["session"]`, then to the user's shared session.
    session: str | None = None
    #: How long to wait for the lease before answering `queued` (0: answer at once).
    wait_s: float = Field(0.0, ge=0, le=300)
    #: The lease's TTL; the machine's `lease_ttl_s` by default.
    ttl_s: float | None = Field(None, gt=0, le=3600)
    #: The memory the work will use, a size such as `4g`: counted against the machine's memory
    #: budget when it has one. None: the machine assumes some per slot of weight.
    memory: str | None = None
    #: The memory each slot granted will use, a size such as `2g`, instead of `memory`: counted
    #: at the size the lease is granted.
    memory_per_slot: str | None = None
    #: An elastic lease (only for a range): its size may move between `weight` and
    #: `max_weight` after it is granted. The holder takes slots when its target rises and gives
    #: them back when it falls (`POST /v1/resize`, `leasehold.elastic`).
    elastic: bool = False
    #: The `[commands]` rule that sized the work (`shell.Rule.key`), and the top of the git work
    #: tree it runs in: what its outcome will be filed under. With a rule, a request that
    #: declares no memory may count what that rule's past runs there used (`[memory] learn`).
    rule: str | None = Field(None, max_length=1024)
    repo: str | None = Field(None, max_length=4096)

    @field_validator("memory", "memory_per_slot")
    @classmethod
    def _memory(cls, value: str | None) -> str | None:
        if value is not None:
            declared_to_bytes(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> AcquireRequest:
        if self.max_weight is not None and self.max_weight < self.weight:
            raise ValueError(f"max_weight {self.max_weight} is below weight {self.weight}")
        if self.memory is not None and self.memory_per_slot is not None:
            raise ValueError("memory and memory_per_slot are both given: give one or the other")
        if self.elastic and (self.max_weight or self.weight) <= self.weight:
            raise ValueError("only a range (max_weight above weight) can be elastic")
        return self

    @field_validator("lease_id", "session")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        if value is not None and not _ID.match(value):
            raise ValueError("is 1-64 of A-Z a-z 0-9 _ . : -")
        return value

    def session_name(self) -> str | None:
        session = self.session or self.labels.get("session")
        return session if session is not None and _ID.match(session) else None


def _grant_key(row: dict) -> tuple:
    """One grant of a lease: asked again and granted again is another."""
    return (row["scope"], row["item_id"], row["acquired_at"])


def _minute(now: float) -> int:
    t = time.localtime(now)
    return t.tm_hour * 60 + t.tm_min


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


#: The `code` of a refusal because no wait could end in a grant (exit 77, `Refused.code`).
NEVER_GRANTABLE = "never_grantable"


def _never(lease_id: str | None, reason: str) -> dict:
    """The answer to a request that could never be granted, however long it waited."""
    answer = {"status": "refused", "reason": reason, "code": NEVER_GRANTABLE}
    return answer if lease_id is None else answer | {"lease_id": lease_id}


def _over_quota(lease_id: str, reason: str) -> dict:
    return {"status": "refused", "lease_id": lease_id, "reason": reason, "code": "quota_exceeded"}


class MachineAgent:
    def __init__(
        self,
        config: MachineConfig,
        *,
        ledger: Ledger | None = None,
        facts: dict | None = None,
        load: LoadReader | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] | None = None,
        minute: Callable[[float], int] = _minute,
        processes: Callable[[], Mapping[int, Proc]] = proctable.scan,
        user_name: Callable[[int], str] | None = None,
        net: Callable[..., MemoryNet] = MemoryNet,
    ) -> None:
        self.config = config
        #: The process table, for `[[observe]]` rules, and the name of a process's owner (fakes
        #: in tests and the demo).
        self._processes = processes
        self._user_name = user_name or _user_name
        self._uids = observe.uid_table(config.observe)
        self.facts = facts if facts is not None else probe()
        cpus = self.facts.get("cpus_logical") or os.cpu_count() or 1
        self.limits = config.limits(
            cpus=cpus, memory_bytes=self.facts.get("memory_bytes") or 1 << 30
        )
        self._cpus = cpus
        self.ledger = ledger if ledger is not None else Ledger(config.ledger_path, now=clock)
        self.ledger.settle_s = config.elastic.settle_s
        self.ledger.headroom = config.elastic.headroom
        slices = list(dict.fromkeys(config.load.slices))
        # A user's daemon runs beside the user's work (in the same container, user slice or
        # limited scope), so a limit on its own cgroup bounds that work too. The machine's
        # daemon serves work that runs in users' slices and services elsewhere in the tree.
        self.loader = load if load is not None else LoadReader(slices, memory_limit=config.for_user)
        self._clock = clock
        #: The clock the guard's dwell and settling, and the settle window, are measured on:
        #: one that never steps. A fake `clock` (in tests and the demo) stands in for it too.
        self._monotonic = monotonic or (time.monotonic if clock is time.time else clock)
        self._minute = minute
        self.gate = LendGate(config.lend)
        memory = self.facts.get("memory_bytes") or self.limits.memory_bytes
        #: The machine's memory, which `[memory]` budgets.
        self.memory_bytes = memory
        bound = self.loader.limit()
        #: The cgroup memory limit the daemon's work was held to when it started, as
        #: `LoadReader.limit` reports it (None: none); an `auto` budget is worked out under it.
        self.memory_limit: dict | None = bound
        self.budget = config.memory_budget(memory, self._limit_bytes)
        self.guard = Guard(config.guard, min_available=config.guard.floor(memory))
        #: The cgroups whose memory pressure the guard reads besides the machine's, relative to
        #: the cgroup root: the slice contained commands run in, for a user's own daemon that
        #: contains them, and the cgroup that sets the memory limit, when one does.
        self.guard_cgroups: tuple[str, ...] = self._guard_cgroups()
        self.verdict = Verdict(False, None, "not measured yet", None)
        self.last_load: dict | None = None
        self.lend_pools = frozenset(config.lend.pools)
        #: Administrator override: lending stays shut even when the machine is idle.
        self._lend_off = False
        #: Administrator overrides of pools' `max`, never above the machine's slots.
        self._max_overrides: dict[str, int] = {}
        self.holder = Holder.current()
        self._lock = threading.RLock()
        #: Notified whenever a lease may have been freed or granted: blocked acquires recheck.
        self.changed = threading.Condition()
        #: Recent events (`leasehold.events`), worked out from the ledger after each change.
        self.events = EventLog()
        self._released: set[tuple[str, str]] = set()
        #: Observed leases given back because their process stopped matching, since then.
        self._unmatched: set[tuple[str, str]] = set()
        #: When each live lease was first seen granted, on `_monotonic`: the settle window.
        #: A lease from before this daemon started is placed by its wall-clock `acquired_at`
        #: (no later than now), so one granted within `settle_s` of a restart is still recent.
        wall, mono = self._clock(), self._monotonic()
        self._granted_at: dict[tuple, float] = {
            _grant_key(r): mono - max(0.0, wall - r["acquired_at"]) for r in self.ledger.leases()
        }
        #: When each waiting item was first seen in line, on `_monotonic` (`_overdue`).
        self._queued_at: dict[tuple[str, str], float] = {}
        #: `[memory] learn`: `off` without a `[memory]` table.
        self.learn = config.memory.learn if config.memory is not None else "off"
        #: (user, rule, repo) to (when worked out on `_monotonic`, the learned figure).
        self._learned: dict[tuple, tuple[float, estimate.Learned]] = {}
        #: Guards `_learned`, which request threads fill outside `_lock`.
        self._learned_lock = threading.Lock()
        #: The memory net (`leasehold.net`), when `[memory_net]` turns it on; `net` makes it
        #: (a test gives it fake roots). The server runs its loop. With it off, whatever a net
        #: before left paused or limited is let go.
        self.net: MemoryNet | None = None
        record = None if config.ledger_dir is None else config.ledger_dir / "memory-net.json"
        if config.memory_net.on and config.for_user:
            self.net = net(
                config.memory_net,
                leases=self._net_leases,
                on_change=self._net_changed,
                state=record,
                clock=clock,
                monotonic=self._monotonic,
            )
        else:
            memory_net.release(record)
        self._seen = self._snapshot()

    def _guard_cgroups(self) -> tuple[str, ...]:
        found = []
        if self.config.for_user and self.config.contain.mode != "off":
            found.append(contain.root_slice_cgroup())
        if self.memory_limit is not None:
            found.append(self.memory_limit["cgroup"].strip("/") or ".")
        return tuple(dict.fromkeys(c for c in found if c))

    def _memory_pressure(self, load: dict) -> float | None:
        """The memory PSI (`some avg10`) the guard goes by: the highest of the machine's and
        each of `guard_cgroups`' that can be read; None when none can. Pressure inside a
        cgroup's limit can be high while the machine's is low."""
        values = [some_avg10(load["pressure"]["memory"])]
        values += [some_avg10(self.loader.pressure(cg)["memory"]) for cg in self.guard_cgroups]
        known = [v for v in values if v is not None]
        return max(known) if known else None

    @property
    def _limit_bytes(self) -> int | None:
        return None if self.memory_limit is None else self.memory_limit["limit_bytes"]

    # --- decisions -----------------------------------------------------------------------------

    def policy(self) -> AdmissionPolicy:
        closed = frozenset() if self.verdict.eligible and not self._lend_off else self.lend_pools
        guarding = self.config.guarded and self.guard.on
        counting = self.budget is not None
        free = self.memory_free() if guarding and counting else None
        return self.config.policy(
            closed=closed,
            max_overrides=self._max_overrides,
            capped=guarding and not counting,
            memory_bytes=self.memory_bytes,
            memory_limit=self._limit_bytes,
            memory_free=free,
            memory_short=self._memory_short() if free is not None else None,
            memory_pressing=guarding and counting and self.guard.pressing,
            memory_overdue=self._overdue() if free is not None else frozenset(),
            lease_memory=self.limits.memory_bytes,
            maintenance=None if (info := self.maintenance()) is None else info["message"],
            memory_held=None if self.net is None else self.net.held(),
        )

    def maintenance(self) -> dict | None:
        """The maintenance lease waiters are told about (`leasehold.maintenance.describe`):
        the oldest one held, else the oldest one waiting for the machine to drain. None when
        there is neither."""
        for rows, held in ((self.ledger.leases(), True), (self.ledger.queued(), False)):
            for row in rows:
                if row["kind"] in WHOLE:
                    return maintenance.describe(status.public(row), held=held, now=self._clock())
        return None

    def _whole(self) -> int:
        """The weight a maintenance lease takes: every slot standing work does not hold."""
        return self.limits.slots - sum(self.config.standing_held().values())

    def memory_free(self) -> int:
        """While the guard is on, the most memory new work may count: none while a reading is
        over a threshold, else what is available above the floor less what leases granted
        within `settle_s` count (they may not have grown to it yet). Available is the smaller
        of `MemAvailable` and what the cgroup limit leaves (`leasehold.load.available`). A
        lease not yet seen granted counts as recent. Observed leases are not counted: they
        record processes already running, whose memory is already in use."""
        free = available(self.last_load)
        if self.guard.pressing or free is None or self.budget is None:
            return 0
        since = self._monotonic() - self.config.guard.settle_s
        recent = sum(
            row["memory"]
            if row["memory"] is not None
            else row["weight"]
            * (
                row["memory_per_slot"]
                if row["memory_per_slot"] is not None
                else self.budget.assumed_per_slot
            )
            for row in self.ledger.leases()
            if row["scope"] != OBSERVED_SCOPE
            and self._granted_at.get(_grant_key(row), since) >= since
        )
        return max(0, free - self.guard.min_available - recent)

    def _overdue(self) -> frozenset[tuple[str, str]]:
        """The waiting items that have waited longer than `settle_s`, measured on `_monotonic`:
        short only of the memory free, they stop letting the items behind them start. Each is
        stamped when first seen waiting, at its wall-clock `queued_at` taken back from now (no
        later than now), so a step of the wall clock after that moves nothing. Under the lock:
        `policy()` runs from threads that do not hold it."""
        with self._lock:
            wall, mono = self._clock(), self._monotonic()
            live = {(r["scope"], r["item_id"]): r["queued_at"] for r in self.ledger.queued()}
            for key, queued_at in live.items():
                if key not in self._queued_at:
                    self._queued_at[key] = mono - max(0.0, wall - queued_at)
            for key in self._queued_at.keys() - live.keys():
                del self._queued_at[key]
            since = mono - self.config.guard.settle_s
            return frozenset(k for k, at in self._queued_at.items() if at < since)

    def _note_grants(self) -> None:
        """Note when each lease granted since the last call was first seen; forget ended ones."""
        now = self._monotonic()
        live = {_grant_key(r) for r in self.ledger.leases()}
        for key in live - self._granted_at.keys():
            self._granted_at[key] = now
        for key in self._granted_at.keys() - live:
            del self._granted_at[key]

    def _net_leases(self) -> list[tuple[str, float]]:
        """The leases the memory net watches: every lease of `SCOPE`, with when it was
        granted."""
        return [
            (r["item_id"], r["acquired_at"]) for r in self.ledger.leases() if r["scope"] == SCOPE
        ]

    def _net_changed(self) -> None:
        """The memory net parked or resumed a lease, or one has settled: record it, and admit
        what its hold no longer keeps back."""
        assert self.net is not None
        with self._lock:
            rows = {r["item_id"]: r for r in self.ledger.leases() if r["scope"] == SCOPE}
            found = []
            for event in self.net.events():
                row = rows.get(event["lease_id"])
                about = {} if row is None else status.public(row)
                found.append(
                    {
                        "type": EventType(event["type"]),
                        **{k: about.get(k) for k in ("kind", "pool", "user", "weight")},
                        "lease_id": event["lease_id"],
                    }
                )
            admitted = self.ledger.settle(self.policy())
            self._record(*found)
        if admitted:
            self._notify()

    def _memory_short(self) -> str:
        if self.guard.pressing:
            return f"memory guard on: {self.guard.reason}"
        return f"memory guard settling after {self.guard.reason}"

    def set_lend(self, enabled: bool) -> dict:
        """Whether lend leases may be granted. Off closes the lend pools now and revokes
        what they hold; on lets the idle gate decide again. It never raises the self-cap."""
        with self._lock:
            self._lend_off = not enabled
            if self._lend_off:
                self.verdict = Verdict(False, _LENDING_OFF, _LENDING_OFF, None)
            elif self.last_load is not None:
                self.verdict = self.gate.observe(self.observe())
            reason = _LENDING_OFF if self._lend_off else self.verdict.revoke
            revoked = self._revoke(reason) if reason else []
            self.ledger.settle(self.policy())
            self._record({"type": EventType.LEND_SWITCH, "open": enabled})
        if revoked:
            self._notify()
        return self._lend_status()

    def set_pool_max(self, pool: str, maximum: int | None) -> dict:
        """Give `pool` a `max` other than its configured one until the daemon restarts (None:
        back to the configured one). Never above the machine's slots, nor above what the pool
        could ever hold beside the other pools' reservations and standing work."""
        pools = self.config.pool_set().pools
        if pool not in pools:
            raise ValueError(f"there is no pool {pool!r}")
        if maximum is not None and not 1 <= maximum <= self.limits.slots:
            raise ValueError(f"max must be between 1 and the machine's {self.limits.slots} slots")
        if maximum is not None and pool != self.config.observed_pool:
            held = self.config.standing_held()
            others = sum(
                max(p.reserved, held.get(name, 0)) for name, p in pools.items() if name != pool
            )
            if maximum > self.limits.slots - others:
                raise ValueError(
                    f"max {maximum} could never be reached: the other pools' reservations and "
                    f"standing work keep {others} of the {self.limits.slots} slots"
                )
        if maximum is not None and maximum < pools[pool].reserved:
            raise ValueError(
                f"max {maximum} is below pool {pool!r}'s reserved {pools[pool].reserved}"
            )
        with self._lock:
            # Checked and set under the lock acquires queue under: nothing heavier can join the
            # line in between.
            standing = self.config.standing_held().get(pool, 0)
            waiting = [r["weight"] for r in self.ledger.queued() if r["pool"] == pool]
            heaviest = max(waiting, default=0)
            if maximum is not None and maximum < standing:
                raise ValueError(
                    f"max {maximum} is below the {standing} slots standing work holds in "
                    f"pool {pool!r}"
                )
            if maximum is not None and maximum < heaviest:
                # It could never be admitted, and with no backfill it would hold up the line.
                raise ValueError(
                    f"max {maximum} is below the weight {heaviest} waiting in pool {pool!r}"
                )
            if maximum is None:
                self._max_overrides.pop(pool, None)
            else:
                self._max_overrides[pool] = maximum
            admitted = self.ledger.settle(self.policy())
            current = maximum if maximum is not None else pools[pool].max
            self._record(
                {
                    "type": EventType.POOL_MAX,
                    "pool": pool,
                    "max": current,
                    "overridden": maximum is not None,
                }
            )
        if admitted:
            self._notify()
        return {
            "pool": pool,
            "max": maximum if maximum is not None else pools[pool].max,
            "configured_max": pools[pool].max,
            "overridden": maximum is not None,
        }

    def _pressure(self, resource: str) -> float | None:
        cgroups = self.config.lend.pressure_cgroups
        if cgroups:
            values = [some_avg10(self.loader.pressure(cg)[resource]) for cg in cgroups]
            known = [v for v in values if v is not None]
            if known:
                return max(known)
        assert self.last_load is not None
        return some_avg10(self.last_load["pressure"][resource])

    def observe(self) -> Observation:
        now = self._clock()
        queued_other = sum(1 for r in self.ledger.queued() if r["kind"] not in PREEMPTIBLE)
        loadavg = self.last_load["loadavg"] if self.last_load else None
        return Observation(
            now=now,
            minute=self._minute(now),
            queued_other=queued_other,
            cpu_pressure=self._pressure("cpu"),
            memory_pressure=self._pressure("memory"),
            load_per_cpu=None if not loadavg else loadavg[0] / self._cpus,
        )

    def tick(self) -> dict:
        """Measure, decide on lending, revoke what must stop, admit what fits."""
        load = self.loader.read()
        with self._lock:
            self.last_load = load
            if self.config.guarded:
                self.guard.observe(
                    Reading(
                        now=self._monotonic(),
                        at=self._clock(),
                        memory_pressure=self._memory_pressure(load),
                        available=available(load),
                    )
                )
            if not self._lend_off:
                self.verdict = self.gate.observe(self.observe())
            reason = _LENDING_OFF if self._lend_off else self.verdict.revoke
            revoked = self._revoke(reason) if reason else []
            self._watch_processes()
            admitted = self.ledger.settle(self.policy())
            self._record()
        if revoked or admitted:
            self._notify()
        return {"revoked": revoked, "admitted": admitted, "lend": self._lend_status()}

    def _watch_processes(self) -> None:
        """Impose an observed lease for each process an `[[observe]]` rule counts and the ledger
        does not hold yet; one already held is beaten. Its end is the ledger's to notice."""
        held = [r for r in self.ledger.leases() if r["scope"] == OBSERVED_SCOPE]
        if not self.config.observe and not held:
            return  # nothing to count and nothing to give back: no scan
        procs = self._processes()
        policy = self.policy()
        pool = self.config.observed_pool
        wanted: set[str] = set()
        leased = observe.under_leasehold(procs)
        for rule in self.config.observe:
            for proc in observe.roots(rule, procs, self._uids, leased):
                wanted.add(observe.item_id(rule, proc))
                item = LedgerItem(
                    scope=OBSERVED_SCOPE,
                    item_id=observe.item_id(rule, proc),
                    kind="observed",
                    user=self._user_name(proc.uid),
                    pool=rule.pool or pool,
                    weight=rule.weight,
                    labels=(
                        ("command", proc.cmdline[:200] or proc.comm),
                        ("pid", str(proc.pid)),
                        ("rule", rule.name),
                    ),
                )
                self.ledger.impose(item, policy, holder=Holder(proc.pid, str(proc.start)))
        # A process still running but no longer counted (it exec'd something else, max_count
        # shrank) gives its lease back, and so does every lease of a rule that is gone, seen or
        # not. Another the scan cannot see is left to the ledger, which ends it when the
        # process is gone.
        rules = {rule.name for rule in self.config.observe}
        for lease in held:
            if lease["item_id"] not in wanted:
                labels = lease["labels"] or {}
                pid = int(labels.get("pid", -1))
                if pid in procs or labels.get("rule") not in rules:
                    self._unmatched.add((OBSERVED_SCOPE, lease["item_id"]))
                    self.ledger.release((OBSERVED_SCOPE, lease["item_id"]))

    def _revoke(self, reason: str) -> list:
        """Revoke every lease of a `PREEMPTIBLE` kind; the keys newly revoked."""
        return [
            key
            for kind in sorted(PREEMPTIBLE)
            for key in self.ledger.revoke(
                kind=kind, reason=reason, grace_s=self.config.lend.revoke_grace_s
            )
        ]

    def _snapshot(self) -> Snapshot:
        policy = self.policy()
        lines = self.ledger.lines(policy)
        leases = [status.public(r) | {"scope": r["scope"]} for r in self.ledger.leases()]
        queue = [
            status.public(r, policy)
            | {"scope": r["scope"], "position": lines.get((r["scope"], r["item_id"]), (None,))[0]}
            for r in self.ledger.queued()
        ]
        lend = self._lend_status()
        return Snapshot.of(leases, queue, lend_open=lend["eligible"], reason=lend["reason"])

    def _record(self, *extra: dict) -> None:
        """Emit what changed since the last call, after `extra` (an administrator's change)."""
        with self._lock:
            now = self._clock()
            seen = self._snapshot()
            found = changes(
                self._seen,
                seen,
                now=now,
                released=frozenset(self._released),
                unmatched=frozenset(self._unmatched),
            )
            self._seen = seen
            self._note_grants()
            self._released.clear()
            self._unmatched.clear()
            self.events.emit([*extra, *found], now=now)

    def _notify(self) -> None:
        with self.changed:
            self.changed.notify_all()

    # --- requests ------------------------------------------------------------------------------

    def _refusal(self, req: AcquireRequest) -> tuple[str | None, str | None]:
        """(pool, None) for a request that may go in line, or (None, why not)."""
        if req.kind not in GRANTED:
            return None, f"{req.kind} leases are made by the machine agent, never asked for"
        pool = req.pool or self.config.kind_pool(req.kind)
        if pool is None:
            return None, f"no pool is set for {req.kind} leases"
        if pool not in self.config.pool_set().pools:
            return None, f"there is no pool {pool!r}"
        if pool == self.config.observed_pool:
            return None, f"pool {pool!r} holds only observed processes"
        if (req.kind in PREEMPTIBLE) != (pool in self.lend_pools):
            return None, f"pool {pool!r} takes " + (
                "only lend leases" if pool in self.lend_pools else "no lend leases"
            )
        if req.kind in WHOLE and self._whole() < 1:
            return None, (
                f"[[standing]] work holds all {self.limits.slots} slots, so a maintenance lease "
                "has none to take; lower its weight to take the machine"
            )
        return pool, None

    def acquire(self, req: AcquireRequest, *, user: str | None, in_line: bool = False) -> dict:
        """One admission attempt (no waiting): `granted`, `queued` or `refused`. With `in_line`
        (asked again while waiting), a request no longer in line (its place was given back) is
        refused rather than queued again."""
        pool, refused = self._refusal(req)
        if refused is not None:
            return _never(None, refused)
        if req.kind in WHOLE:
            # The whole machine, whatever was asked: its size is not the client's to choose.
            req = req.model_copy(
                update={"weight": self._whole(), "max_weight": None, "elastic": False}
            )
        lease_id = req.lease_id or uuid.uuid4().hex
        memory = None if req.memory is None else memory_to_bytes(req.memory)
        per_slot = None if req.memory_per_slot is None else memory_to_bytes(req.memory_per_slot)
        least = memory if per_slot is None else req.weight * per_slot
        # [ceiling] memory limits each lease: a per-slot size always, a total with a budget.
        limited = self.budget is not None or per_slot is not None
        if limited and least is not None and least > self.limits.memory_bytes:
            what = (
                f"memory {req.memory}"
                if per_slot is None
                else f"memory_per_slot {req.memory_per_slot} at weight {req.weight} "
                f"({least / 2**30:.1f} GiB)"
            )
            limit = lease_memory_limit(self.config.ceiling.memory, self.limits.memory_bytes)
            return _never(lease_id, f"{what} is more than {limit}")

        # Learned after the [ceiling] check, which is for declared memory only: a learned
        # figure is no limit on the command, so it never refuses one.
        learned, learned_runs = self._learned_for(req, user)
        if learned is not None and self.learn == "on":
            per_slot = learned
        item = LedgerItem(
            scope=SCOPE,
            item_id=lease_id,
            kind=req.kind,
            user=user,
            pool=pool,
            weight=req.weight,
            labels=tuple(sorted(req.labels.items())),
            session=req.session_name(),
            memory=memory,
            max_weight=req.max_weight,
            memory_per_slot=per_slot,
            elastic=req.elastic,
            learned_per_slot=learned,
            learned_runs=learned_runs,
        )
        ttl = req.ttl_s or self.config.lease_ttl_s
        with self._lock:
            # Under the lock, so two racing first asks with one id cannot both be new submits.
            known = self._check_owner(lease_id, user)  # the ledger checks again, in its tx
            if in_line and not known:
                reason = "its place in line was given back while it waited"
                return {"status": "refused", "lease_id": lease_id, "reason": reason}
            limits = self.config.pool_set().pools[pool]
            rate = limits.create_rate_per_min
            if not known and rate is not None and self.ledger.recent_submits(user, pool) >= rate:
                return _over_quota(
                    lease_id,
                    f"{user or 'this user'} has submitted {rate} item(s) to pool {pool!r} in the "
                    f"last minute, its per-user limit",
                )
            outcome = self.ledger.try_admit(item, self.policy(), ttl_s=ttl, holder=self.holder)
            if not known and isinstance(outcome, Queued) and self._over_queued(item, limits):
                self.ledger.dequeue(item)  # it may not wait: back out of line, as if never asked
                return _over_quota(
                    lease_id,
                    f"{user or 'this user'} already has {limits.per_user_max_queued} item(s) "
                    f"waiting in pool {pool!r}, its per-user limit",
                )
            if not known and rate is not None and isinstance(outcome, Admitted | Queued):
                # Counted only once kept: a refused request never uses up the rate.
                self.ledger.record_submit(user, pool, max_queued=None, rate_per_min=None)
            if isinstance(outcome, Queued) and req.kind not in PREEMPTIBLE:
                # Work is waiting: lending stops now, and the quiet period starts over.
                self.gate.reset()
                self.verdict = Verdict(
                    False, "non-lend work waiting", "non-lend work waiting", None
                )
                if self._revoke("non-lend work waiting"):
                    self._notify()
            self._record()
        if isinstance(outcome, Admitted):
            lease = self._lease(lease_id)
            if lease is not None:
                lease["jobs"] = self.config.jobs.for_weight(lease["weight"])
                lease["target_jobs"] = self.config.jobs.for_weight(lease["target"])
                lease["max_jobs"] = self.config.jobs.for_weight(lease["max_weight"])
                lease["memory_counted_bytes"] = status.counted(self.policy(), lease)
            return {
                "status": "granted",
                "lease": lease,
                "contain": self.config.contain.model_dump(),
                "elastic": self.config.elastic.model_dump(),
            }
        if isinstance(outcome, Queued):
            info = self.maintenance()
            held_up = {} if info is None or info["lease_id"] == lease_id else {"maintenance": info}
            return {
                "status": "queued",
                "lease_id": lease_id,
                "position": outcome.position,
                "blocked": outcome.blocked,
                **self._waiting_range(lease_id),
                **held_up,
            }
        return _never(lease_id, outcome.reason)

    def learned(self, user: str | None, rule: str | None, repo: str | None) -> estimate.Learned:
        """What `user`'s past runs of `rule` in `repo` give (`leasehold.estimate.learn`), worked
        out again after `LEARNED_TTL_S` or once a new outcome for them is kept."""
        key = (user, rule, repo)
        now = self._monotonic()
        with self._learned_lock:
            hit = self._learned.get(key)
        if hit is not None and now - hit[0] < LEARNED_TTL_S:
            return hit[1]
        found = estimate.learn(self.ledger.outcomes_of(user, rule, repo))
        with self._learned_lock:
            if len(self._learned) >= 4096:
                self._learned.clear()
            self._learned[key] = (now, found)
        return found

    def _forget_learned(self, rule: str | None, repo: str | None) -> None:
        """Drop every user's cached figure for `rule` in `repo`: a new outcome for it is kept."""
        with self._learned_lock:
            for key in [k for k in self._learned if k[1:] == (rule, repo)]:
                del self._learned[key]

    def _learned_for(self, req: AcquireRequest, user: str | None) -> tuple[int | None, int | None]:
        """The learned figure a slot for `req` and the clean runs it rests on, as `LedgerItem`
        keeps them: (None, None) when none is worked out (learning off, memory declared, no rule,
        or a lease of the whole machine), (None, runs) while too few runs are known. A figure is
        cut only so that `req`'s least weight of it fits the budget beside standing memory: such
        work is admitted alone, not refused."""
        if (
            self.learn == "off"
            or req.memory is not None
            or req.memory_per_slot is not None
            or req.rule is None
            or req.kind in WHOLE
        ):
            return None, None
        found = self.learned(user, req.rule, req.repo)
        if found.per_slot is None:
            return None, found.runs
        if self.budget is None:
            return found.per_slot, found.runs
        room = self.budget.budget - sum(self.config.standing_memory().values())
        return min(found.per_slot, max(1, room // req.weight)), found.runs

    def _waiting_range(self, lease_id: str) -> dict:
        """The range the item `lease_id` waits with, as admission would cut it now
        (`AdmissionPolicy.most`): `min_weight` and `max_weight`; empty when it is not waiting."""
        for row in self.ledger.queued():
            if row["scope"] == SCOPE and row["item_id"] == lease_id:
                weight = row["weight"]
                most = self.policy().most(
                    row["pool"], weight, row["max_weight"], row["memory"], row["memory_per_slot"]
                )
                return {"min_weight": weight, "max_weight": most}
        return {}

    def acquire_wait(self, req: AcquireRequest, *, user: str | None) -> dict:
        """`acquire`, retried as capacity changes until granted, refused or `wait_s` is up. A
        place in line given back while it waits (`release`) ends the wait, refused."""
        deadline = time.monotonic() + req.wait_s
        if req.lease_id is None:
            req = req.model_copy(update={"lease_id": uuid.uuid4().hex})
        in_line = False
        while True:
            answer = self.acquire(req, user=user, in_line=in_line)
            in_line = True
            left = deadline - time.monotonic()
            if answer["status"] != "queued" or left <= 0:
                return answer
            with self.changed:
                self.changed.wait(min(left, 0.5))

    def renew(self, lease_id: str, *, user: str | None, ttl_s: float | None = None) -> dict:
        # Checked and done under one lock: every change to this scope goes through the agent.
        with self._lock:
            self._check_owner(lease_id, user)
            renewal = self.ledger.renew((SCOPE, lease_id), ttl_s=ttl_s)
        answer = {
            "status": renewal.status,
            "lease_id": lease_id,
            "expires_at": renewal.expires_at,
            "reason": renewal.reason,
        }
        if renewal.weight is not None:
            answer |= {
                "weight": renewal.weight,
                "min_weight": renewal.min_weight,
                "max_weight": renewal.max_weight,
                "pool": renewal.pool,
                "jobs": self.config.jobs.for_weight(renewal.weight),
                "target": renewal.target,
                "target_jobs": self.config.jobs.for_weight(renewal.target or renewal.weight),
                "elastic": renewal.elastic,
            }
        if renewal.status == "ok":
            answer["parked"] = self.net is not None and self.net.state(lease_id) == "parked"
        return answer

    def resize(self, lease_id: str, weight: int, *, user: str | None) -> dict:
        """Move the elastic lease `lease_id` to `weight` (`Ledger.resize`): give slots back, or
        take the ones its target offers. A growth that no longer fits, or a lease no longer
        held, is a `Conflict`; a lease that is not elastic, or a weight outside its range, a
        ValueError."""
        with self._lock:
            self._check_owner(lease_id, user)
            done = self.ledger.resize((SCOPE, lease_id), weight, self.policy())
            if done.status == "resized":
                self.ledger.settle(self.policy())
                self._record()
        if done.status == "unknown":
            raise Conflict(f"lease {lease_id} is not held")
        if done.status == "refused":
            raise Conflict(f"lease {lease_id} cannot grow to {weight} now: {done.reason}")
        self._notify()
        assert done.weight is not None and done.target is not None
        return {
            "status": "resized",
            "lease_id": lease_id,
            "weight": done.weight,
            "target": done.target,
            "jobs": self.config.jobs.for_weight(done.weight),
            "target_jobs": self.config.jobs.for_weight(done.target),
        }

    def make_elastic(self, lease_id: str, *, user: str | None) -> dict:
        """Make the held range `lease_id` elastic from now on (`Ledger.make_elastic`): its
        weight and target, and their job counts. A lease no longer held is a `Conflict`; one
        with no range, a ValueError."""
        with self._lock:
            self._check_owner(lease_id, user)
            done = self.ledger.make_elastic((SCOPE, lease_id), self.policy())
            if done.status == "resized":
                self.ledger.settle(self.policy())
                self._record()
        if done.status == "unknown":
            raise Conflict(f"lease {lease_id} is not held")
        self._notify()
        assert done.weight is not None and done.target is not None
        return {
            "status": "elastic",
            "lease_id": lease_id,
            "elastic": True,
            "weight": done.weight,
            "target": done.target,
            "jobs": self.config.jobs.for_weight(done.weight),
            "target_jobs": self.config.jobs.for_weight(done.target),
        }

    def release(
        self, lease_id: str, *, user: str | None, outcome: outcomes.Outcome | None = None
    ) -> dict:
        """Release `lease_id`, keeping `outcome`, what its command used, with what the lease
        held (`Ledger.record_outcome`) and how often the memory net parked it, when the lease
        is still held."""
        with self._lock:
            self._check_owner(lease_id, user)
            parks = {}
            if self.net is not None:
                parks = {"parks": 0, "parked_s": 0.0} | self.net.take(lease_id)
            if outcome is not None:
                self.ledger.record_outcome((SCOPE, lease_id), outcome.model_dump() | parks)
                self._forget_learned(outcome.rule, outcome.repo)
            self._released.add((SCOPE, lease_id))
            self.ledger.release((SCOPE, lease_id))
            self.ledger.settle(self.policy())
            self._record()
        self._notify()
        return {"status": "released", "lease_id": lease_id}

    def _over_queued(self, item: LedgerItem, limits) -> bool:
        """Whether `item`, just queued, takes its user past the pool's `per_user_max_queued`."""
        if limits.per_user_max_queued is None:
            return False
        mine = [
            row
            for row in self.ledger.queued()
            if row["pool"] == item.pool and row["user"] == item.user
        ]
        return len(mine) > limits.per_user_max_queued

    def _check_owner(self, lease_id: str, user: str | None) -> bool:
        """Refuse a request about a lease (or place in line) another user holds; whether the
        ledger has it at all. One nobody holds is anyone's to ask for."""
        for row in self.ledger.leases() + self.ledger.queued():
            if row["scope"] == SCOPE and row["item_id"] == lease_id:
                if row["user"] != user and user != "root":
                    raise Forbidden(f"lease {lease_id} is held by another user")
                return True
        return False

    def _lease(self, lease_id: str) -> dict | None:
        for row in self.ledger.leases():
            if row["scope"] == SCOPE and row["item_id"] == lease_id:
                return status.public(row)
        return None

    # --- reports -------------------------------------------------------------------------------

    def leases(self) -> dict:
        """The leases held and the queue, each queued item with its place and why it waits. The
        queue is in line order: by pool (in the order pools are served), then place, then
        arrival."""
        policy = self.policy()
        lines = self.ledger.lines(policy)
        pool_order = {name: i for i, name in enumerate(self.config.pool_set().in_order())}
        queue = []
        for row in self.ledger.queued():
            place, blocked = lines.get((row["scope"], row["item_id"]), (None, None))
            queue.append(status.public(row, policy) | {"position": place, "blocked": blocked})
        queue.sort(
            key=lambda q: (
                pool_order.get(q["pool"], len(pool_order)),
                q["position"] is None,
                q["position"] or 0,
            )
        )  # stable: arrival order breaks ties
        leases = [status.public(r) for r in self.ledger.leases()]
        return {
            "leases": status.with_counted(policy, leases),
            "queue": status.with_counted(policy, queue),
        }

    def outcomes(self, *, user: str | None, everyone: bool = False) -> dict:
        """The outcomes kept (`Ledger.outcomes`): `user`'s, or with `everyone` every user's,
        newest first (`outcomes`), and summed up by rule and repository (`groups`,
        `leasehold.outcomes.summarize`). Each group with a rule also has
        `learned_per_slot_bytes` and `learned_runs`: the figure a slot `user`'s own runs there
        give (`learned`; None while too few are known) and the clean runs behind it. A group
        with no rule has None for both."""
        rows = self.ledger.outcomes(user=user, everyone=everyone)
        groups = outcomes.summarize(rows)
        for group in groups:
            found = None
            if group["rule"] is not None:
                found = self.learned(user, group["rule"], group["repo"])
            group["learned_per_slot_bytes"] = None if found is None else found.per_slot
            group["learned_runs"] = None if found is None else found.runs
        return {"groups": groups, "outcomes": rows}

    def _lend_status(self) -> dict:
        v = self.verdict
        off = self._lend_off
        return {
            "eligible": False if off else v.eligible,
            "reason": _LENDING_OFF if off else v.reason,
            "quiet_since": None if off else v.quiet_since,
            "quiet_s": self.config.lend.quiet_s,
            "window": self.config.lend.window,
            "pools": sorted(self.lend_pools),
            "switch": not off,
            "thresholds": {
                "cpu": self.config.lend.cpu_pressure_max,
                "memory": self.config.lend.memory_pressure_max,
                "load_per_cpu": self.config.lend.load_per_cpu_max,
            },
        }

    def commands(self) -> dict:
        """Which commands take a lease, for agent hooks and shims to match against: each rule
        with its pool named and its weight and `max_weight` each cut to the most one lease
        declaring its memory could ever be granted in that pool (`AdmissionPolicy.most`: what
        fits the pool and the machine, and with a per-slot size `[ceiling] memory`), so what it
        asks for can be granted, and that most as its `ceiling`; each of its cases' weights is
        cut to the same most, and 0 stays 0 (no lease). Its `hook_ceiling` is what the
        rule's range alone would be granted on an idle machine (`admission.alone`): the most an
        agent hook asks, cutting both ends of its range to it. All are computed when served,
        from the pools as they are then. A rule declaring `memory_per_slot` alone is also served a
        `memory`, its least weight's worth, for clients from before memory per slot. A rule no
        lease could ever be granted for (`MachineConfig.never_reason`, which judges its least
        weight) is served at its own weights with `never`, the reason: it still matches its
        commands, and hooks and shims refuse them rather than take a lighter rule's lease."""
        settings = self.config.commands
        policy = self.policy()
        rules = []
        for src, rule in settings.rules():
            served = rule.model_dump() | {
                "pool": self.config.rule_pool(rule),
                "source": src,
                "max_weight": rule.most,
            }
            reason = self.config.never_reason(rule, policy, self.memory_bytes)
            if reason is not None:
                rules.append(served | {"weight": rule.weight, "never": reason})
                continue
            memory = None if rule.memory is None else memory_to_bytes(rule.memory)
            per_slot = (
                None if rule.memory_per_slot is None else memory_to_bytes(rule.memory_per_slot)
            )
            pool = served["pool"]
            most = policy.most(pool, 1, self.config.ceiling.slots, memory, per_slot)
            top = min(most, self.config.ceiling.slots)
            least = min(rule.weight, top)
            most = max(least, min(rule.most, top))
            sizes = {
                "weight": least,
                "max_weight": most,
                "ceiling": top,
                "hook_ceiling": alone(policy, pool, least, most, memory, per_slot),
            }
            if per_slot is not None and memory is None:
                sizes["memory"] = f"{least * per_slot}b"
            sizes["case"] = [c | {"weight": min(c["weight"], top)} for c in served["case"]]
            rules.append(served | sizes)
        return {
            "rules": rules,
            "wait_s": settings.wait_s,
            "when_busy": settings.when_busy,
        }

    def status(self) -> dict:
        if self.last_load is None:
            self.last_load = self.loader.read()
        listing = self.leases()
        return status.report(
            config=self.config,
            limits=self.limits,
            policy=self.policy(),
            facts=self.facts,
            load=self.last_load,
            lend=self._lend_status(),
            guard=self.guard.status()
            | {"enabled": self.config.guarded, "pressure_cgroups": list(self.guard_cgroups)},
            budget=self.budget,
            learn=self.learn,
            memory_limit=self.memory_limit,
            leases=listing["leases"],
            queue=listing["queue"],
            maintenance=self.maintenance(),
            memory_net=None if self.net is None else self.net.status(),
            now=self._clock(),
            version=__version__,
        )
