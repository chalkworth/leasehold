"""Which waiting items the pools can admit now: a pure function of what is held, what waits and
the policy (`decide`). The ledger (`leasehold.ledger`) calls it inside its one transaction and
turns the answer into leases.

The rules, per host of `C` slots (a machine's pools all share one), with `U_q` the weight pool
`q` holds and `R_q` its reservation:

* an item of weight `w` enters pool `p` only if `U_p + w <= max_p` and
  `C - sum(U) - w >= sum over the host's other pools q of max(0, R_q - U_q)`: a reservation is
  never lent to another pool, and nothing is ever preempted to make room;
* pools are served in priority order (lower first, then by name);
* inside a pool, users take turns: the next item is the head of the user with the fewest items
  running in the pool, then the one served longest ago (`State.served`, which the ledger keeps,
  so the rotation survives across calls and one-slot pools), then the oldest head. A user with
  one queued item is admitted within one rotation however many another has queued;
* inside a user's turn, that user's **sessions** take turns by the same three keys
  (`State.session_served`). A session is whatever the client names one (an agent session, a
  terminal); items without one share one session per user. Splitting work into more sessions
  only divides that user's own turns, never takes another user's. One session's own items stay
  FIFO;
* a user at the pool's `per_user_max_running` is skipped until one of theirs ends;
* a `closed` pool admits nothing, and its waiting items keep their places; a `capped` pool
  admits only up to its reservation, as if `max_p` were `R_p`;
* the item whose turn it is blocks its pool when it does not fit: nothing behind it -- its own
  user's or another's -- is admitted around it (no backfill), so a heavy item is never starved
  by lighter ones. Another pool is not blocked by it: the reservations are what protect a pool.

`AdmissionPolicy.standing` is weight that long-lived work outside the ledger already occupies
(a machine's `[[standing]]` entries). `decide` counts it as used before the held leases, and
never as running, so per-user limits and whose turn it is stay a matter of real leases only.

**Memory**, on a host that `AdmissionPolicy.memory` gives a budget (and only there):

* every item counts memory: what it declares (`memory`, in bytes), or, when it declares none,
  its weight times the host's `assumed_memory` per slot (0 when that is not given);
* an item enters only if the memory counted on its host (standing memory, held leases and what
  this decision admitted) plus its own stays within the budget. Like weight, it is never
  preempted to make room, and the item whose turn it is still blocks its pool;
* while `memory_free` names the host (memory is short there), the items one decision admits
  may together count at most that much more.

A host with no budget counts no memory at all: with none given anywhere, every rule above is
the slots' alone, exactly as before memory was counted.

Weight held beyond a pool's `max` or a host's slots is allowed (`leasehold.ledger.Ledger.impose`
records work that was never asked for, such as an observed process): `fits` then fails for that
pool and host until enough of it ends.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from leasehold.model import MACHINE, PoolSet

#: An item on the machine: (scope, item id), the scope being whatever keeps two clients' item
#: ids apart (a repository path, say, or `leasehold` for the daemon's own leases).
ItemKey = tuple[str, str]


@dataclass(frozen=True)
class PoolPolicy:
    host: str
    reserved: int
    max: int
    priority: int = 0
    per_user_max_running: int | None = None


@dataclass(frozen=True)
class AdmissionPolicy:
    """What `decide` reads: each host's slots and each pool's shape, and, optionally, each
    host's memory budget. Every memory field is empty by default: no memory is counted."""

    slots: Mapping[str, int]
    pools: Mapping[str, PoolPolicy]
    #: Pools that admit nothing now (their items keep their place in line): how a machine agent
    #: holds its lend pool shut while the machine is not idle. Empty for a plain client.
    closed: frozenset[str] = frozenset()
    #: Pool name to weight that work outside the ledger already holds. Counted as used before
    #: any lease; never a running count.
    standing: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Pools that admit only up to their `reserved` now, whatever their `max` (items past it
    #: keep their place in line): how a machine agent holds back new work while the machine is
    #: under memory pressure. Empty for a plain client.
    capped: frozenset[str] = frozenset()
    #: Host to the memory, in bytes, that items admitted there may count together. A host not
    #: named here has no memory budget, and no memory is counted on it.
    memory: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Host to the memory, in bytes, one slot of weight counts as when an item declares none.
    #: A host not named here counts such items as 0.
    assumed_memory: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Pool name to memory, in bytes, that work outside the ledger already holds there; counted
    #: on a host with a budget, before any lease.
    standing_memory: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Host to the most memory, in bytes, the items one decision admits there may count
    #: together, on top of the budget: set while memory is short (a machine agent's guard).
    #: A host not named here has no such limit.
    memory_free: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Why `memory_free` is set, in words, for `blocker` to give.
    memory_short: str | None = None

    @classmethod
    def from_pools(
        cls,
        pool_set: PoolSet,
        *,
        closed: frozenset[str] = frozenset(),
        standing: Mapping[str, int] | None = None,
        capped: frozenset[str] = frozenset(),
        memory: int | None = None,
        assumed_memory: int | None = None,
        standing_memory: Mapping[str, int] | None = None,
        memory_free: int | None = None,
        memory_short: str | None = None,
    ) -> AdmissionPolicy:
        """The policy for one machine's `PoolSet`, every pool on host `MACHINE`. `memory` is
        the machine's memory budget in bytes (None: no memory is counted), `assumed_memory`
        what one slot of weight counts as when an item declares none, and `memory_free` the
        most one decision may admit while memory is short (None: no such limit)."""
        return cls(
            slots={MACHINE: pool_set.slots},
            pools={
                name: PoolPolicy(
                    host=MACHINE,
                    reserved=p.reserved,
                    max=p.max,
                    priority=p.priority,
                    per_user_max_running=p.per_user_max_running,
                )
                for name, p in pool_set.pools.items()
            },
            closed=closed,
            standing=dict(standing or {}),
            capped=capped,
            memory={} if memory is None else {MACHINE: memory},
            assumed_memory={} if assumed_memory is None else {MACHINE: assumed_memory},
            standing_memory=dict(standing_memory or {}),
            memory_free={} if memory_free is None else {MACHINE: memory_free},
            memory_short=memory_short,
        )

    def counted(self, host: str, weight: int, memory: int | None) -> int:
        """The memory, in bytes, an item of `weight` declaring `memory` (None: none) counts on
        `host`: 0 where the host has no budget."""
        if host not in self.memory:
            return 0
        if memory is not None:
            return memory
        return weight * self.assumed_memory.get(host, 0)

    def refusal(self, pool: str, weight: int, memory: int | None = None) -> str | None:
        """Why an item of `weight` (declaring `memory` bytes, or None) could never be admitted
        to `pool`, or None if it could: it is below 1, it would not fit `pool` even with
        nothing held but the standing work (whatever is capped now), or the memory it counts
        would not fit its host's budget even then. Such an item would block its pool's line
        forever."""
        policy = self.pools.get(pool)
        if policy is None:
            return f"there is no pool {pool!r}"
        slots = self.slots.get(policy.host, 0)
        if weight < 1:
            return f"weight {weight} is below 1"
        if weight > policy.max:
            return f"weight {weight} is more than pool {pool!r}'s max {policy.max}"
        if weight > slots:
            return f"weight {weight} is more than the {slots} slots there are"
        most = self.heaviest(pool)
        idle = _Tally.of(State(), self)
        if weight <= most:
            return self._memory_refusal(idle, policy.host, weight, memory)
        held = idle.host_used.get(policy.host, 0)
        owed = _owed(idle, self, pool)
        why = [
            *([f"{held} are held by standing work"] if held else []),
            *([f"{owed} are reserved for other pools"] if owed else []),
        ]
        return (
            f"weight {weight} cannot fit pool {pool!r} even on an idle machine, which has room "
            f"for {most}: of the {slots} slots, " + " and ".join(why)
        )

    def heaviest(self, pool: str) -> int:
        """The most weight one item could ever be admitted with in `pool`: what fits it with
        nothing held but the standing work, whatever is capped now. 0 for a pool there is not,
        or one with no room at all."""
        policy = self.pools.get(pool)
        if policy is None:
            return 0
        idle = _Tally.of(State(), self)
        by_pool = policy.max - idle.used.get(pool, 0)
        by_host = (
            self.slots.get(policy.host, 0)
            - idle.host_used.get(policy.host, 0)
            - _owed(idle, self, pool)
        )
        return max(0, min(by_pool, by_host))

    def _memory_refusal(
        self, idle: _Tally, host: str, weight: int, memory: int | None
    ) -> str | None:
        """Why what an item counts could never fit `host`'s memory budget, or None."""
        budget = self.memory.get(host)
        if budget is None:
            return None
        need = self.counted(host, weight, memory)
        if need > budget:
            need_text = _memory_need(self, host, weight, memory)
            return f"{need_text} is more than the {gib(budget)} memory budget"
        held = idle.memory.get(host, 0)
        if held + need > budget:
            return (
                f"{_memory_need(self, host, weight, memory)} cannot fit the {gib(budget)} "
                f"memory budget even on an idle machine: {gib(held)} is held by standing work"
            )
        return None


@dataclass(frozen=True)
class Held:
    pool: str
    host: str
    user: str | None
    weight: int
    session: str | None = None
    #: The memory, in bytes, the item declared; None: it declared none.
    memory: int | None = None


@dataclass(frozen=True)
class Waiting:
    key: ItemKey
    pool: str
    user: str | None
    weight: int
    #: The item's place in its session's FIFO: when it was queued, then the ledger's sequence.
    order: tuple[float, int]
    session: str | None = None
    #: The memory, in bytes, the item declares; None: it declares none.
    memory: int | None = None


#: (pool, user): who has had a turn in which pool.
Turn = tuple[str, str | None]
#: (pool, user, session): which of a user's sessions has had a turn in which pool.
SessionTurn = tuple[str, str | None, str | None]


@dataclass(frozen=True)
class State:
    held: tuple[Held, ...] = ()
    waiting: tuple[Waiting, ...] = ()
    #: When each user last had an item admitted in each pool, as an increasing sequence number;
    #: a user never served there is absent (served first).
    served: Mapping[Turn, int] = field(default_factory=dict)
    #: The same, per session of a user.
    session_served: Mapping[SessionTurn, int] = field(default_factory=dict)


@dataclass
class _Tally:
    used: dict[str, int] = field(default_factory=dict)  # pool -> weight held
    host_used: dict[str, int] = field(default_factory=dict)  # host -> weight held
    running: dict[Turn, int] = field(default_factory=dict)
    session_running: dict[SessionTurn, int] = field(default_factory=dict)
    served: dict[Turn, int] = field(default_factory=dict)
    session_served: dict[SessionTurn, int] = field(default_factory=dict)
    last_turn: int = 0
    memory: dict[str, int] = field(default_factory=dict)  # host -> memory counted
    fresh_memory: dict[str, int] = field(default_factory=dict)  # host -> counted by this decision

    @classmethod
    def of(cls, state: State, policy: AdmissionPolicy | None = None) -> _Tally:
        """`state`'s leases, after any standing weight (which has no user and runs nothing)."""
        tally = cls(
            served=dict(state.served),
            session_served=dict(state.session_served),
            last_turn=max([*state.served.values(), *state.session_served.values()], default=0),
        )
        if policy is not None:
            for pool, weight in policy.standing.items():
                shaped = policy.pools.get(pool)
                if shaped is None or weight < 1:
                    continue
                tally.used[pool] = tally.used.get(pool, 0) + weight
                tally.host_used[shaped.host] = tally.host_used.get(shaped.host, 0) + weight
            for pool, memory in policy.standing_memory.items():
                shaped = policy.pools.get(pool)
                if shaped is None or shaped.host not in policy.memory or memory < 1:
                    continue
                tally.memory[shaped.host] = tally.memory.get(shaped.host, 0) + memory
        for h in state.held:
            counted = 0 if policy is None else policy.counted(h.host, h.weight, h.memory)
            tally.add(h.pool, h.host, h.user, h.session, h.weight, counted)
        return tally

    def add(
        self,
        pool: str,
        host: str,
        user: str | None,
        session: str | None,
        weight: int,
        memory: int = 0,
    ):
        self.used[pool] = self.used.get(pool, 0) + weight
        self.host_used[host] = self.host_used.get(host, 0) + weight
        if memory:
            self.memory[host] = self.memory.get(host, 0) + memory
        self.running[(pool, user)] = self.running.get((pool, user), 0) + 1
        key = (pool, user, session)
        self.session_running[key] = self.session_running.get(key, 0) + 1

    def serve(self, pool: str, user: str | None, session: str | None) -> None:
        self.last_turn += 1
        self.served[(pool, user)] = self.last_turn
        self.session_served[(pool, user, session)] = self.last_turn


def top(policy: AdmissionPolicy, pool: str) -> int:
    """The most `pool` may hold now: its `max`, or its `reserved` while it is capped."""
    p = policy.pools[pool]
    return p.reserved if pool in policy.capped else p.max


def fits(
    tally: _Tally, policy: AdmissionPolicy, pool: str, weight: int, memory: int | None = None
) -> bool:
    """The capacity rules, for `weight` more in `pool` from an item declaring `memory` bytes
    (None: none): the two slot rules, and the memory rules where its host has a budget."""
    host = policy.pools[pool].host
    return _slots_fit(tally, policy, pool, weight) and _memory_fits(
        tally, policy, host, policy.counted(host, weight, memory)
    )


def _slots_fit(tally: _Tally, policy: AdmissionPolicy, pool: str, weight: int) -> bool:
    """The two slot rules alone, for `weight` more in `pool`."""
    p = policy.pools[pool]
    if tally.used.get(pool, 0) + weight > top(policy, pool):
        return False
    free = policy.slots.get(p.host, 0) - tally.host_used.get(p.host, 0) - weight
    return free >= _owed(tally, policy, pool)


def _owed(tally: _Tally, policy: AdmissionPolicy, pool: str) -> int:
    """The slots of `pool`'s host still owed to the other pools there: each one's reservation
    less what it holds."""
    host = policy.pools[pool].host
    return sum(
        max(0, q.reserved - tally.used.get(name, 0))
        for name, q in policy.pools.items()
        if name != pool and q.host == host
    )


def _memory_fits(tally: _Tally, policy: AdmissionPolicy, host: str, need: int) -> bool:
    budget = policy.memory.get(host)
    if budget is None:
        return True
    if tally.memory.get(host, 0) + need > budget:
        return False
    free = policy.memory_free.get(host)
    return free is None or tally.fresh_memory.get(host, 0) + need <= free


def gib(n: int) -> str:
    """`n` bytes in GiB, as status and blockers give memory."""
    return f"{n / 2**30:.1f} GiB"


def _memory_need(policy: AdmissionPolicy, host: str, weight: int, memory: int | None) -> str:
    """What an item counts, and where that comes from, in words."""
    need = policy.counted(host, weight, memory)
    if memory is not None:
        return f"its declared {gib(need)}"
    per_slot = policy.assumed_memory.get(host, 0)
    return f"{gib(need)} (weight {weight} at {gib(per_slot)} a slot, none declared)"


#: A pool's line: user -> session -> that session's items, oldest first.
_Line = dict[str | None, dict[str | None, list[Waiting]]]


def _line(waiting: Iterable[Waiting], pool: str) -> _Line:
    line: _Line = {}
    for item in sorted((w for w in waiting if w.pool == pool), key=lambda w: w.order):
        line.setdefault(item.user, {}).setdefault(item.session, []).append(item)
    return line


_DONE = object()  # nobody has anything left to admit (a user may be None, so not None)


def _next_user(line: _Line, tally: _Tally, pool: str, skip: set[str | None]) -> object:
    """Whose turn it is in `pool`: fewest running, then served longest ago, then oldest head;
    `_DONE` when nobody's."""
    candidates = [u for u, sessions in line.items() if _any(sessions) and u not in skip]
    if not candidates:
        return _DONE
    return min(
        candidates,
        key=lambda u: (
            tally.running.get((pool, u), 0),
            tally.served.get((pool, u), 0),
            min(q[0].order for q in line[u].values() if q),
        ),
    )


def _next_session(
    sessions: dict[str | None, list[Waiting]], tally: _Tally, pool: str, user: str | None
) -> str | None:
    """Which of `user`'s sessions goes next, by the same three keys."""
    return min(
        (s for s, q in sessions.items() if q),
        key=lambda s: (
            tally.session_running.get((pool, user, s), 0),
            tally.session_served.get((pool, user, s), 0),
            sessions[s][0].order,
        ),
    )


def _any(sessions: dict[str | None, list[Waiting]]) -> bool:
    return any(sessions.values())


def _pools_in_order(policy: AdmissionPolicy) -> list[str]:
    return sorted(policy.pools, key=lambda name: (policy.pools[name].priority, name))


def decide(state: State, policy: AdmissionPolicy) -> list[ItemKey]:
    """The waiting items to admit now, in admission order. Waiting items in a pool the policy
    does not have are left waiting; weight held in such a pool still counts against its host.

    `policy.standing` is already occupied when this starts: it reduces what fits, and it does
    not change whose turn it is."""
    tally = _Tally.of(state, policy)
    admits: list[ItemKey] = []
    for pool in _pools_in_order(policy):
        if pool in policy.closed:
            continue
        limit = policy.pools[pool].per_user_max_running
        line = _line(state.waiting, pool)
        at_limit: set[str | None] = set()
        while True:
            user = _next_user(line, tally, pool, at_limit)
            if user is _DONE:
                break
            if limit is not None and tally.running.get((pool, user), 0) >= limit:
                at_limit.add(user)
                continue
            session = _next_session(line[user], tally, pool, user)
            head = line[user][session][0]
            if not fits(tally, policy, pool, head.weight, head.memory):
                break  # no backfill around the item whose turn it is
            line[user][session].pop(0)
            host = policy.pools[pool].host
            counted = policy.counted(host, head.weight, head.memory)
            tally.add(pool, host, user, session, head.weight, counted)
            if counted:
                tally.fresh_memory[host] = tally.fresh_memory.get(host, 0) + counted
            tally.serve(pool, user, session)
            admits.append(head.key)
    return admits


def positions(state: State, policy: AdmissionPolicy) -> dict[ItemKey, int]:
    """Each waiting item's 1-based place in its pool's line: the order `decide` would take
    them in if capacity freed one at a time (per-user limits aside)."""
    tally = _Tally.of(state)
    out: dict[ItemKey, int] = {}
    for pool in sorted({w.pool for w in state.waiting}):
        line = _line(state.waiting, pool)
        place = 0
        while (user := _next_user(line, tally, pool, set())) is not _DONE:
            session = _next_session(line[user], tally, pool, user)
            head = line[user][session].pop(0)
            place += 1
            out[head.key] = place
            tally.running[(pool, user)] = tally.running.get((pool, user), 0) + 1
            key = (pool, user, session)
            tally.session_running[key] = tally.session_running.get(key, 0) + 1
            tally.serve(pool, user, session)
    return out


def blocker(
    state: State,
    policy: AdmissionPolicy,
    key: ItemKey,
    places: Mapping[ItemKey, int] | None = None,
) -> str | None:
    """Why the waiting item `key` is not admitted now, in words, or None when it is not waiting
    (or would be admitted). The first reason that applies, in this order: its pool is closed;
    another item is ahead of it; its user is at the pool's per-user limit; its pool is capped
    to its reservation; memory is short (`memory_free`); its pool is full; the machine's free
    slots are owed to other pools' reservations or are all in use; the memory budget is used.
    `places` is `positions(state, policy)`, for a caller asking about many items."""
    item = next((w for w in state.waiting if w.key == key), None)
    if item is None or item.pool not in policy.pools:
        return None
    pool = policy.pools[item.pool]
    if item.pool in policy.closed:
        return f"pool {item.pool} is closed"
    place = (places if places is not None else positions(state, policy)).get(key, 1)
    if place > 1:
        return f"{place - 1} ahead of it in pool {item.pool}"
    tally = _Tally.of(state, policy)
    if (
        pool.per_user_max_running is not None
        and tally.running.get((item.pool, item.user), 0) >= pool.per_user_max_running
    ):
        return (
            f"its user already runs {pool.per_user_max_running} in pool {item.pool}, "
            "the per-user limit"
        )
    used = tally.used.get(item.pool, 0)
    if item.pool in policy.capped and pool.reserved < used + item.weight <= pool.max:
        return (
            f"the machine is under pressure: pool {item.pool} admits only its "
            f"{pool.reserved} reserved slots and holds {used}; this needs {item.weight}"
        )
    need = policy.counted(pool.host, item.weight, item.memory)
    free_now = policy.memory_free.get(pool.host)
    if pool.host in policy.memory and free_now is not None and need > free_now:
        why = f" ({policy.memory_short})" if policy.memory_short else ""
        return (
            f"memory is short{why}: {gib(free_now)} may start now; this counts "
            f"{_memory_need(policy, pool.host, item.weight, item.memory)}"
        )
    if used + item.weight > pool.max:
        return f"pool {item.pool} holds {used} of {pool.max}; this needs {item.weight}"
    if fits(tally, policy, item.pool, item.weight, item.memory):
        return None
    if _slots_fit(tally, policy, item.pool, item.weight):
        budget = policy.memory[pool.host]
        counted = tally.memory.get(pool.host, 0)
        return (
            f"the memory budget is used: {gib(counted)} of {gib(budget)} counted; this counts "
            f"{_memory_need(policy, pool.host, item.weight, item.memory)}"
        )
    slots = policy.slots.get(pool.host, 0)
    free = slots - tally.host_used.get(pool.host, 0)
    owed = _owed(tally, policy, item.pool)
    if free >= item.weight and owed:
        return f"{free} of {slots} slots are free, but {owed} are reserved for other pools"
    return f"{max(free, 0)} of {slots} slots are free; this needs {item.weight}"
