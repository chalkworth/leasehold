"""The machine-wide ledger: who holds which pool slots on this machine, and who waits.

One SQLite file shared by every client process on the machine (one per kernel: a VM has its
own), so no client gets around the pools by being a separate process.

Every decision is one `BEGIN IMMEDIATE` transaction: reclaim the leases and queue entries of
dead holders, tally what is held per host and pool, ask the pure `leasehold.admission.decide`
which waiting items fit, and turn those into leases. A holder is a process fenced by pid *and*
birth time (`leasehold.process`), so a lease whose process died -- or whose pid was reused by
another process -- is reclaimed by the next transaction, whoever makes it. A pid means something
only in its own pid namespace, and containers can share the file but not one: a holder in
another namespace is judged instead by when it last touched its rows (`beat`, at least every
`STALE_S / 4` from a long-lived holder), and reclaimed once that is `STALE_S` old.

**Leases with a TTL.** A lease or queue entry given a `ttl_s` belongs to whoever renews it, not
to a process: it lives until `expires_at`, which each `renew` pushes `ttl_s` further on, and the
holder's pid is never consulted (etcd-style keepalive -- what a machine agent grants to clients
that talk to it over a socket). A lease without one is fenced by its holder process as above.
Each grant takes the next number of a ledger-wide sequence (`grant`), a fencing token. A
preemptible lease is ended by `revoke`: it keeps its weight while its holder stops (its `renew`
says `revoked` and reaches at most `revoke_deadline`), then is released or expires.

**Transient and rebuildable.** The ledger holds only leases, queue entries and recent submit
times. Clients keep the durable truth (when each item was queued) and re-enqueue from it;
deleting the file loses nothing but the current leases, which their live holders re-acquire.

**Fails closed.** A missing or unwritable directory is `LedgerUnavailable`, never a silent pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from leasehold.admission import (
    AdmissionPolicy,
    Held,
    ItemKey,
    State,
    Waiting,
    blocker,
    decide,
    positions,
)
from leasehold.process import pid_namespace, process_birth, process_matches

FILENAME = "ledger.sqlite3"
#: How many sessions' last turns the ledger remembers (the most recently served).
SESSION_TURNS_KEPT = 4096
SCHEMA_VERSION = 4
#: How far back the create-rate window looks.
RATE_WINDOW_S = 60.0
#: A holder in another pid namespace (another container sharing the ledger file but not a pid
#: namespace) cannot be judged by its pid; it is live while it has touched its rows (`beat`)
#: within this long.
STALE_S = 120.0
_BUSY_TIMEOUT_S = 30.0

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', '{SCHEMA_VERSION}');
INSERT OR IGNORE INTO meta (key, value) VALUES ('grants', '0');
-- An admitted item: the weight it holds in its pool, and the process that holds it.
CREATE TABLE IF NOT EXISTS leases (
    scope TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    user TEXT,
    session TEXT,
    host TEXT NOT NULL,
    pool TEXT NOT NULL,
    weight INTEGER NOT NULL,
    holder_pid INTEGER NOT NULL,
    holder_birth TEXT,
    holder_ns TEXT,
    beat_at REAL NOT NULL,
    acquired_at REAL NOT NULL,
    labels TEXT,
    -- A TTL lease (renewed by its holder, never judged by pid); NULL for a process-fenced one.
    ttl_s REAL,
    expires_at REAL,
    grant_no INTEGER NOT NULL DEFAULT 0,
    revoked_at REAL,
    revoke_reason TEXT,
    revoke_deadline REAL,
    PRIMARY KEY (scope, item_id)
);
-- A waiting item, in its user's FIFO by (queued_at, seq).
CREATE TABLE IF NOT EXISTS queue (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    user TEXT,
    session TEXT,
    pool TEXT NOT NULL,
    weight INTEGER NOT NULL,
    holder_pid INTEGER NOT NULL,
    holder_birth TEXT,
    holder_ns TEXT,
    beat_at REAL NOT NULL,
    queued_at REAL NOT NULL,
    labels TEXT,
    ttl_s REAL,
    expires_at REAL,
    UNIQUE (scope, item_id)
);
-- Whose turn was last in each pool: the round-robin's memory ('' is the item with no user).
CREATE TABLE IF NOT EXISTS turns (
    pool TEXT NOT NULL,
    user TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (pool, user)
);
-- Whose turn was last among each user's sessions ('' is no user, or no session).
CREATE TABLE IF NOT EXISTS session_turns (
    pool TEXT NOT NULL,
    user TEXT NOT NULL,
    session TEXT NOT NULL,
    seq INTEGER NOT NULL,
    PRIMARY KEY (pool, user, session)
);
-- Recent submits, for the per-user create rate (pruned past the window).
CREATE TABLE IF NOT EXISTS submits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT,
    pool TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS submits_by_user ON submits (pool, user, at);
"""


Problem = Literal["missing", "unwritable", "unopenable"]


class LedgerUnavailable(RuntimeError):
    """The ledger cannot be used: its directory is `missing` or `unwritable`, or the file is
    `unopenable` (the `sqlite3.Error` is the `__cause__`). Admission fails closed on it."""

    code = "ledger_unavailable"

    def __init__(self, message: str, *, problem: Problem, path: Path) -> None:
        super().__init__(message)
        self.message = message
        self.problem = problem
        self.path = path


@dataclass(frozen=True)
class Holder:
    """The process a lease or queue entry belongs to: its pid, its birth time and the pid
    namespace both mean something in (this process's, unless said otherwise)."""

    pid: int
    birth: str | None
    ns: str | None = field(default_factory=pid_namespace)

    @classmethod
    def current(cls) -> Holder:
        return cls(pid=os.getpid(), birth=process_birth())

    def judgeable(self) -> bool:
        """Whether this process can tell by pid if the holder is alive: same pid namespace (or
        either unknown)."""
        here = pid_namespace()
        return self.ns is None or here is None or self.ns == here

    def alive(self) -> bool:
        """By pid and birth time; only meaningful when `judgeable`."""
        if self.birth is not None:
            return process_matches(self.pid, self.birth)
        try:  # no birth token on this platform: the pid alone
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


@dataclass(frozen=True)
class LedgerItem:
    """One thing that takes capacity: `weight` slots in `pool`. `scope` keeps different
    clients' `item_id`s apart, and `kind` is a free label (the machine agent writes `work`,
    `external`, `lend` and `observed`); both are stored as given. `session` is the user's
    session the item belongs to, for taking turns between one user's sessions (None: the
    user's shared session). `labels` are free-form (key, value) pairs, stored and listed but
    never read by admission."""

    scope: str
    item_id: str
    kind: str
    user: str | None
    pool: str
    weight: int
    labels: tuple[tuple[str, str], ...] = ()
    session: str | None = None

    @property
    def key(self) -> ItemKey:
        return (self.scope, self.item_id)


@dataclass(frozen=True)
class Admitted:
    pass


@dataclass(frozen=True)
class Queued:
    position: int
    #: Why it is not admitted yet, in words (`leasehold.admission.blocker`).
    blocked: str | None = None


@dataclass(frozen=True)
class Refused:
    reason: str


Outcome = Admitted | Queued | Refused

RenewStatus = Literal["ok", "revoked", "queued", "unknown"]


@dataclass(frozen=True)
class Renewal:
    """What a `renew` found: the lease is `ok` (its expiry pushed on), `revoked` (it still holds
    its weight until `expires_at`, which never passes its revoke deadline: stop and release), a
    place in line kept (`queued`), or `unknown` (expired, released or never granted)."""

    status: RenewStatus
    expires_at: float | None = None
    reason: str | None = None


class Ledger:
    """The machine-wide ledger at `path` (`FILENAME` in a directory the caller owns). The
    directory must exist and be writable: it is never created, since a directory made by the
    wrong user would hide the mistake."""

    def __init__(self, path: Path, *, now=time.time) -> None:
        self.path = Path(path)
        self._now = now
        directory = self.path.parent
        if not directory.is_dir():
            raise LedgerUnavailable(
                f"the ledger directory {directory} does not exist",
                problem="missing",
                path=directory,
            )
        if not os.access(directory, os.W_OK | os.X_OK):
            raise LedgerUnavailable(
                f"the ledger directory {directory} is not writable by uid {os.getuid()}",
                problem="unwritable",
                path=directory,
            )
        try:
            with self._db() as conn:
                _migrate(conn)
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise LedgerUnavailable(
                f"cannot open the ledger {self.path}: {exc}", problem="unopenable", path=self.path
            ) from exc

    # --- connections ---------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=_BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # transient: a lost tail is re-acquired
        conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
        return conn

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # --- the transaction's steps ---------------------------------------------------------------

    def _reclaim(self, conn: sqlite3.Connection) -> None:
        """Drop every lease and queue entry whose holder is no longer that live process: by pid
        and birth time in this pid namespace, by its last `beat` (older than `STALE_S`) in
        another, where its pid means nothing."""
        now = self._now()
        for table in ("leases", "queue"):
            conn.execute(
                f"DELETE FROM {table} WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            rows = conn.execute(
                f"SELECT holder_pid, holder_birth, holder_ns, MAX(beat_at) AS beat FROM {table} "
                "WHERE expires_at IS NULL GROUP BY holder_pid, holder_birth, holder_ns"
            ).fetchall()
            for row in rows:
                holder = Holder(row["holder_pid"], row["holder_birth"], row["holder_ns"])
                live = holder.alive() if holder.judgeable() else now - row["beat"] < STALE_S
                if not live:
                    conn.execute(
                        f"DELETE FROM {table} WHERE expires_at IS NULL AND holder_pid = ? "
                        "AND holder_birth IS ? AND holder_ns IS ?",
                        (holder.pid, holder.birth, holder.ns),
                    )

    @staticmethod
    def _state(conn: sqlite3.Connection) -> State:
        held = tuple(
            Held(
                pool=r["pool"],
                host=r["host"],
                user=r["user"],
                weight=r["weight"],
                session=r["session"],
            )
            for r in conn.execute("SELECT pool, host, user, session, weight FROM leases")
        )
        waiting = tuple(
            Waiting(
                key=(r["scope"], r["item_id"]),
                pool=r["pool"],
                user=r["user"],
                weight=r["weight"],
                order=(r["queued_at"], r["seq"]),
                session=r["session"],
            )
            for r in conn.execute("SELECT * FROM queue")
        )
        served = {
            (r["pool"], r["user"] or None): r["seq"]
            for r in conn.execute("SELECT pool, user, seq FROM turns")
        }
        session_served = {
            (r["pool"], r["user"] or None, r["session"] or None): r["seq"]
            for r in conn.execute("SELECT pool, user, session, seq FROM session_turns")
        }
        return State(held=held, waiting=waiting, served=served, session_served=session_served)

    def _insert_queue(
        self,
        conn: sqlite3.Connection,
        item: LedgerItem,
        holder: Holder,
        queued_at: float | None,
        ttl_s: float | None = None,
    ) -> None:
        """Put `item` in line; one already there keeps its place (and, with `ttl_s`, has its
        expiry pushed on: a client polling for its turn is what keeps it in line)."""
        expires_at = None if ttl_s is None else self._now() + ttl_s
        if ttl_s is not None:
            conn.execute(
                "UPDATE queue SET expires_at = ?, ttl_s = ? WHERE scope = ? AND item_id = ? "
                "AND expires_at IS NOT NULL",
                (expires_at, ttl_s, item.scope, item.item_id),
            )
        conn.execute(
            "INSERT INTO queue (scope, item_id, kind, user, session, pool, weight, holder_pid, "
            "holder_birth, holder_ns, beat_at, queued_at, labels, ttl_s, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (scope, item_id) DO NOTHING",
            (
                item.scope,
                item.item_id,
                item.kind,
                item.user,
                item.session,
                item.pool,
                item.weight,
                holder.pid,
                holder.birth,
                holder.ns,
                self._now(),
                self._now() if queued_at is None else queued_at,
                json.dumps(dict(item.labels)) if item.labels else None,
                ttl_s,
                expires_at,
            ),
        )

    def _admit(self, conn: sqlite3.Connection, policy: AdmissionPolicy) -> list[ItemKey]:
        now = self._now()
        admitted = decide(self._state(conn), policy)
        for scope, item_id in admitted:
            row = conn.execute(
                "SELECT * FROM queue WHERE scope = ? AND item_id = ?", (scope, item_id)
            ).fetchone()
            grant = self._next_grant(conn)
            ttl_s = row["ttl_s"]
            conn.execute(
                "INSERT INTO leases (scope, item_id, kind, user, session, host, pool, weight, "
                "holder_pid, holder_birth, holder_ns, beat_at, acquired_at, labels, ttl_s, "
                "expires_at, grant_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope,
                    item_id,
                    row["kind"],
                    row["user"],
                    row["session"],
                    policy.pools[row["pool"]].host,
                    row["pool"],
                    row["weight"],
                    row["holder_pid"],
                    row["holder_birth"],
                    row["holder_ns"],
                    row["beat_at"],
                    now,
                    row["labels"],
                    ttl_s,
                    None if ttl_s is None else now + ttl_s,
                    grant,
                ),
            )
            conn.execute("DELETE FROM queue WHERE seq = ?", (row["seq"],))
            conn.execute(
                "INSERT INTO turns (pool, user, seq) VALUES (?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) + 1 FROM turns)) "
                "ON CONFLICT (pool, user) DO UPDATE SET seq = excluded.seq",
                (row["pool"], row["user"] or ""),
            )
            conn.execute(
                "INSERT INTO session_turns (pool, user, session, seq) VALUES (?, ?, ?, "
                "(SELECT COALESCE(MAX(seq), 0) FROM turns)) "
                "ON CONFLICT (pool, user, session) DO UPDATE SET seq = excluded.seq",
                (row["pool"], row["user"] or "", row["session"] or ""),
            )
        if admitted:
            # Sessions are often one-off (a CI job, an agent's session): forget the least
            # recently served of those with nothing held or waiting. One that comes back ranks
            # as never served, as the oldest did anyway; one still in line keeps its place.
            conn.execute(
                "DELETE FROM session_turns WHERE seq < (SELECT seq FROM session_turns "
                "ORDER BY seq DESC LIMIT 1 OFFSET ?) AND NOT EXISTS ("
                "SELECT 1 FROM queue q WHERE q.pool = session_turns.pool "
                "AND COALESCE(q.user, '') = session_turns.user "
                "AND COALESCE(q.session, '') = session_turns.session) AND NOT EXISTS ("
                "SELECT 1 FROM leases l WHERE l.pool = session_turns.pool "
                "AND COALESCE(l.user, '') = session_turns.user "
                "AND COALESCE(l.session, '') = session_turns.session)",
                (SESSION_TURNS_KEPT - 1,),
            )
        return admitted

    # --- the API -------------------------------------------------------------------------------

    def enqueue(
        self,
        item: LedgerItem,
        *,
        queued_at: float | None = None,
        holder: Holder | None = None,
        ttl_s: float | None = None,
    ) -> None:
        """Put `item` in line without deciding anything (a no-op if it waits or holds already).
        `queued_at` restores its place from a durable record when a host rebuilds the queue.
        With `ttl_s` its place lasts only while it is renewed (or polled with `try_admit`).
        A weight below 1 is a ValueError."""
        if item.weight < 1:
            raise ValueError(f"weight {item.weight} is below 1")
        holder = holder or Holder.current()
        with self._tx() as conn:
            self._reclaim(conn)
            self._beat(conn, holder)
            if not self._leased(conn, item.key):
                self._insert_queue(conn, item, holder, queued_at, ttl_s)

    def dequeue(self, item: LedgerItem | ItemKey, *, holder: Holder | None = None) -> None:
        """Take a waiting item out of line (cancelled, timed out); a lease is left alone.
        With `holder`, only that holder's entry: one another process queued stays."""
        scope, item_id = item.key if isinstance(item, LedgerItem) else item
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM queue WHERE scope = ? AND item_id = ?"
                + _BY_HOLDER[holder is not None],
                (scope, item_id, *_holder_args(holder)),
            )

    def try_admit(
        self,
        item: LedgerItem,
        policy: AdmissionPolicy,
        *,
        queued_at: float | None = None,
        holder: Holder | None = None,
        ttl_s: float | None = None,
    ) -> Outcome:
        """In one transaction: reclaim the dead, put `item` in line if it is not already, admit
        whatever now fits (anyone's -- a holder learns of its lease on its next call), and say
        where `item` stands. Idempotent: an item that holds a lease is `Admitted` again. A key
        another user holds or waits under is `Refused`: two users never share one lease. So is
        one `policy.refusal` gives a reason for (a weight below 1, or one that could never fit).
        With `ttl_s` the item's place and lease are TTL ones (see the module docstring)."""
        holder = holder or Holder.current()
        with self._tx() as conn:
            self._reclaim(conn)
            self._beat(conn, holder)
            owner = self._owner(conn, item.key)
            if owner is not None and owner != (item.user or ""):
                return Refused(f"{item.key[1]} is another user's lease")
            if self._leased(conn, item.key):
                return Admitted()
            reason = policy.refusal(item.pool, item.weight)
            if reason is not None:
                conn.execute("DELETE FROM queue WHERE scope = ? AND item_id = ?", item.key)
                return Refused(reason)
            self._insert_queue(conn, item, holder, queued_at, ttl_s)
            self._admit(conn, policy)
            if self._leased(conn, item.key):
                return Admitted()
            state = self._state(conn)
            return Queued(positions(state, policy)[item.key], blocker(state, policy, item.key))

    def impose(self, item: LedgerItem, policy: AdmissionPolicy, *, holder: Holder) -> bool:
        """Record a lease for work that is already running and was never asked for (a process
        seen on the machine), without asking `decide`: its weight counts from now on, even past
        its pool's `max` or the machine's slots, and new work waits until enough of it ends.
        `holder` is that process (pid and birth time), so the lease ends when it does (for a
        holder in another pid namespace: once nothing `beat`s for it for `STALE_S`). True
        when a lease was made; False when `item` already holds one or `holder` is not live. An
        `item` waiting in line leaves it. A pool `policy` does not have is a ValueError."""
        if item.pool not in policy.pools:
            raise ValueError(f"there is no pool {item.pool!r}")
        if item.weight < 1:
            raise ValueError(f"weight {item.weight} is below 1")
        with self._tx() as conn:
            self._reclaim(conn)
            if self._leased(conn, item.key):
                self._beat(conn, holder)  # seen again: still running, wherever it runs
                return False
            if holder.judgeable() and not holder.alive():
                return False
            conn.execute("DELETE FROM queue WHERE scope = ? AND item_id = ?", item.key)
            now = self._now()
            grant = self._next_grant(conn)
            conn.execute(
                "INSERT INTO leases (scope, item_id, kind, user, session, host, pool, weight, "
                "holder_pid, holder_birth, holder_ns, beat_at, acquired_at, labels, ttl_s, "
                "expires_at, grant_no) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (
                    item.scope,
                    item.item_id,
                    item.kind,
                    item.user,
                    item.session,
                    policy.pools[item.pool].host,
                    item.pool,
                    item.weight,
                    holder.pid,
                    holder.birth,
                    holder.ns,
                    now,
                    now,
                    json.dumps(dict(item.labels)) if item.labels else None,
                    grant,
                ),
            )
            return True

    @staticmethod
    def _next_grant(conn: sqlite3.Connection) -> int:
        grant = int(conn.execute("SELECT value FROM meta WHERE key = 'grants'").fetchone()[0]) + 1
        conn.execute("UPDATE meta SET value = ? WHERE key = 'grants'", (str(grant),))
        return grant

    def settle(self, policy: AdmissionPolicy) -> list[ItemKey]:
        """Reclaim the dead and admit whatever now fits, for nobody in particular: what a
        process that holds the line open (a machine agent's tick) calls after a release, an expiry
        or a pool reopening. The keys admitted, in order."""
        with self._tx() as conn:
            self._reclaim(conn)
            return self._admit(conn, policy)

    def renew(self, item: LedgerItem | ItemKey, *, ttl_s: float | None = None) -> Renewal:
        """Keep `item`'s TTL lease (or TTL place in line) alive for `ttl_s` more (its own TTL by
        default). A revoked lease is kept only up to its revoke deadline, and says so. A
        process-fenced lease is only beaten. An expired item is reclaimed first: `unknown`."""
        key = item.key if isinstance(item, LedgerItem) else item
        now = self._now()
        with self._tx() as conn:
            self._reclaim(conn)
            row = conn.execute(
                "SELECT ttl_s, expires_at, revoked_at, revoke_reason, revoke_deadline "
                "FROM leases WHERE scope = ? AND item_id = ?",
                key,
            ).fetchone()
            if row is not None:
                ttl = ttl_s if ttl_s is not None else row["ttl_s"]
                if row["expires_at"] is None or ttl is None:
                    conn.execute(
                        "UPDATE leases SET beat_at = ? WHERE scope = ? AND item_id = ?",
                        (now, *key),
                    )
                    expires = row["expires_at"]
                else:
                    expires = now + ttl
                    if row["revoked_at"] is not None:
                        expires = min(expires, row["revoke_deadline"])
                    conn.execute(
                        "UPDATE leases SET expires_at = ?, beat_at = ? "
                        "WHERE scope = ? AND item_id = ?",
                        (expires, now, *key),
                    )
                if row["revoked_at"] is not None:
                    return Renewal("revoked", expires, row["revoke_reason"])
                return Renewal("ok", expires)
            row = conn.execute(
                "SELECT ttl_s, expires_at FROM queue WHERE scope = ? AND item_id = ?", key
            ).fetchone()
            if row is not None:
                ttl = ttl_s if ttl_s is not None else row["ttl_s"]
                expires = row["expires_at"] if row["expires_at"] is None else now + ttl
                conn.execute(
                    "UPDATE queue SET expires_at = ?, beat_at = ? WHERE scope = ? AND item_id = ?",
                    (expires, now, *key),
                )
                return Renewal("queued", expires)
            return Renewal("unknown")

    def revoke(self, *, kind: str, reason: str, grace_s: float) -> list[ItemKey]:
        """Revoke every live lease of `kind` not revoked already: each keeps its weight while
        its holder stops it, for at most `grace_s` plus one TTL, then expires unless released
        sooner. The keys newly revoked."""
        now = self._now()
        with self._tx() as conn:
            self._reclaim(conn)
            rows = conn.execute(
                "SELECT scope, item_id, ttl_s FROM leases WHERE kind = ? AND revoked_at IS NULL",
                (kind,),
            ).fetchall()
            for row in rows:
                deadline = now + grace_s + (row["ttl_s"] or 0.0)
                conn.execute(
                    "UPDATE leases SET revoked_at = ?, revoke_reason = ?, revoke_deadline = ?, "
                    "expires_at = CASE WHEN expires_at IS NULL THEN NULL "
                    "ELSE MIN(expires_at, ?) END WHERE scope = ? AND item_id = ?",
                    (now, reason, deadline, deadline, row["scope"], row["item_id"]),
                )
            return [(row["scope"], row["item_id"]) for row in rows]

    def leases(self) -> list[dict]:
        """Every live lease, oldest first, as plain values (live holders only: reclaims first)."""
        return self._rows("leases", "acquired_at, grant_no")

    def queued(self) -> list[dict]:
        """Every waiting item in the order it was queued (live holders only)."""
        return self._rows("queue", "queued_at, seq")

    def _rows(self, table: str, order: str) -> list[dict]:
        with self._tx() as conn:
            self._reclaim(conn)
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry.pop("seq", None)
            entry["labels"] = json.loads(row["labels"]) if row["labels"] else {}
            out.append(entry)
        return out

    def lines(self, policy: AdmissionPolicy) -> dict[ItemKey, tuple[int, str | None]]:
        """Each waiting item's place in its pool's line and why it is not admitted yet
        (`leasehold.admission.positions` and `blocker`), live holders only."""
        with self._tx() as conn:
            self._reclaim(conn)
            state = self._state(conn)
        places = positions(state, policy)
        return {key: (place, blocker(state, policy, key, places)) for key, place in places.items()}

    def beat(self, holder: Holder | None = None) -> None:
        """Say `holder` (this process by default) is alive: what keeps its leases and places in
        line from being reclaimed by a process in another pid namespace (`STALE_S`). A
        long-lived holder calls it at least every `STALE_S / 4`; `enqueue` and `try_admit` do
        it too."""
        with self._tx() as conn:
            self._beat(conn, holder or Holder.current())

    def _beat(self, conn: sqlite3.Connection, holder: Holder) -> None:
        for table in ("leases", "queue"):
            conn.execute(
                f"UPDATE {table} SET beat_at = ? WHERE holder_pid = ? AND "
                "holder_birth IS ? AND holder_ns IS ?",
                (self._now(), holder.pid, holder.birth, holder.ns),
            )

    def release(self, item: LedgerItem | ItemKey, *, holder: Holder | None = None) -> None:
        """Give back `item`'s lease, or its place in line. With `holder`, only what that holder
        holds: two processes may name an item by the same key, and neither may give back the
        other's lease."""
        scope, item_id = item.key if isinstance(item, LedgerItem) else item
        args = (scope, item_id, *_holder_args(holder))
        with self._tx() as conn:
            for table in ("leases", "queue"):
                conn.execute(
                    f"DELETE FROM {table} WHERE scope = ? AND item_id = ?"
                    + _BY_HOLDER[holder is not None],
                    args,
                )

    def recent_submits(self, user: str | None, pool: str) -> int:
        """How many submits `user` made to `pool` in the last minute (`record_submit` counts
        them)."""
        with self._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM submits WHERE pool = ? AND user IS ? AND at > ?",
                (pool, user, self._now() - RATE_WINDOW_S),
            ).fetchone()[0]

    def record_submit(
        self, user: str | None, pool: str, *, max_queued: int | None, rate_per_min: int | None
    ) -> str | None:
        """The submit-time quota, checked and recorded in one transaction: why `user` may not
        submit another item to `pool` now, or None -- in which case the submit is counted."""
        now = self._now()
        with self._tx() as conn:
            self._reclaim(conn)
            conn.execute("DELETE FROM submits WHERE at <= ?", (now - RATE_WINDOW_S,))
            if max_queued is not None:
                queued = conn.execute(
                    "SELECT COUNT(*) FROM queue WHERE pool = ? AND user IS ?", (pool, user)
                ).fetchone()[0]
                if queued >= max_queued:
                    return (
                        f"{_who(user)} already has {queued} item(s) waiting in pool "
                        f"{pool!r}, its per-user limit of {max_queued}"
                    )
            if rate_per_min is not None:
                recent = conn.execute(
                    "SELECT COUNT(*) FROM submits WHERE pool = ? AND user IS ? AND at > ?",
                    (pool, user, now - RATE_WINDOW_S),
                ).fetchone()[0]
                if recent >= rate_per_min:
                    return (
                        f"{_who(user)} has submitted {recent} item(s) to pool {pool!r} in "
                        f"the last minute, its limit of {rate_per_min} per minute"
                    )
            conn.execute("INSERT INTO submits (user, pool, at) VALUES (?, ?, ?)", (user, pool, now))
            return None

    def snapshot(self) -> dict:
        """Per pool: the weight held, how many items hold it, how many wait, and each user's
        running count. Reclaims the dead first, so it reports only live holders."""
        with self._tx() as conn:
            self._reclaim(conn)
            pools: dict[str, dict] = {}

            def pool(name: str) -> dict:
                return pools.setdefault(
                    name, {"held_weight": 0, "running": 0, "queued": 0, "running_by_user": {}}
                )

            for r in conn.execute("SELECT pool, host, user, weight FROM leases"):
                entry = pool(r["pool"])
                entry["held_weight"] += r["weight"]
                entry["running"] += 1
                user = r["user"] or ""
                entry["running_by_user"][user] = entry["running_by_user"].get(user, 0) + 1
            for r in conn.execute("SELECT pool, COUNT(*) AS n FROM queue GROUP BY pool"):
                pool(r["pool"])["queued"] = r["n"]
        return {"path": str(self.path), "pools": pools}

    def leased(self, item: LedgerItem | ItemKey) -> bool:
        """Whether `item` holds a lease now (by anyone)."""
        key = item.key if isinstance(item, LedgerItem) else item
        with self._db() as conn:
            return self._leased(conn, key)

    @staticmethod
    def _owner(conn: sqlite3.Connection, key: ItemKey) -> str | None:
        """The user ('' for none) holding or waiting under `key`, or None when nobody is."""
        for table in ("leases", "queue"):
            row = conn.execute(
                f"SELECT user FROM {table} WHERE scope = ? AND item_id = ?", key
            ).fetchone()
            if row is not None:
                return row["user"] or ""
        return None

    @staticmethod
    def _leased(conn: sqlite3.Connection, key: ItemKey) -> bool:
        return (
            conn.execute("SELECT 1 FROM leases WHERE scope = ? AND item_id = ?", key).fetchone()
            is not None
        )


#: The extra `WHERE` a holder-scoped delete adds, indexed by "a holder was given".
_BY_HOLDER = ("", " AND holder_pid = ? AND holder_birth IS ? AND holder_ns IS ?")


def _holder_args(holder: Holder | None) -> tuple:
    return () if holder is None else (holder.pid, holder.birth, holder.ns)


def _migrate(conn: sqlite3.Connection) -> None:
    """A ledger from an older schema is dropped and rebuilt: it is transient, its live holders
    re-acquire on their next call."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return  # a new file
    if row is not None and row[0] == str(SCHEMA_VERSION):
        return
    conn.executescript(
        "DROP TABLE IF EXISTS leases; DROP TABLE IF EXISTS queue; "
        "DROP TABLE IF EXISTS turns; DROP TABLE IF EXISTS session_turns; "
        "DROP TABLE IF EXISTS submits; "
        "DROP TABLE IF EXISTS meta;"
    )


def _who(user: str | None) -> str:
    return f"user {user!r}" if user is not None else "the local user"
