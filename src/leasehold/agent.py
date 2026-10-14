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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from leasehold import __version__, observe, proctable, status
from leasehold.access import Forbidden
from leasehold.admission import AdmissionPolicy
from leasehold.events import EventLog, EventType, Snapshot, changes
from leasehold.facts import probe
from leasehold.guard import Guard, Reading
from leasehold.kinds import GRANTED, PREEMPTIBLE, LeaseKind
from leasehold.ledger import Admitted, Holder, Ledger, LedgerItem, Queued
from leasehold.lend import LendGate, Observation, Verdict
from leasehold.load import LoadReader, some_avg10
from leasehold.machine import MachineConfig
from leasehold.proctable import Proc

#: The ledger scope observed processes' leases are filed under; no client may name it.
OBSERVED_SCOPE = "leasehold.observed"

#: The ledger scope every lease this agent grants is filed under.
SCOPE = "leasehold"
#: What a lease id and a session name may be made of.
ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,64}$"
_ID = re.compile(ID_PATTERN)
_LENDING_OFF = "lending switched off"

__all__ = ["SCOPE", "AcquireRequest", "Forbidden", "MachineAgent"]


class AcquireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: LeaseKind
    pool: str | None = None
    weight: int = Field(1, ge=1)
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

    @field_validator("lease_id", "session")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        if value is not None and not _ID.match(value):
            raise ValueError("is 1-64 of A-Z a-z 0-9 _ . : -")
        return value

    def session_name(self) -> str | None:
        session = self.session or self.labels.get("session")
        return session if session is not None and _ID.match(session) else None


def _minute(now: float) -> int:
    t = time.localtime(now)
    return t.tm_hour * 60 + t.tm_min


def _user_name(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


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
        minute: Callable[[float], int] = _minute,
        processes: Callable[[], Mapping[int, Proc]] = proctable.scan,
        user_name: Callable[[int], str] | None = None,
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
        slices = list(dict.fromkeys(config.load.slices))
        self.loader = load if load is not None else LoadReader(slices)
        self._clock = clock
        self._minute = minute
        self.gate = LendGate(config.lend)
        memory = self.facts.get("memory_bytes") or self.limits.memory_bytes
        self.guard = Guard(config.guard, min_available=config.guard.floor(memory))
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
        self._seen = self._snapshot()

    # --- decisions -----------------------------------------------------------------------------

    def policy(self) -> AdmissionPolicy:
        closed = frozenset() if self.verdict.eligible and not self._lend_off else self.lend_pools
        return self.config.policy(
            closed=closed,
            max_overrides=self._max_overrides,
            capped=self.config.guarded and self.guard.on,
        )

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
                        now=self._clock(),
                        memory_pressure=some_avg10(load["pressure"]["memory"]),
                        available=load.get("mem_available_bytes"),
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
        lines = self.ledger.lines(self.policy())
        leases = [status.public(r) | {"scope": r["scope"]} for r in self.ledger.leases()]
        queue = [
            status.public(r)
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
        pool = req.pool or self.config.kinds.get(req.kind)
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
        return pool, None

    def acquire(self, req: AcquireRequest, *, user: str | None) -> dict:
        """One admission attempt (no waiting): `granted`, `queued` or `refused`."""
        pool, refused = self._refusal(req)
        if refused is not None:
            return {"status": "refused", "reason": refused}
        lease_id = req.lease_id or uuid.uuid4().hex
        item = LedgerItem(
            scope=SCOPE,
            item_id=lease_id,
            kind=req.kind,
            user=user,
            pool=pool,
            weight=req.weight,
            labels=tuple(sorted(req.labels.items())),
            session=req.session_name(),
        )
        ttl = req.ttl_s or self.config.lease_ttl_s
        with self._lock:
            # Under the lock, so two racing first asks with one id cannot both be new submits.
            known = self._check_owner(lease_id, user)  # the ledger checks again, in its tx
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
            return {
                "status": "granted",
                "lease": lease,
                "contain": self.config.contain.model_dump(),
            }
        if isinstance(outcome, Queued):
            return {
                "status": "queued",
                "lease_id": lease_id,
                "position": outcome.position,
                "blocked": outcome.blocked,
            }
        return {"status": "refused", "lease_id": lease_id, "reason": outcome.reason}

    def acquire_wait(self, req: AcquireRequest, *, user: str | None) -> dict:
        """`acquire`, retried as capacity changes until granted, refused or `wait_s` is up."""
        deadline = time.monotonic() + req.wait_s
        if req.lease_id is None:
            req = req.model_copy(update={"lease_id": uuid.uuid4().hex})
        while True:
            answer = self.acquire(req, user=user)
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
        return {
            "status": renewal.status,
            "lease_id": lease_id,
            "expires_at": renewal.expires_at,
            "reason": renewal.reason,
        }

    def release(self, lease_id: str, *, user: str | None) -> dict:
        with self._lock:
            self._check_owner(lease_id, user)
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
            queue.append(status.public(row) | {"position": place, "blocked": blocked})
        queue.sort(
            key=lambda q: (
                pool_order.get(q["pool"], len(pool_order)),
                q["position"] is None,
                q["position"] or 0,
            )
        )  # stable: arrival order breaks ties
        return {"leases": [status.public(r) for r in self.ledger.leases()], "queue": queue}

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
        with its pool named and its weight cut to the most one lease could ever be admitted
        with in that pool (`AdmissionPolicy.heaviest`), so what it asks for can be granted."""
        settings = self.config.commands
        policy = self.policy()
        rules = []
        for src, rule in settings.rules():
            pool = rule.pool or self.config.kinds.get("work", "work")
            top = policy.heaviest(pool) if pool in policy.pools else rule.weight
            weight = max(1, min(rule.weight, top, self.config.ceiling.slots))
            rules.append(rule.model_dump() | {"pool": pool, "weight": weight, "source": src})
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
            guard=self.guard.status() | {"enabled": self.config.guarded},
            leases=listing["leases"],
            queue=listing["queue"],
            now=self._clock(),
            version=__version__,
        )
