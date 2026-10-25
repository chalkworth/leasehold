"""What happened on the machine, newest last: a ring of recent events for `top`, `ui` and
`GET /v1/events?since=N`.

Events are worked out by comparing two snapshots of the ledger (`Snapshot`, `changes`), so a
lease that ends by any path, a release, an expired TTL, a holder that died, another process
writing the same ledger, is reported the same way. Each event is a flat dict:

* `seq` (increasing, never reused while the daemon runs), `time`, `type`;
* for a lease or a place in line: `lease_id`, `kind`, `pool`, `user`, `session`, `weight`
  (a lease's granted size; a waiting item's least), `min_weight` and `max_weight`;
* `type` is an `EventType`: `queued` (with `position`), `granted` (`waited`: it had been in
  line), `left` (gave up its place in line), `released`, `ended` (`reason`: `expired`,
  `holder gone`, or `no longer matched` for an observed process that runs on as something its
  rule does not name), `revoked` (`reason`), `lend_opened` and `lend_closed` (`reason`),
  `pool_max` (`pool`, `max`, `overridden`), `lend_switch` (`open`), `resized` (an elastic
  lease's `weight` changed, from `previous_weight`), `session_started` and `session_ended`
  (`leasehold.sessions`), and `held`, `paused` and `runaway` from the watchdog
  (`leasehold.watchdog`): new heavy work would wait, the tree it would pause, or a tree found
  running away. `would` is true in `watch` mode, where the watchdog reports what it would do
  and acts on nothing.

Events live in memory only: a restarted daemon starts a new sequence, and a reader that asks
for a `since` from before the ring's oldest event is told some were `dropped`; one that asks for
a `since` the daemon has not reached (it restarted) gets everything from the oldest.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class EventType(StrEnum):
    """Every event `type`. Each is a `str` equal to its value, the name clients see."""

    QUEUED = "queued"
    GRANTED = "granted"
    LEFT = "left"
    RELEASED = "released"
    ENDED = "ended"
    REVOKED = "revoked"
    LEND_OPENED = "lend_opened"
    LEND_CLOSED = "lend_closed"
    POOL_MAX = "pool_max"
    LEND_SWITCH = "lend_switch"
    RESIZED = "resized"
    HELD = "held"
    PAUSED = "paused"
    RESUMED = "resumed"
    STOPPED = "stopped"
    RUNAWAY = "runaway"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"


#: How many events the ring keeps.
CAPACITY = 500

_LEASE_FIELDS = (
    "lease_id",
    "kind",
    "pool",
    "user",
    "session",
    "weight",
    "min_weight",
    "max_weight",
)


def _about(row: Mapping) -> dict:
    return {name: row.get(name) for name in _LEASE_FIELDS}


#: A lease or place in line: (ledger scope, lease id). Two scopes may use the same lease id (a
#: client may name its lease like an observed one), and stay two leases.
Key = tuple[str | None, str]


def key(row: Mapping) -> Key:
    return row.get("scope"), row["lease_id"]


@dataclass(frozen=True)
class Snapshot:
    """The leases and the line at one moment, keyed by `Key`, and whether lending is open."""

    leases: Mapping[Key, Mapping] = field(default_factory=dict)
    queue: Mapping[Key, Mapping] = field(default_factory=dict)
    lend_open: bool = False
    lend_reason: str = ""

    @classmethod
    def of(
        cls, leases: Iterable[Mapping], queue: Iterable[Mapping], *, lend_open: bool, reason: str
    ) -> Snapshot:
        return cls(
            leases={key(r): r for r in leases},
            queue={key(r): r for r in queue},
            lend_open=lend_open,
            lend_reason=reason,
        )


def changes(
    before: Snapshot,
    after: Snapshot,
    *,
    now: float,
    released: frozenset[Key] = frozenset(),
    unmatched: frozenset[Key] = frozenset(),
) -> list[dict]:
    """The events that take `before` to `after`: lending, revocations, what ended, what left
    the line, what was resized, then what was granted and what joined the line, so a grant
    follows the release or the shrink that made room for it. `released` names the leases
    given back by request since `before`: those end as `released`, others as `ended`;
    `unmatched` the observed leases given back because their process stopped matching."""
    out: list[dict] = []
    if before.lend_open != after.lend_open:
        kind = EventType.LEND_OPENED if after.lend_open else EventType.LEND_CLOSED
        out.append({"type": kind, "reason": after.lend_reason})
    for k, row in after.leases.items():
        old = before.leases.get(k)
        if old is not None and row.get("revoked") and not old.get("revoked"):
            out.append(
                {"type": EventType.REVOKED, **_about(row), "reason": row.get("revoke_reason")}
            )
    for k, row in before.leases.items():
        if k in after.leases:
            continue
        if k in released:
            out.append({"type": EventType.RELEASED, **_about(row)})
        elif k in unmatched:
            out.append({"type": EventType.ENDED, **_about(row), "reason": "no longer matched"})
        else:
            expires = row.get("expires_at")
            reason = "expired" if expires is not None and expires <= now else "holder gone"
            out.append({"type": EventType.ENDED, **_about(row), "reason": reason})
    for k, row in before.queue.items():
        if k not in after.queue and k not in after.leases:
            out.append({"type": EventType.LEFT, **_about(row)})
    for k, row in after.leases.items():
        old = before.leases.get(k)
        if old is not None and old.get("weight") != row.get("weight"):
            out.append(
                {"type": EventType.RESIZED, **_about(row), "previous_weight": old.get("weight")}
            )
    for k, row in after.leases.items():
        if k not in before.leases:
            out.append({"type": EventType.GRANTED, **_about(row), "waited": k in before.queue})
    for k, row in after.queue.items():
        if k not in before.queue:
            out.append({"type": EventType.QUEUED, **_about(row), "position": row.get("position")})
    return out


class EventLog:
    """The ring. Thread-safe: the daemon's request threads and its tick thread all write."""

    def __init__(self, capacity: int = CAPACITY) -> None:
        self._events: deque[dict] = deque(maxlen=capacity)
        self._next = 1
        self._lock = threading.Lock()

    def emit(self, events: Iterable[dict], *, now: float) -> None:
        with self._lock:
            for event in events:
                self._events.append({"seq": self._next, "time": now, **event})
                self._next += 1

    def since(self, seq: int = 0, limit: int = CAPACITY) -> dict:
        """Events after `seq`, oldest first, at most `limit`: {`events`, `next` (the `since` to
        ask with next time), `dropped` (events after `seq` the ring no longer has)}."""
        with self._lock:
            if seq > self._next - 1:
                seq = 0  # a `since` from before a restart: start again from the oldest
            oldest = self._events[0]["seq"] if self._events else self._next
            chosen = [e for e in self._events if e["seq"] > seq][:limit]
            following = chosen[-1]["seq"] if chosen else max(seq, self._next - 1)
            return {
                "events": [dict(e) for e in chosen],
                "next": following,
                "dropped": seq + 1 < oldest and seq < self._next - 1,
            }
