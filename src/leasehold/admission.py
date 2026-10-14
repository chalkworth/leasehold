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
    """What `decide` reads: each host's slots and each pool's shape."""

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

    @classmethod
    def from_pools(
        cls,
        pool_set: PoolSet,
        *,
        closed: frozenset[str] = frozenset(),
        standing: Mapping[str, int] | None = None,
        capped: frozenset[str] = frozenset(),
    ) -> AdmissionPolicy:
        """The policy for one machine's `PoolSet`, every pool on host `MACHINE`."""
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
        )

    def refusal(self, pool: str, weight: int) -> str | None:
        """Why an item of `weight` could never be admitted to `pool`, or None if it could: it
        is below 1, or it would not fit `pool` even with nothing held but the standing work
        (whatever is capped now). Such an item would block its pool's line forever."""
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
        if weight <= most:
            return None
        idle = _Tally.of(State(), self)
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


@dataclass(frozen=True)
class Held:
    pool: str
    host: str
    user: str | None
    weight: int
    session: str | None = None


@dataclass(frozen=True)
class Waiting:
    key: ItemKey
    pool: str
    user: str | None
    weight: int
    #: The item's place in its session's FIFO: when it was queued, then the ledger's sequence.
    order: tuple[float, int]
    session: str | None = None


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
        for h in state.held:
            tally.add(h.pool, h.host, h.user, h.session, h.weight)
        return tally

    def add(self, pool: str, host: str, user: str | None, session: str | None, weight: int):
        self.used[pool] = self.used.get(pool, 0) + weight
        self.host_used[host] = self.host_used.get(host, 0) + weight
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


def fits(tally: _Tally, policy: AdmissionPolicy, pool: str, weight: int) -> bool:
    """The two capacity rules, for `weight` more in `pool`."""
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
            if not fits(tally, policy, pool, head.weight):
                break  # no backfill around the item whose turn it is
            line[user][session].pop(0)
            tally.add(pool, policy.pools[pool].host, user, session, head.weight)
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
    to its reservation; its pool is full;
    the machine's free slots are owed to other pools' reservations or are all in use.
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
    if used + item.weight > pool.max:
        return f"pool {item.pool} holds {used} of {pool.max}; this needs {item.weight}"
    if fits(tally, policy, item.pool, item.weight):
        return None
    slots = policy.slots.get(pool.host, 0)
    free = slots - tally.host_used.get(pool.host, 0)
    owed = _owed(tally, policy, item.pool)
    if free >= item.weight and owed:
        return f"{free} of {slots} slots are free, but {owed} are reserved for other pools"
    return f"{max(free, 0)} of {slots} slots are free; this needs {item.weight}"
