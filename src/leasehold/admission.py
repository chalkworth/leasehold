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

* every item counts memory: what it declares (`memory`, in bytes, or `memory_per_slot`, which
  may instead be a figure learned from past runs, counted the same way), or, when it declares
  none, its weight times the host's `assumed_memory` per slot (0 when that is not given);
* an item enters only if the memory counted on its host (standing memory, held leases and what
  this decision admitted) plus its own stays within the budget. Like weight, it is never
  preempted to make room, and the item whose turn it is still blocks its pool;
* while `memory_pressing` names the host (memory is under real pressure there), nothing new
  starts on it, whatever it counts;
* while `memory_free` names the host (memory is short there), the items one decision admits
  may together count at most that much more. An item whose turn it is but which fails only
  this allowance (its slots and the budget would take it) waits without blocking its pool:
  the items behind it that fit the allowance may start, while its own session's later items
  keep waiting behind it. An item in `memory_overdue` (one that has waited long enough) does
  not: it blocks its pool as any item whose turn it is does, so a stream of small items in its
  own pool cannot keep it waiting for ever. (Items in other pools on the host may still start
  around it, as with slots alone.)

**The memory net's hold.** While `memory_held` names a host (a memory net has a lease parked
there, or one resuming after a park: `leasehold.net`), nothing new starts on it that counts
memory there, and on a host with no budget, where nothing is counted, nothing new starts at
all. The parked lease is still held, so its memory stays counted, and it is first to run again.
An item held back this way blocks its pool as any item whose turn it is does. No item there is
granted more than its `weight`, and no elastic lease there grows.

A host with no budget counts no memory at all: with none given anywhere, every rule above is
the slots' alone, exactly as before memory was counted.

**Ranges.** A waiting item asks for at least its `weight` and at most its `max_weight` (the
same, unless it says otherwise). `grants` admits in two passes, and the size it grants is the
lease's for its whole life:

1. Every rule above, at each item's `weight` alone: what is admitted, and in which order, is
   exactly what it would be were every `max_weight` its `weight`.
2. Sizing. No item is granted more than its `weight` in a capped pool, or on a host in
   `memory_free` or `memory_pressing`: no extra slot uses the allowance the guard leaves for
   new work. Elsewhere each item admitted in the first pass that asks for more is offered its
   share of `F`, how much more its pool could hold when the decision began: its `max` less
   what it holds, and no more than its host's free slots less those owed to the other pools'
   reservations. The share is a level filled evenly over the items admitted to the pool in
   this decision and those still waiting there, each counted from its `weight` up to its most
   (its `max_weight`, cut by `AdmissionPolicy.most`); an item that reaches its most leaves
   the rest of its share to the others. Extra slots are dealt one at a time, round the items
   in admission order, until each has its offer or its next slot would break one of these
   limits:

   * the slot rules hold with room kept for every item whose turn came in this decision but
     which was not admitted (the one blocking a pool, or one waiting for the memory
     allowance), in every open pool of the host. Its own pool keeps their `weight` free. The
     host keeps free, for each other pool, the more of its unused reservation and what its
     kept-for items could take before anything of that pool's ends (their `weight`, up to the
     room left below the pool's `max`);
   * the pool still has its own `headroom` free after the slot, and the host the largest
     `headroom` of its pools an extra slot could land in (open, not capped, not `observed`,
     and with a headroom below their top), kept for requests that arrive later. Extra slots
     never take another pool's unused reservation either;
   * the memory the slot counts, beside the memory the kept-for items count at their
     `weight` and the host's headroom counts at the larger of `assumed_memory` and the
     slot's own memory, fits the host's budget.

   `alone` is what this gives one item alone on an idle host.

   An item kept for therefore never waits on an extra slot: every extra slot could be given
   back and it would still not fit, or it fits beside them. A closed pool admits nothing, so
   nothing is kept for its items.

An extra slot counts the item's `memory_per_slot` where it declares one, nothing where it
declares a total `memory` (the total does not grow with the size), and the host's
`assumed_memory` otherwise; an item declaring both counts the per-slot size. Refusals and
`heaviest` judge an item by its `weight`.

**Whole hosts.** An item marked `whole` (a maintenance lease) takes its host to itself:

* it is admitted only once its host holds no lease at all (standing weight aside), and is
  granted every slot the host's standing work does not hold (at least 1), whatever its
  `weight`, its pool's `max`, its per-user limit or memory say;
* while it waits, the host drains: no item queued after it there, in any pool, is admitted,
  while the leases running end in their own time. Items queued before it keep their turns
  (`State.waiting` order). Of several whole items on one host, the oldest goes first and the
  others wait behind it;
* nothing else on the host is admitted around it: an item behind it waits without blocking
  anything, and is not kept room for. While one is held, nothing else on its host is admitted,
  whatever its weight leaves free.

Weight held beyond a pool's `max` or a host's slots is allowed (`leasehold.ledger.Ledger.impose`
records work that was never asked for, such as an observed process): `fits` then fails for that
pool and host until enough of it ends.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

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
    #: The slots extra slots of a range leave free in the pool for requests that arrive later;
    #: its host keeps the largest of its pools'. A request's `weight` may still use them.
    headroom: int = 0


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
    #: Hosts where nothing new starts now, whatever it counts: memory is under real pressure
    #: there (a machine agent's guard, while a reading is over a threshold).
    memory_pressing: frozenset[str] = frozenset()
    #: Waiting items that, short only of `memory_free`, no longer let the items behind them
    #: start: they have waited long enough (a machine agent's settle window).
    memory_overdue: frozenset[ItemKey] = frozenset()
    #: Host to the most memory, in bytes, one lease may count there (a machine's `[ceiling]
    #: memory`): a range declaring memory per slot is granted no more slots than fit it. A
    #: host not named here has no such limit.
    lease_memory: Mapping[str, int] = field(default_factory=dict, hash=False)
    #: Pools nothing waits in line for (a machine's observed pool, which records processes
    #: already running): no extra slot ever lands there, so their headroom keeps nothing free
    #: on the host.
    observed: frozenset[str] = frozenset()
    #: What `blocker` says of an item a whole item holds up, in words (who holds the machine
    #: and until when). None: it says only that the machine is in, or draining for,
    #: maintenance.
    maintenance: str | None = None
    #: Host to why the memory net holds new work there, in words: while it is named, nothing
    #: that counts memory there starts (nothing at all on a host with no budget).
    memory_held: Mapping[str, str] = field(default_factory=dict, hash=False)

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
        memory_pressing: bool = False,
        memory_overdue: frozenset[ItemKey] = frozenset(),
        lease_memory: int | None = None,
        observed: frozenset[str] = frozenset(),
        maintenance: str | None = None,
        memory_held: str | None = None,
    ) -> AdmissionPolicy:
        """The policy for one machine's `PoolSet`, every pool on host `MACHINE`. `memory` is
        the machine's memory budget in bytes (None: no memory is counted), `assumed_memory`
        what one slot of weight counts as when an item declares none, and `memory_free` the
        most one decision may admit while memory is short (None: no such limit).
        `memory_pressing` stops every new start on the machine. `memory_overdue` are
        the items that no longer let others by when short. `lease_memory` is the most memory
        one lease may count (None: no such limit), `observed` the pools nothing waits in
        line for, `maintenance` what `blocker` says of an item a whole item holds up, and
        `memory_held` why the memory net holds new work on the machine (None: it does not)."""
        return cls(
            slots={MACHINE: pool_set.slots},
            pools={
                name: PoolPolicy(
                    host=MACHINE,
                    reserved=p.reserved,
                    max=p.max,
                    priority=p.priority,
                    per_user_max_running=p.per_user_max_running,
                    headroom=p.headroom_slots(),
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
            memory_pressing=frozenset({MACHINE}) if memory_pressing else frozenset(),
            memory_overdue=memory_overdue,
            lease_memory={} if lease_memory is None else {MACHINE: lease_memory},
            observed=observed,
            maintenance=maintenance,
            memory_held={} if memory_held is None else {MACHINE: memory_held},
        )

    def held_back(
        self, host: str, weight: int, memory: int | None, memory_per_slot: int | None = None
    ) -> bool:
        """Whether the memory net's hold keeps an item of `weight` declaring `memory` or
        `memory_per_slot` from starting on `host` now: the hold names the host, and the item
        counts memory there, or the host counts none."""
        if host not in self.memory_held:
            return False
        return host not in self.memory or self.counted(host, weight, memory, memory_per_slot) > 0

    def counted(
        self, host: str, weight: int, memory: int | None, memory_per_slot: int | None = None
    ) -> int:
        """The memory, in bytes, an item of `weight` declaring `memory` in all (None: none) or
        `memory_per_slot` (None: none) counts on `host`: 0 where the host has no budget. A
        per-slot size wins over a total (a lease declaring one also records the total it was
        granted, for readers from before memory per slot); with neither, the host's assumed
        memory per slot."""
        if host not in self.memory:
            return 0
        if memory_per_slot is not None:
            return weight * memory_per_slot
        if memory is not None:
            return memory
        return weight * self.assumed_memory.get(host, 0)

    def refusal(
        self,
        pool: str,
        weight: int,
        memory: int | None = None,
        memory_per_slot: int | None = None,
        *,
        whole: bool = False,
    ) -> str | None:
        """Why an item of `weight` (declaring `memory` bytes in all, `memory_per_slot` bytes a
        slot, or neither) could never be admitted
        to `pool`, or None if it could: it is below 1, it would not fit `pool` even with
        nothing held but the standing work (whatever is capped now), or the memory it counts
        would not fit its host's budget even then. Such an item would block its pool's line
        forever. A `whole` item is judged only on its pool being there and its weight being
        at least 1: it waits for an empty host, not for room."""
        policy = self.pools.get(pool)
        if policy is None:
            return f"there is no pool {pool!r}"
        slots = self.slots.get(policy.host, 0)
        if weight < 1:
            return f"weight {weight} is below 1"
        if whole:
            return None
        if weight > policy.max:
            return f"weight {weight} is more than the max {policy.max} of pool {pool!r}"
        if weight > slots:
            return f"weight {weight} is more than the {slots} slots there are"
        most = self._slots_heaviest(pool)
        idle = _Tally.of(State(), self)
        if weight <= most:
            return self._memory_refusal(idle, policy.host, weight, memory, memory_per_slot)
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

    def most(
        self,
        pool: str,
        weight: int,
        max_weight: int | None,
        memory: int | None = None,
        memory_per_slot: int | None = None,
    ) -> int:
        """The most weight an item asking `weight` to `max_weight` (None: `weight`) may be
        granted in `pool` now: `max_weight` cut to what one item could ever be admitted with
        there (`heaviest`) and, with a per-slot size, to the slots whose memory fits
        `lease_memory`; never below `weight`, which is judged on its own. An item declaring no
        memory on a host with a budget counts the assumed size per slot, and is cut the same
        way."""
        most = weight if max_weight is None else max_weight
        if most <= weight:
            return weight
        most = min(most, self.heaviest(pool, memory, memory_per_slot))
        p = self.pools.get(pool)
        limit = None if p is None else self.lease_memory.get(p.host)
        per_slot = memory_per_slot
        if per_slot is None and memory is None and p is not None and p.host in self.memory:
            per_slot = self.assumed_memory.get(p.host)
        if limit is not None and per_slot:
            most = min(most, limit // per_slot)
        return max(weight, most)

    def heaviest(
        self, pool: str, memory: int | None = None, memory_per_slot: int | None = None
    ) -> int:
        """The most weight one item declaring `memory` bytes in all, or `memory_per_slot`
        bytes a slot (None: neither), could ever be admitted with in `pool`: what fits its
        slots with nothing held but the standing work, whatever is capped now, and, on a host
        with a memory budget, whose memory fits beside the standing memory: a declared total
        fits whatever the weight or never (0), a per-slot size, declared or assumed, is cut to
        the weight whose share fits. 0 for a pool there is not, or one where such
        an item could never be admitted (`refusal` gives the reason)."""
        policy = self.pools.get(pool)
        if policy is None:
            return 0
        most = self._slots_heaviest(pool)
        budget = self.memory.get(policy.host)
        if budget is None:
            return most
        held = _Tally.of(State(), self).memory.get(policy.host, 0)
        if memory is not None:
            return most if held + memory <= budget else 0
        per_slot = (
            memory_per_slot
            if memory_per_slot is not None
            else self.assumed_memory.get(policy.host, 0)
        )
        if per_slot <= 0:
            return most
        return max(0, min(most, (budget - held) // per_slot))

    def _slots_heaviest(self, pool: str) -> int:
        """`heaviest` by the slot rules alone."""
        policy = self.pools[pool]
        idle = _Tally.of(State(), self)
        by_pool = policy.max - idle.used.get(pool, 0)
        by_host = (
            self.slots.get(policy.host, 0)
            - idle.host_used.get(policy.host, 0)
            - _owed(idle, self, pool)
        )
        return max(0, min(by_pool, by_host))

    def _memory_refusal(
        self,
        idle: _Tally,
        host: str,
        weight: int,
        memory: int | None,
        memory_per_slot: int | None = None,
    ) -> str | None:
        """Why what an item counts could never fit `host`'s memory budget, or None."""
        budget = self.memory.get(host)
        if budget is None:
            return None
        need = self.counted(host, weight, memory, memory_per_slot)
        need_text = _memory_need(self, host, weight, memory, memory_per_slot)
        if need > budget:
            return f"{need_text} is more than the {gib(budget)} memory budget"
        held = idle.memory.get(host, 0)
        if held + need > budget:
            return (
                f"{need_text} cannot fit the {gib(budget)} "
                f"memory budget even on an idle machine: {gib(held)} is held by standing work"
            )
        return None


@dataclass(frozen=True)
class Held:
    pool: str
    host: str
    user: str | None
    #: The weight the lease was granted.
    weight: int
    session: str | None = None
    #: The memory, in bytes, the item declared in all; None: it declared none.
    memory: int | None = None
    #: The memory, in bytes, the item declared for each slot it holds; None: none.
    memory_per_slot: int | None = None
    #: A whole item's lease (the module docstring's whole hosts).
    whole: bool = False


@dataclass(frozen=True)
class Waiting:
    key: ItemKey
    pool: str
    user: str | None
    #: The least weight the item can be admitted with.
    weight: int
    #: The item's place in its session's FIFO: when it was queued, then the ledger's sequence.
    order: tuple[float, int]
    session: str | None = None
    #: The memory, in bytes, the item declares in all; None: it declares none.
    memory: int | None = None
    #: The most weight the item can use (`grants` sizes it from `weight` up to this); None,
    #: or anything below `weight`, is `weight`.
    max_weight: int | None = None
    #: The memory, in bytes, the item declares for each slot it is granted; None: none.
    memory_per_slot: int | None = None
    #: The item takes its host to itself (the module docstring's whole hosts).
    whole: bool = False
    #: Its `memory_per_slot` was learned from past runs, not declared: only what `blocker` says
    #: differs.
    learned: bool = False

    @property
    def most(self) -> int:
        """The most weight the item can be granted."""
        return self.weight if self.max_weight is None else max(self.weight, self.max_weight)


@dataclass(frozen=True)
class Grant:
    """An item `grants` admits, and the weight it is granted: from its `weight` to its
    `max_weight`, fixed for the life of its lease."""

    key: ItemKey
    weight: int


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
            counted = (
                0
                if policy is None
                else policy.counted(h.host, h.weight, h.memory, h.memory_per_slot)
            )
            tally.add(h.pool, h.host, h.user, h.session, h.weight, counted)
        return tally

    def grow(self, pool: str, host: str, memory: int) -> None:
        """One more slot held in `pool` by an item this decision admitted, counting `memory`
        more."""
        self.used[pool] = self.used.get(pool, 0) + 1
        self.host_used[host] = self.host_used.get(host, 0) + 1
        if memory:
            self.memory[host] = self.memory.get(host, 0) + memory
            self.fresh_memory[host] = self.fresh_memory.get(host, 0) + memory

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
    tally: _Tally,
    policy: AdmissionPolicy,
    pool: str,
    weight: int,
    memory: int | None = None,
    memory_per_slot: int | None = None,
) -> bool:
    """The capacity rules, for `weight` more in `pool` from an item declaring `memory` bytes in
    all or `memory_per_slot` bytes a slot (None: neither): the two slot rules, and the memory
    rules where its host has a budget."""
    host = policy.pools[pool].host
    need = policy.counted(host, weight, memory, memory_per_slot)
    return (
        host not in policy.memory_pressing
        and not policy.held_back(host, weight, memory, memory_per_slot)
        and _slots_fit(tally, policy, pool, weight)
        and _memory_fits(tally, policy, host, need)
    )


def _only_short(tally: _Tally, policy: AdmissionPolicy, pool: str, item: Waiting) -> bool:
    """Whether an item that does not fit fails only the `memory_free` allowance: its slots and
    its host's budget would take it, and its host is not pressing."""
    host = policy.pools[pool].host
    budget = policy.memory.get(host)
    if host in policy.memory_pressing or budget is None or host not in policy.memory_free:
        return False
    if policy.held_back(host, item.weight, item.memory, item.memory_per_slot):
        return False
    need = policy.counted(host, item.weight, item.memory, item.memory_per_slot)
    return (
        _slots_fit(tally, policy, pool, item.weight) and tally.memory.get(host, 0) + need <= budget
    )


def _lets_by(policy: AdmissionPolicy, pool: str, head: Waiting) -> bool:
    """Whether `head`, short only of the allowance, lets the items behind it start: it is not
    `memory_overdue`."""
    return head.key not in policy.memory_overdue


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


def _memory_need(
    policy: AdmissionPolicy,
    host: str,
    weight: int,
    memory: int | None,
    memory_per_slot: int | None = None,
    learned: bool = False,
) -> str:
    """What an item counts, and where that comes from, in words. A `learned` `memory_per_slot`
    came from past runs, not a declaration."""
    need = policy.counted(host, weight, memory, memory_per_slot)
    if memory is not None:
        return f"its declared {gib(need)}"
    if memory_per_slot is not None:
        source = "learned" if learned else "declared"
        return f"{gib(need)} (weight {weight} at its {source} {gib(memory_per_slot)} a slot)"
    per_slot = policy.assumed_memory.get(host, 0)
    return f"{gib(need)} (weight {weight} at {gib(per_slot)} a slot, none declared)"


def _item_need(policy: AdmissionPolicy, host: str, item: Waiting) -> str:
    """`_memory_need` for a waiting item."""
    return _memory_need(
        policy, host, item.weight, item.memory, item.memory_per_slot, learned=item.learned
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


def draining(state: State, policy: AdmissionPolicy) -> dict[str, Waiting]:
    """Per host, the whole item it drains for: the oldest whole item waiting there, in a pool
    the policy has. A host with none is absent."""
    out: dict[str, Waiting] = {}
    for item in sorted(state.waiting, key=lambda w: w.order):
        if item.whole and item.pool in policy.pools:
            out.setdefault(policy.pools[item.pool].host, item)
    return out


def _behind(item: Waiting, drains: Mapping[str, Waiting], policy: AdmissionPolicy) -> bool:
    """Whether `item` waits behind the whole item its host drains for: queued after it."""
    p = policy.pools.get(item.pool)
    first = None if p is None else drains.get(p.host)
    return first is not None and item.key != first.key and item.order > first.order


def _whole_size(policy: AdmissionPolicy, host: str) -> int:
    """The weight a whole item is granted on `host`: every slot its standing work does not
    hold, and at least 1."""
    standing = _Tally.of(State(), policy).host_used.get(host, 0)
    return max(1, policy.slots.get(host, 0) - standing)


def _pools_in_order(policy: AdmissionPolicy) -> list[str]:
    return sorted(policy.pools, key=lambda name: (policy.pools[name].priority, name))


def decide(state: State, policy: AdmissionPolicy) -> list[ItemKey]:
    """The waiting items to admit now, in admission order (`grants`, without the sizes)."""
    return [g.key for g in grants(state, policy)]


def grants(state: State, policy: AdmissionPolicy) -> list[Grant]:
    """The waiting items to admit now, in admission order, each with the weight it is granted.
    Waiting items in a pool the policy does not have are left waiting; weight held in such a
    pool still counts against its host.

    `policy.standing` is already occupied when this starts: it reduces what fits, and it does
    not change whose turn it is."""
    start = _Tally.of(state, policy)
    tally = _Tally.of(state, policy)
    admitted, stopped = _admit_least(state, policy, tally)
    sizes = _sizes(state, policy, start, tally, admitted, stopped)
    return [Grant(item.key, sizes[item.key]) for item in admitted]


def alone(
    policy: AdmissionPolicy,
    pool: str,
    weight: int,
    max_weight: int | None = None,
    memory: int | None = None,
    memory_per_slot: int | None = None,
) -> int:
    """The weight `grants` gives one item asking `weight` to `max_weight` in `pool` on an idle
    host: nothing held but the standing work, the pool open, memory neither short nor under
    pressure, and the memory net holding nothing. `weight` when it would not be admitted even
    then. An agent hook cuts both ends of the range it asks to this."""
    idle = replace(
        policy,
        closed=frozenset(),
        capped=frozenset(),
        memory_free={},
        memory_short=None,
        memory_pressing=frozenset(),
        memory_overdue=frozenset(),
        memory_held={},
    )
    item = Waiting(
        key=("", ""),
        pool=pool,
        user=None,
        weight=weight,
        order=(0.0, 0),
        memory=memory,
        max_weight=max_weight,
        memory_per_slot=memory_per_slot,
    )
    granted = grants(State(waiting=(item,)), idle)
    return granted[0].weight if granted else weight


def _sizes(
    state: State,
    policy: AdmissionPolicy,
    start: _Tally,
    tally: _Tally,
    admitted: list[Waiting],
    stopped: Mapping[str, list[Waiting]],
) -> dict[ItemKey, int]:
    """The weight each of `admitted` is granted (the second pass in the module docstring).
    `start` is the tally before this decision, `tally` after its first pass (grown here), and
    `stopped` each pool's items whose turn came but which were not admitted."""
    sizes = {item.key: item.weight for item in admitted}
    kept = _Kept.of(policy, stopped)
    offers = _offers(state, policy, start, admitted)
    dealing = [item for item in admitted if offers.get(item.key, item.weight) > item.weight]
    while dealing:
        still = []
        for item in dealing:
            host = policy.pools[item.pool].host
            memory = _extra_memory(policy, host, item)
            if not _extra_fits(tally, policy, item.pool, kept, memory):
                continue
            tally.grow(item.pool, host, memory)
            sizes[item.key] += 1
            if sizes[item.key] < offers[item.key]:
                still.append(item)
        dealing = still
    return sizes


def _offers(
    state: State, policy: AdmissionPolicy, start: _Tally, admitted: list[Waiting]
) -> dict[ItemKey, int]:
    """The most each admitted range may be granted: its share of what its pool had free, filled
    level (`_level`), from its `weight` to its most as it could ever be granted
    (`AdmissionPolicy.heaviest`). Its `weight` alone in a capped pool, or on a host where
    memory is short or pressing."""
    keys = {item.key for item in admitted}
    level: dict[str, int] = {}
    offers: dict[ItemKey, int] = {}
    for item in admitted:
        host = policy.pools[item.pool].host
        if item.most == item.weight or item.pool in policy.capped:
            continue
        if host in policy.memory_pressing or host in policy.memory_free:
            continue  # the allowance is for new work's least, never for extra slots
        if host in policy.memory_held:
            continue
        if item.pool not in level:
            waiting = [w for w in state.waiting if w.pool == item.pool and w.key not in keys]
            mine = [w for w in admitted if w.pool == item.pool]
            level[item.pool] = _level(
                _room(start, policy, item.pool),
                [(w.weight, _most(policy, w)) for w in [*mine, *waiting]],
            )
        offers[item.key] = min(_most(policy, item), max(item.weight, level[item.pool]))
    return offers


def _most(policy: AdmissionPolicy, item: Waiting) -> int:
    """The most `item` may be granted now (`AdmissionPolicy.most`)."""
    return policy.most(item.pool, item.weight, item.most, item.memory, item.memory_per_slot)


def _level(free: int, wants: list[tuple[int, int]]) -> int:
    """The highest level `L` at which the items sharing `free` slots, each taking its least,
    its most, or `L` between them (`wants`: (least, most) pairs), take no more than `free`
    together: an item that reaches its most leaves the rest of its share to the others."""

    def taken(level: int) -> int:
        return sum(min(most, max(least, level)) for least, most in wants)

    low, high = 0, max([free, *(most for _, most in wants)])
    while low < high:
        mid = (low + high + 1) // 2
        if taken(mid) <= free:
            low = mid
        else:
            high = mid - 1
    return low


@dataclass(frozen=True)
class _Kept:
    """What extra slots leave for the items whose turn came but which were not admitted: the
    slots per pool, and the memory per host, they count at their `weight`."""

    slots: Mapping[str, int]
    memory: Mapping[str, int]

    @classmethod
    def of(cls, policy: AdmissionPolicy, stopped: Mapping[str, list[Waiting]]) -> _Kept:
        slots: dict[str, int] = {}
        memory: dict[str, int] = {}
        for pool, items in stopped.items():
            host = policy.pools[pool].host
            for item in items:
                slots[pool] = slots.get(pool, 0) + item.weight
                need = policy.counted(host, item.weight, item.memory, item.memory_per_slot)
                memory[host] = memory.get(host, 0) + need
        return cls(slots, memory)


def _extra_fits(
    tally: _Tally,
    policy: AdmissionPolicy,
    pool: str,
    kept: _Kept,
    memory: int,
    *,
    headroom: str = "keep",
) -> bool:
    """Whether one extra slot, counting `memory`, fits `pool` with room left for what `kept`
    names and the pool's and its host's `headroom` (the limits in the module docstring), as
    `headroom` says: "keep" all of it, "leave-one" one slot of each (none where it is 0), "use"
    none, so the slot may take it (an elastic lease gives it back, `leasehold.elastic`)."""
    p = policy.pools[pool]
    pool_headroom = _headroom_left(p.headroom, headroom)
    host_headroom = _headroom_left(_host_headroom(policy, p.host), headroom)
    mine = 1 + kept.slots.get(pool, 0)
    if tally.used.get(pool, 0) + mine + pool_headroom > top(policy, pool):
        return False
    others = sum(
        max(0, q.reserved - tally.used.get(name, 0), _reachable(tally, policy, name, kept))
        for name, q in policy.pools.items()
        if name != pool and q.host == p.host
    )
    spare = mine + host_headroom
    if policy.slots.get(p.host, 0) - tally.host_used.get(p.host, 0) - spare < others:
        return False
    budget = policy.memory.get(p.host)
    if budget is None:
        return True
    # The memory the host's headroom would count, at the assumed size or this slot's if larger,
    # so a request like this one that the headroom keeps room for can start.
    room = host_headroom * max(policy.assumed_memory.get(p.host, 0), memory)
    return tally.memory.get(p.host, 0) + memory + kept.memory.get(p.host, 0) + room <= budget


def _headroom_left(headroom: int, mode: str) -> int:
    """The part of `headroom` an extra slot leaves free under `mode` (`_extra_fits`)."""
    if mode == "use":
        return 0
    if mode == "leave-one":
        return min(1, headroom)
    return headroom


def _reachable(tally: _Tally, policy: AdmissionPolicy, pool: str, kept: _Kept) -> int:
    """The slots on its host the items kept for in `pool` could take before anything of
    `pool`'s own ends: what they need, up to the room left below the pool's top."""
    return min(kept.slots.get(pool, 0), max(0, top(policy, pool) - tally.used.get(pool, 0)))


def _host_headroom(policy: AdmissionPolicy, host: str) -> int:
    """The headroom extra slots leave free on `host`: the largest of its pools' where an extra
    slot could land now. A closed, capped or observed pool takes none, and neither does one
    whose headroom leaves it no room below its top."""
    return max(
        (
            q.headroom
            for name, q in policy.pools.items()
            if q.host == host
            and name not in policy.closed
            and name not in policy.capped
            and name not in policy.observed
            and q.headroom < top(policy, name)
        ),
        default=0,
    )


def _room(tally: _Tally, policy: AdmissionPolicy, pool: str) -> int:
    """How much more `pool` could hold by the slot rules, as `tally` stands."""
    p = policy.pools[pool]
    by_pool = top(policy, pool) - tally.used.get(pool, 0)
    by_host = (
        policy.slots.get(p.host, 0) - tally.host_used.get(p.host, 0) - _owed(tally, policy, pool)
    )
    return max(0, min(by_pool, by_host))


def _extra_memory(policy: AdmissionPolicy, host: str, item: Waiting) -> int:
    """The memory one slot past an item's `weight` counts on `host`: its own per-slot size,
    nothing beside a declared total, else the host's assumed size (0 without a budget)."""
    if host not in policy.memory:
        return 0
    if item.memory is not None:
        return 0
    if item.memory_per_slot is not None:
        return item.memory_per_slot
    return policy.assumed_memory.get(host, 0)


def _admit_least(
    state: State, policy: AdmissionPolicy, tally: _Tally
) -> tuple[list[Waiting], dict[str, list[Waiting]]]:
    """The first pass: every rule at each item's `weight`, `tally` grown by what it admits.
    The items admitted, in order, and per pool the items whose turn came but which were not
    admitted (the one blocking the pool, and those waiting for the memory allowance)."""
    admits: list[Waiting] = []
    stopped: dict[str, list[Waiting]] = {}
    drains = draining(state, policy)
    taken = {h.host for h in state.held if h.whole}
    # Whole items go in once their host is empty, below; what waits behind one, or on a host one
    # holds, stays put.
    lines = [
        w
        for w in state.waiting
        if not w.whole
        and not _behind(w, drains, policy)
        and (w.pool not in policy.pools or policy.pools[w.pool].host not in taken)
    ]
    for pool in _pools_in_order(policy):
        if pool in policy.closed:
            continue
        limit = policy.pools[pool].per_user_max_running
        line = _line(lines, pool)
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
            if not fits(tally, policy, pool, head.weight, head.memory, head.memory_per_slot):
                stopped.setdefault(pool, []).append(head)
                if _only_short(tally, policy, pool, head) and _lets_by(policy, pool, head):
                    # It waits for the allowance; its session waits behind it, the rest may
                    # start if they fit what is free.
                    del line[user][session]
                    continue
                break  # no backfill around the item whose turn it is
            line[user][session].pop(0)
            host = policy.pools[pool].host
            counted = policy.counted(host, head.weight, head.memory, head.memory_per_slot)
            tally.add(pool, host, user, session, head.weight, counted)
            if counted:
                tally.fresh_memory[host] = tally.fresh_memory.get(host, 0) + counted
            tally.serve(pool, user, session)
            admits.append(head)
    for host, first in drains.items():
        busy = any(h.host == host for h in state.held) or any(
            policy.pools[a.pool].host == host for a in admits
        )
        if busy:
            continue
        first = replace(first, weight=_whole_size(policy, host), max_weight=None)
        counted = policy.counted(host, first.weight, first.memory, first.memory_per_slot)
        tally.add(first.pool, host, first.user, first.session, first.weight, counted)
        tally.serve(first.pool, first.user, first.session)
        admits.append(first)
    return admits, stopped


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
    (or would be admitted). The first reason that applies, in this order: a whole item holds
    its host (`policy.maintenance`); it waits behind the whole item its host drains for (the
    same); it is that whole item, and leases still run there; its pool is closed;
    another item is ahead of it; its user is at the pool's per-user limit; its pool is capped
    to its reservation; memory is under pressure (`memory_pressing`), held by the memory net
    (`memory_held`) or short (`memory_free`);
    its pool is full; the machine's free
    slots are owed to other pools' reservations or are all in use; the memory budget is used.
    `places` is `positions(state, policy)`, for a caller asking about many items."""
    item = next((w for w in state.waiting if w.key == key), None)
    if item is None or item.pool not in policy.pools:
        return None
    pool = policy.pools[item.pool]
    drains = draining(state, policy)
    if any(h.whole and h.host == pool.host for h in state.held):
        return policy.maintenance or "the machine is in maintenance"
    if _behind(item, drains, policy):
        return policy.maintenance or "the machine is draining for maintenance"
    if item.whole:
        running = sum(1 for h in state.held if h.host == pool.host)
        if running == 0:
            return None
        leases = "lease" if running == 1 else "leases"
        return f"draining: waiting for the {running} {leases} running to end"
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
            f"{pool.reserved} reserved slots and holds {used}; this needs "
            f"{_needs(policy, item)}"
        )
    if pool.host in policy.memory_pressing:
        why = f" ({policy.memory_short})" if policy.memory_short else ""
        return f"memory is short{why}: nothing new starts until the pressure eases"
    if policy.held_back(pool.host, item.weight, item.memory, item.memory_per_slot):
        return f"the memory net holds new work: {policy.memory_held[pool.host]}"
    need = policy.counted(pool.host, item.weight, item.memory, item.memory_per_slot)
    free_now = policy.memory_free.get(pool.host)
    if pool.host in policy.memory and free_now is not None and need > free_now:
        why = f" ({policy.memory_short})" if policy.memory_short else ""
        return (
            f"memory is short{why}: {gib(free_now)} may start now; this counts "
            f"{_item_need(policy, pool.host, item)}"
        )
    if used + item.weight > pool.max:
        return f"pool {item.pool} holds {used} of {pool.max}; this needs {_needs(policy, item)}"
    if fits(tally, policy, item.pool, item.weight, item.memory, item.memory_per_slot):
        return None
    if _slots_fit(tally, policy, item.pool, item.weight):
        budget = policy.memory[pool.host]
        counted = tally.memory.get(pool.host, 0)
        return (
            f"the memory budget is used: {gib(counted)} of {gib(budget)} counted; this counts "
            f"{_item_need(policy, pool.host, item)}"
        )
    slots = policy.slots.get(pool.host, 0)
    free = slots - tally.host_used.get(pool.host, 0)
    owed = _owed(tally, policy, item.pool)
    if free >= item.weight and owed:
        return f"{free} of {slots} slots are free, but {owed} are reserved for other pools"
    return f"{max(free, 0)} of {slots} slots are free; this needs {_needs(policy, item)}"


def _needs(policy: AdmissionPolicy, item: Waiting) -> str:
    """What an item needs, in words: its weight, or for a range the least and the most it
    could be granted now (`AdmissionPolicy.most`)."""
    most = _most(policy, item)
    if most == item.weight:
        return str(item.weight)
    return f"at least {item.weight} (asks up to {most})"
