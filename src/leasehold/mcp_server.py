"""`leasehold mcp`: the machine and its leases for a coding agent, as an MCP server on stdio.

Five tools:

* `machine_status`: free slots, each pool, who waits and why, pressure and lending, in one
  paragraph that ends with a recommendation, plus the same numbers as structured content.
* `acquire_lease`: ask for a lease; it answers granted or queued within 20 s.
* `wait_for_capacity`: wait up to 20 s more for a queued lease.
* `release_lease`: give back a lease this server made.
* `list_my_leases`: what this server holds and has waiting.

No tool runs a command, on purpose. A lease tells the agent how much of the machine it may use;
the agent runs its work with its own shell tool, so leasehold never executes anything a model
asks for. An agent that wants a command tied to a lease can run `leasehold hold -- cmd` itself.

Leases end with the session. Every lease id this server makes is `mcp.<hex>`, labelled
`agent=mcp` (and `reason=` when one is given), on a TTL of `DEFAULT_TTL_S` that one background
thread renews every third of it. When the server exits -- the agent closes stdin, or SIGTERM or
SIGINT arrives -- it releases everything it holds. Killed outright, it stops renewing, and each
lease ends one TTL later. A revoked lease (only `lend` leases are ever revoked) is answered as
revoked, with the advice to stop its work and release it, never as granted.

A place in line is kept for `QUEUE_GRACE_S` after the agent's last call naming it
(`acquire_lease` with `lease_id`, or `wait_for_capacity`); the background thread renews it
meanwhile, so the agent may do other work between waits. The daemon grants a waiting item as
soon as it fits, call or no call: such a lease is held and handed over on the agent's next call
about it. A place, or a grant the agent has not been told of, that sees no call for
`QUEUE_GRACE_S` is given back, so a request the agent forgot never sits on the machine's slots.

The handlers (`machine_status`, `acquire_lease`, `wait_for_capacity`, `release_lease`,
`list_my_leases`) are plain functions: each returns an `Answer` (text for the model and a dict
of the key facts) or raises `ToolFailure`. `serve` registers them with the MCP SDK's `MCPServer`;
the SDK (the `mcp` extra) is imported there and nowhere else.
"""

import contextlib
import copy
import os
import secrets
import signal
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from leasehold import __version__
from leasehold.client import ApiError, Client, Unavailable
from leasehold.hold import waiting_line

#: The longest one tool call waits for a lease: MCP calls stay short.
MAX_WAIT_S = 20.0
#: The longest one ask to the daemon waits; a tool call's wait is a series of them, so shutdown
#: waits on the daemon for at most one ask.
ASK_S = 1.0
#: How long past its own wait one ask may take to be answered before the daemon counts as
#: unreachable; a tool call ends within its wait plus this, whatever the daemon does.
ASK_MARGIN_S = 1.0
#: The TTL of every lease and place in line this server asks for.
DEFAULT_TTL_S = 30.0
#: How long a place in line, or a grant not yet handed over, is kept without a call about it.
QUEUE_GRACE_S = 120.0
#: PSI `some avg10` (percent) from which the status warns that the machine is slow.
HIGH_PRESSURE = 25.0
#: How many ended lease ids the server remembers, to say why one is gone.
_ENDED_KEPT = 100
#: How many waiting items the status names; the rest are counted.
_WAITING_NAMED = 5

INSTRUCTIONS = """\
leasehold shares this machine fairly between people, CI and agents. Before heavy work (a full \
test suite, a build, a benchmark), call machine_status or acquire_lease with the weight the work \
needs (about one slot per parallel job), run the work with your own shell tool using the job \
count the grant gives, then call release_lease. These tools never run commands. Leases are \
released when this session ends."""


class ToolFailure(Exception):
    """A tool call that cannot be answered; its message is what the model is told."""


@dataclass(frozen=True)
class Answer:
    """What a tool gives the model: `text` to read, and `data`, the same facts structured."""

    text: str
    data: dict


@dataclass
class Held:
    """A lease or place in line this server made."""

    lease_id: str
    kind: str
    pool: str | None
    weight: int
    reason: str
    #: `queued` or `granted`.
    state: str = "queued"
    #: Whether the agent has been told of the grant.
    claimed: bool = False
    #: The agent's last call about it (the server's monotonic clock).
    seen_at: float = 0.0
    #: The lease as the daemon last answered it, once granted.
    lease: dict = field(default_factory=dict)
    #: Why a held lease is ending, when the daemon revoked it.
    revoked: str | None = None

    def public(self) -> dict:
        out = {
            "lease_id": self.lease_id,
            "state": self.state,
            "kind": self.kind,
            "pool": self.lease.get("pool", self.pool),
            "weight": self.weight,
            "reason": self.reason,
        }
        if self.state == "granted":
            out["claimed"] = self.claimed
            out["jobs"] = self.lease.get("jobs")
            out["revoked"] = self.revoked
            if self.revoked:
                out["state"] = "revoked"
        return out


def unreachable(exc: Exception) -> str:
    """The tool error for a daemon that does not answer."""
    return (
        f"{exc}. Is the leasehold daemon running? Start it with `leasehold serve`, or run "
        "`leasehold doctor` to see which socket a client finds and why."
    )


class Leases:
    """The leases and places in line one server made, kept alive by one background thread
    (`start`) and all released by `close`. Safe to use from several threads."""

    def __init__(
        self,
        client: Client,
        *,
        session: str | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        grace_s: float = QUEUE_GRACE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        #: The daemon session every lease is filed under: this server's own unless one is given,
        #: so one agent's requests take turns with each other, not with the user's other work.
        self.session = session or f"mcp.{secrets.token_hex(4)}"
        self.ttl_s = ttl_s
        self.grace_s = grace_s
        self._clock = clock
        self._lock = threading.Lock()
        #: Notified when the renewer should look again (a new item, `close`) and when an ask to
        #: the daemon ends.
        self._changed = threading.Condition(self._lock)
        self._items: dict[str, Held] = {}
        self._ended: OrderedDict[str, str] = OrderedDict()
        self._thread: threading.Thread | None = None
        self._closed = False
        #: Asks to the daemon in flight (`asking`).
        self._asking = 0
        #: Ids to release that nobody tracks any more (`abandon`).
        self._abandoned: list[str] = []

    # --- the agent's side ------------------------------------------------------------------

    def new(self, *, kind: str, pool: str | None, weight: int, reason: str) -> Held:
        """A new request, tracked from now on (so a failed or slow first ask is still renewed
        and released)."""
        held = Held(f"mcp.{secrets.token_hex(8)}", kind, pool, weight, reason)
        with self._lock:
            if self._closed:
                raise ToolFailure("this leasehold MCP server is shutting down")
            held.seen_at = self._clock()
            self._items[held.lease_id] = held
            self._changed.notify_all()
        return held

    @contextlib.contextmanager
    def asking(self):
        """Around each acquire sent to the daemon: refused with a `ToolFailure` once `close` has
        begun, and waited for by `close`, so no ask can put back what its sweep releases."""
        with self._lock:
            if self._closed:
                raise ToolFailure("this leasehold MCP server is shutting down")
            self._asking += 1
        try:
            yield
        finally:
            with self._lock:
                self._asking -= 1
                self._changed.notify_all()

    def touch(self, lease_id: str) -> Held:
        """The item `lease_id`, marked as asked about now. A `ToolFailure` that says why for an
        id this server did not make or no longer holds."""
        with self._lock:
            held = self._items.get(lease_id)
            if held is not None:
                held.seen_at = self._clock()
                return held
            why = self._ended.get(lease_id)
        if why is not None:
            raise ToolFailure(f"{lease_id} is no longer held: {why}")
        raise ToolFailure(
            f"{lease_id} is not a lease this MCP server made; it only acts on its own leases "
            "(see list_my_leases)"
        )

    def request(self, held: Held) -> dict:
        """The acquire body for `held`, without `wait_s`."""
        labels = {"agent": "mcp"} | ({"reason": held.reason} if held.reason else {})
        return {
            "kind": held.kind,
            "pool": held.pool,
            "weight": held.weight,
            "labels": labels,
            "lease_id": held.lease_id,
            "session": self.session,
            "ttl_s": self.ttl_s,
        }

    def answered(self, held: Held, lease: dict | None) -> str | None:
        """Record the daemon's answer to the agent's call: `lease` once granted, None while
        queued. Why the lease is revoked, if the answer or the renewer (before or during the
        call) says it is; None otherwise. A `ToolFailure` if `held` stopped being tracked
        during the call (the server is closing, or the place was given back): its lease is
        then released."""
        with self._lock:
            tracked = self._items.get(held.lease_id) is held
            if tracked:
                held.seen_at = self._clock()
                if lease is not None:
                    held.state, held.claimed, held.lease = "granted", True, lease
                    if lease.get("revoked") and not held.revoked:
                        held.revoked = lease.get("revoke_reason") or "revoked"
            why = self._ended.get(held.lease_id, "it was given back")
            revoked = held.revoked
        if not tracked:
            self.release_quietly(held.lease_id)
            raise ToolFailure(f"{held.lease_id} is no longer held: {why}")
        return revoked

    def abandon(self, held: Held) -> None:
        """Stop tracking `held`, a request the agent never learnt of, and have the renewer (or
        `close`) release whatever the daemon made of it, off the caller's thread."""
        with self._lock:
            if self._forget(held.lease_id, "the request failed") is None:
                return
            if not self._closed:
                self._abandoned.append(held.lease_id)
                self._changed.notify_all()
                return
        self.release_quietly(held.lease_id)  # closing: no renewer is left to do it

    def forget(self, lease_id: str, why: str) -> Held | None:
        """Stop tracking `lease_id`, remembering why; the item, if it was still tracked. An id
        already forgotten keeps its first reason."""
        with self._lock:
            return self._forget(lease_id, why)

    def _forget(self, lease_id: str, why: str) -> Held | None:
        held = self._items.pop(lease_id, None)
        if held is not None or lease_id not in self._ended:
            self._ended[lease_id] = why
            self._ended.move_to_end(lease_id)
            while len(self._ended) > _ENDED_KEPT:
                self._ended.popitem(last=False)
        return held

    def items(self) -> list[Held]:
        with self._lock:
            return list(self._items.values())

    # --- the renewing side -----------------------------------------------------------------

    def _deadline(self, held: Held) -> float | None:
        """When `held` is given back unless the agent asks about it: places in line and grants
        the agent has not taken. None for a lease the agent holds."""
        if held.state == "queued" or not held.claimed:
            return held.seen_at + self.grace_s
        return None

    def expire(self) -> None:
        """Give back every place in line, and grant not yet taken, whose grace is up."""
        now = self._clock()
        stale = []
        with self._lock:
            abandoned, self._abandoned = self._abandoned, []
            for held in list(self._items.values()):
                deadline = self._deadline(held)
                if deadline is not None and now >= deadline:
                    what = "place in line" if held.state == "queued" else "grant"
                    why = (
                        f"its {what} was given back after {self.grace_s:g} s without a call "
                        "about it; ask again with acquire_lease"
                    )
                    stale.append(self._forget(held.lease_id, why))
        for lease_id in [*abandoned, *(held.lease_id for held in stale)]:
            self.release_quietly(lease_id)

    def renew_all(self) -> None:
        """One pass: give back what the agent stopped asking about (`expire`), renew everything
        else, and record what the daemon says changed (a grant, a revocation, a lease it lost)."""
        self.expire()
        for held in self.items():
            try:
                answer = self.client.renew(held.lease_id, ttl_s=self.ttl_s)
            except Unavailable:
                continue  # a restarting daemon keeps leases for their TTL: try again next pass
            except ApiError as exc:
                self.forget(held.lease_id, f"the daemon refused to renew it ({exc})")
                continue
            status = answer.get("status")
            if status == "unknown":
                self.forget(
                    held.lease_id,
                    "the daemon no longer has it (it expired, or was released elsewhere)",
                )
                continue
            with self._lock:
                if status == "ok" and held.state == "queued":
                    held.state, held.claimed = "granted", False
                elif status == "revoked":
                    held.revoked = answer.get("reason") or "revoked"

    def start(self) -> None:
        """From a daemon thread until `close`: renew every TTL/3, and give each place in line
        back as its grace ends rather than at the next renewal."""
        if self._thread is not None:
            return
        interval = max(0.05, self.ttl_s / 3)

        def run() -> None:
            next_renewal = self._clock() + interval
            while True:
                with self._lock:
                    if self._closed:
                        return
                    deadlines = [self._deadline(h) for h in self._items.values()]
                    wake = min([next_renewal, *(d for d in deadlines if d is not None)])
                    if self._abandoned:
                        wake = self._clock()
                    self._changed.wait(max(0.0, wake - self._clock()))
                    if self._closed:
                        return
                if self._clock() >= next_renewal:
                    self.renew_all()
                    next_renewal = self._clock() + interval
                else:
                    self.expire()

        self._thread = threading.Thread(target=run, name="leasehold-mcp-renew", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop renewing, refuse new asks, wait (a little) for those in flight, then release
        everything this server holds or has waiting. Idempotent."""
        with self._lock:
            self._closed = True
            self._changed.notify_all()
            deadline = time.monotonic() + ASK_S + ASK_MARGIN_S  # the longest one ask takes
            while self._asking and (left := deadline - time.monotonic()) > 0:
                self._changed.wait(left)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=self.client.timeout)
        with self._lock:  # one step, so nothing abandoned meanwhile falls between the two
            ids, self._abandoned = self._abandoned, []
            for held in list(self._items.values()):
                self._forget(held.lease_id, "this MCP server exited")
                ids.append(held.lease_id)
        for lease_id in ids:
            self.release_quietly(lease_id)

    def release_quietly(self, lease_id: str) -> None:
        """Release `lease_id` if the daemon answers; otherwise it ends at its TTL."""
        with contextlib.suppress(Unavailable, ApiError):
            self.client.release(lease_id)


# --- the tools ---------------------------------------------------------------------------------


def _wait(wait_s: float) -> float:
    return min(MAX_WAIT_S, max(0.0, float(wait_s)))


def _revoked(held: Held) -> Answer:
    text = (
        f"Revoked {held.lease_id}: {held.revoked}. Do not start work under it; stop the work it "
        f"was for now, then call release_lease with this lease_id."
    )
    return Answer(text, {"status": "revoked"} | held.public())


def _bounded(client: Client) -> Client:
    """`client`, answering each request within its wait plus `ASK_MARGIN_S` or raising
    `Unavailable`."""
    short = copy.copy(client)
    short.timeout = ASK_MARGIN_S
    return short


def _ask(leases: Leases, held: Held, wait_s: float) -> Answer:
    """Ask the daemon for `held` (again), waiting up to `wait_s`, and say how it stands, within
    `wait_s` plus `ASK_MARGIN_S` whatever the daemon does. A revoked lease is answered as
    revoked, never as granted."""
    if held.revoked:
        return _revoked(held)
    request = leases.request(held)
    client = _bounded(leases.client)
    deadline = time.monotonic() + _wait(wait_s)
    answer = None
    try:
        # Short asks, each checked against `close`, make up the wait. None starts after the
        # deadline (but the first), and none waits past it; each one's socket timeout is its
        # wait plus the margin, so the call ends by the deadline plus the margin.
        while answer is None or answer["status"] == "queued":
            left = deadline - time.monotonic()
            if answer is not None and left <= 0:
                break
            with leases.asking():
                answer = client.acquire(**request, wait_s=min(ASK_S, max(0.0, left)))
    except Unavailable as exc:
        raise ToolFailure(unreachable(exc)) from None
    except ApiError as exc:
        leases.forget(held.lease_id, f"the daemon refused it ({exc})")
        leases.release_quietly(held.lease_id)
        raise ToolFailure(f"leasehold refused the request: {exc}") from None
    status = answer["status"]
    if status == "granted":
        lease = answer["lease"]
        if leases.answered(held, lease):
            return _revoked(held)
        jobs = lease.get("jobs") or held.weight
        text = (
            f"Granted {held.lease_id}: weight {lease['weight']} in pool {lease['pool']}, "
            f"{jobs} parallel job{'s' if jobs != 1 else ''} (for example `make -j{jobs}`). "
            "This server renews it until you call release_lease or the session ends; release "
            "it as soon as the work is done."
        )
        return Answer(text, {"status": "granted"} | held.public())
    if status == "queued":
        leases.answered(held, None)
        line = waiting_line(request, answer)
        text = (
            f"Queued {held.lease_id}: {line.removeprefix('waiting for ')}. Call "
            f"wait_for_capacity with this lease_id to keep waiting, or do other work first: the "
            f"place is kept for {leases.grace_s:g} s after each call about it."
        )
        data = {"status": "queued"} | held.public()
        data |= {"position": answer.get("position"), "blocked": answer.get("blocked")}
        return Answer(text, data)
    reason = answer.get("reason") or "refused"
    leases.forget(held.lease_id, f"refused: {reason}")
    data = {"status": "refused", "lease_id": held.lease_id, "reason": reason}
    return Answer(f"Refused: {reason}. Nothing is held.", data)


def acquire_lease(
    leases: Leases,
    *,
    weight: int = 1,
    pool: str | None = None,
    kind: str = "work",
    reason: str = "",
    wait_s: float = 10,
    lease_id: str | None = None,
) -> Answer:
    """Ask for a lease of `weight` slots, waiting up to `wait_s` (at most `MAX_WAIT_S`).
    With `lease_id`, ask again for that earlier request of this server's, as first made (the
    other arguments are then ignored): the same as `wait_for_capacity`."""
    if lease_id is not None:
        return _ask(leases, leases.touch(lease_id), wait_s)
    if weight < 1:
        raise ToolFailure("weight is at least 1")
    held = leases.new(kind=kind, pool=pool, weight=weight, reason=reason.strip()[:200])
    try:
        return _ask(leases, held, wait_s)
    except ToolFailure:
        # The agent never learnt of this id: whatever the daemon made of it is given back by
        # the renewer (or `close`), so a daemon that does not answer never delays this call.
        leases.abandon(held)
        raise


def wait_for_capacity(leases: Leases, lease_id: str, wait_s: float = 15) -> Answer:
    """Wait up to `wait_s` (at most `MAX_WAIT_S`) for this server's queued `lease_id`, keeping
    its place; an already granted one is answered at once."""
    return _ask(leases, leases.touch(lease_id), wait_s)


def release_lease(leases: Leases, lease_id: str) -> Answer:
    """Release this server's `lease_id`, held or waiting. Refuses ids it did not make."""
    held = leases.touch(lease_id)
    try:
        leases.client.release(lease_id)
    except Unavailable as exc:
        raise ToolFailure(unreachable(exc)) from None
    except ApiError as exc:
        raise ToolFailure(f"leasehold refused the release: {exc}") from None
    leases.forget(lease_id, "released")
    what = "lease" if held.state == "granted" else "place in line"
    return Answer(f"Released {lease_id} ({what}).", {"status": "released", "lease_id": lease_id})


def list_my_leases(leases: Leases) -> Answer:
    """What this server holds and has waiting, as it last heard from the daemon."""
    items = [h.public() for h in leases.items()]
    if not items:
        return Answer("This session holds no leases and waits for none.", {"leases": []})
    parts = []
    for item in items:
        where = f"weight {item['weight']} in {item['pool'] or item['kind']}"
        if item["state"] == "revoked":
            state = f"revoked ({item['revoked']}): stop the work it was for and release it"
        elif item["state"] == "granted" and not item["claimed"]:
            state = "granted while you were away: call wait_for_capacity to take it"
        elif item["state"] == "granted":
            state = f"granted, {item['jobs'] or item['weight']} jobs"
        else:
            state = "waiting in line"
        why = f" for {item['reason']}" if item["reason"] else ""
        parts.append(f"{item['lease_id']} ({where}{why}): {state}")
    return Answer("This session's leases: " + "; ".join(parts) + ".", {"leases": items})


def machine_status(client: Client) -> Answer:
    """The machine's state in one paragraph an agent can act on (`describe`)."""
    try:
        return describe(client.status())
    except Unavailable as exc:
        raise ToolFailure(unreachable(exc)) from None
    except ApiError as exc:
        raise ToolFailure(f"leasehold answered with an error: {exc}") from None


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _fits(st: dict, name: str) -> int:
    """The heaviest lease pool `name` would grant now, before anything already waiting."""
    pools = st["pools"]
    pool = pools.get(name)
    if pool is None or pool["closed"] or pool["queued"]:
        return 0
    unmet = sum(max(0, p["reserved"] - p["held_weight"]) for n, p in pools.items() if n != name)
    return max(0, min(pool["max"] - pool["held_weight"], st["slots"] - st["held"] - unmet))


def describe(st: dict) -> Answer:
    """The status object (`GET /v1/status`) as one short paragraph and its key numbers. The
    recommendation is about a `work` lease, the kind an agent asks for by default."""
    slots, held = st["slots"], st["held"]
    free = max(0, slots - held)
    pools = {
        name: {
            "held": p["held_weight"],
            "max": p["max"],
            "queued": p["queued"],
            "closed": p["closed"],
        }
        for name, p in st["pools"].items()
    }
    waiting = [
        {k: x.get(k) for k in ("lease_id", "kind", "pool", "weight", "position", "blocked")}
        for x in st["queue"]
    ]
    pressure_raw = (st.get("load") or {}).get("pressure") or {}
    pressure = {
        r: (pressure_raw[r]["some"]["avg10"] if pressure_raw.get(r) else None)
        for r in ("cpu", "memory", "io")
    }
    lend = st["lend"]
    fits = _fits(st, "work")

    sentences = [f"{st['machine']['name']}: {free} of {_plural(slots, 'slot')} free"]
    pool_parts = []
    for name, p in pools.items():
        part = f"{name} {p['held']}/{p['max']}"
        if p["queued"]:
            part += f" ({p['queued']} queued)"
        if p["closed"]:
            part += " (closed)"
        pool_parts.append(part)
    sentences[0] += "; pools held/max: " + ", ".join(pool_parts) + "."
    if waiting:
        named = [
            f"{x['lease_id']} (weight {x['weight']} in {x['pool']}"
            + (f": {x['blocked']})" if x["blocked"] else ")")
            for x in sorted(waiting, key=lambda x: x["position"] or 0)[:_WAITING_NAMED]
        ]
        more = len(waiting) - len(named)
        tail = f", and {more} more" if more > 0 else ""
        sentences.append(f"Waiting: {', '.join(named)}{tail}.")
    if any(v is not None for v in pressure.values()):
        psi = ", ".join(f"{r} {v:g}%" for r, v in pressure.items() if v is not None)
        sentences.append(f"Pressure (PSI avg10): {psi}.")
    else:
        sentences.append("Pressure is unknown (no PSI).")
    sentences.append(
        f"Lending is {'open' if lend['eligible'] else 'closed'}"
        + (f" ({lend['reason']})." if lend.get("reason") else ".")
    )
    hot = [r for r, v in pressure.items() if v is not None and v >= HIGH_PRESSURE]
    if fits:
        advice = (
            f"{_plural(free, 'slot')} free: a work lease of weight up to {fits} would start now."
        )
    elif waiting:
        advice = (
            f"The machine is full and {len(waiting)} {'is' if len(waiting) == 1 else 'are'} "
            "waiting; prefer light work, or queue with acquire_lease and do other work meanwhile."
        )
    else:
        advice = "The machine is full; a new lease would wait in line. Prefer light work."
    if hot:
        psi = ", ".join(f"{r} {pressure[r]:g}%" for r in hot)
        advice += f" Pressure is high ({psi}): expect slow runs."
    sentences.append(advice)
    data = {
        "machine": st["machine"]["name"],
        "slots": slots,
        "held": held,
        "free": free,
        "work_fits_now": fits,
        "pools": pools,
        "waiting": waiting,
        "pressure": pressure,
        "lend_open": bool(lend["eligible"]),
        "lend_reason": lend.get("reason"),
    }
    return Answer(" ".join(sentences), data)


# --- the server ----------------------------------------------------------------------------


class MissingExtra(ImportError):
    """The MCP SDK (the `mcp` extra, mcp 2 or later) is not installed."""


def serve(client: Client, *, session: str | None = None) -> None:
    """Serve the tools on stdin/stdout until stdin closes, then release every lease this server
    made and return. On SIGTERM or SIGINT, release them and end the process (status 0) without
    returning. `MissingExtra` without the MCP SDK."""
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except ImportError as exc:
        raise MissingExtra(str(exc)) from exc

    leases = Leases(client, session=session)
    server = MCPServer("leasehold", instructions=INSTRUCTIONS, version=__version__)

    def result(call: Callable[[], Answer]) -> CallToolResult:
        try:
            answer = call()
        except ToolFailure as exc:
            raise ToolError(str(exc)) from None
        return CallToolResult(
            content=[TextContent(type="text", text=answer.text)], structured_content=answer.data
        )

    looking = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    leasing = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @server.tool(name="machine_status", annotations=looking)
    def machine_status_tool() -> CallToolResult:
        """How busy this machine is: free slots, each pool, who waits and why, pressure and
        lending, ending in a recommendation. Call it before heavy work."""
        return result(lambda: machine_status(client))

    @server.tool(name="acquire_lease", annotations=leasing)
    def acquire_lease_tool(
        weight: int = 1,
        pool: str | None = None,
        kind: str = "work",
        reason: str = "",
        wait_s: float = 10,
        lease_id: str | None = None,
    ) -> CallToolResult:
        """Ask for a share of the machine before heavy work. `weight` is the slots it needs,
        about one per parallel job. Waits up to `wait_s` (at most 20) and answers granted (with
        the job count to use) or queued (with the place in line and why). Leave `pool` and
        `kind` unset unless told otherwise. Pass a queued `lease_id` to keep waiting for it.
        Runs nothing: run the work yourself, then call release_lease."""
        return result(
            lambda: acquire_lease(
                leases,
                weight=weight,
                pool=pool,
                kind=kind,
                reason=reason,
                wait_s=wait_s,
                lease_id=lease_id,
            )
        )

    waiting = (
        f"Wait up to `wait_s` seconds (at most {MAX_WAIT_S:g}) for a queued lease to be "
        "granted, keeping its place. Call it again while it stays queued; a place nobody asks "
        f"about for {QUEUE_GRACE_S:g} s is given back."
    )

    @server.tool(name="wait_for_capacity", description=waiting, annotations=leasing)
    def wait_for_capacity_tool(lease_id: str, wait_s: float = 15) -> CallToolResult:
        return result(lambda: wait_for_capacity(leases, lease_id, wait_s))

    @server.tool(name="release_lease", annotations=leasing)
    def release_lease_tool(lease_id: str) -> CallToolResult:
        """Give back a lease (or a place in line) from acquire_lease, as soon as the work it
        was for is done."""
        return result(lambda: release_lease(leases, lease_id))

    @server.tool(name="list_my_leases", annotations=looking)
    def list_my_leases_tool() -> CallToolResult:
        """The leases this session holds and the requests it has waiting."""
        return result(lambda: list_my_leases(leases))

    previous = signal.signal(signal.SIGTERM, _interrupt)
    leases.start()
    interrupted = False
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        interrupted = True
    finally:
        leases.close()
        signal.signal(signal.SIGTERM, previous)
    if interrupted:
        # The SDK reads stdin on a worker thread that no signal interrupts and the interpreter
        # would wait for at exit: with the leases released, end the process here.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def _interrupt(signum, frame) -> None:
    # SIGTERM ends the server the way Ctrl-C does, through `serve`'s cleanup.
    raise KeyboardInterrupt
