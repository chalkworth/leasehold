"""Elastic leases in the ledger, the daemon and `hold`: a lease is offered free slots after its
hold-down and takes them with `POST /v1/resize`, is asked back when a request waits for them and
gives them back, and `hold --jobserver` moves its jobserver's tokens and its scope with it."""

from __future__ import annotations

import array
import fcntl
import os
import sqlite3
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from support import CHILD_ENV, FACTS, Clock, FakeLoad, machine_config, until
from test_jobs import _make_version

from leasehold import api, contain, events, follow, jobs
from leasehold import hold as hold_module
from leasehold import ledger as ledger_module
from leasehold.admission import AdmissionPolicy
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.cli import views
from leasehold.client import ApiError, Client
from leasehold.hold import lease_request
from leasehold.ledger import Admitted, Holder, Ledger, LedgerItem, Queued
from leasehold.machine import MachineConfig
from leasehold.model import Pool, PoolSet
from leasehold.server import Daemon
from leasehold.status import public
from leasehold.top.render import _weight_cell

FOUR = AdmissionPolicy.from_pools(
    PoolSet(slots=4, pools={"work": Pool(reserved=0, max=4, headroom=0)})
)


def item(name, weight=1, most=None, *, elastic=False, user="ann") -> LedgerItem:
    return LedgerItem("s", name, "work", user, "work", weight, max_weight=most, elastic=elastic)


def sizes(ledger: Ledger, name: str) -> tuple[int, int]:
    renewal = ledger.renew(("s", name))
    return renewal.weight, renewal.target


# --- the ledger ---------------------------------------------------------------------------------


def test_an_elastic_lease_is_offered_free_slots_after_its_hold_down_and_takes_them(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "l.sqlite3", now=clock, settle_s=10)
    assert ledger.try_admit(item("other", 2), FOUR, ttl_s=100) == Admitted()
    assert ledger.try_admit(item("e", 1, 4, elastic=True), FOUR, ttl_s=100) == Admitted()
    assert sizes(ledger, "e") == (2, 2)
    ledger.release(("s", "other"))
    ledger.settle(FOUR)
    assert sizes(ledger, "e") == (2, 2)  # granted just now: it settles first
    clock.now += 10
    ledger.settle(FOUR)
    assert sizes(ledger, "e") == (2, 3)  # one slot a decision
    assert ledger.resize(("s", "e"), 4, FOUR).status == "refused"  # past its target
    done = ledger.resize(("s", "e"), 3, FOUR)
    assert (done.status, done.weight, done.target) == ("resized", 3, 3)
    ledger.settle(FOUR)
    assert sizes(ledger, "e") == (3, 3)  # resized just now: it settles again
    clock.now += 10
    ledger.settle(FOUR)
    assert ledger.resize(("s", "e"), 4, FOUR).weight == 4
    assert [r["weight"] for r in ledger.leases()] == [4]  # what any reader sees


def test_a_growth_is_refused_once_the_slot_it_was_offered_is_taken(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    ledger.try_admit(item("other", 2), FOUR, ttl_s=100)
    ledger.try_admit(item("e", 1, 4, elastic=True), FOUR, ttl_s=100)
    ledger.release(("s", "other"))
    ledger.settle(FOUR)
    assert sizes(ledger, "e") == (2, 3)
    assert ledger.try_admit(item("new", 2, user="bob"), FOUR, ttl_s=100) == Admitted()
    done = ledger.resize(("s", "e"), 3, FOUR)
    assert (done.status, done.weight) == ("refused", 2)
    assert sizes(ledger, "e") == (2, 2)


def test_a_growth_is_checked_again_when_its_stored_target_is_stale(tmp_path):
    # Work recorded without a decision (an observed process, or a process from before elastic
    # leases) leaves the stored target as it was: the resize itself checks the slot is free.
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    ledger.try_admit(item("other", 2), FOUR, ttl_s=100)
    ledger.try_admit(item("e", 1, 4, elastic=True), FOUR, ttl_s=100)
    ledger.release(("s", "other"))
    ledger.settle(FOUR)
    assert sizes(ledger, "e") == (2, 3)
    seen = LedgerItem("o", "seen", "observed", "bob", "work", 2)
    assert ledger.impose(seen, FOUR, holder=Holder.current())
    assert sizes(ledger, "e") == (2, 3)
    done = ledger.resize(("s", "e"), 3, FOUR)
    assert (done.status, done.weight, done.target) == ("refused", 2, 2)
    assert sum(r["weight"] for r in ledger.leases()) == 4


def test_the_first_in_line_hears_it_waits_for_slots_being_given_back(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    ledger.try_admit(item("e", 1, 4, elastic=True), FOUR, ttl_s=100)
    waits = ledger.try_admit(item("w", 2, user="bob"), FOUR, ttl_s=100)
    giving = "waiting for an elastic lease to give back 2 slots as its jobs finish"
    assert isinstance(waits, Queued) and waits.blocked.endswith(giving)
    behind = ledger.try_admit(item("v", 1, user="cat"), FOUR, ttl_s=100)
    assert isinstance(behind, Queued) and giving not in (behind.blocked or "")
    lines = ledger.lines(FOUR)
    assert lines[("s", "w")][1].endswith(giving) and giving not in (lines[("s", "v")][1] or "")
    ledger.resize(("s", "e"), 2, FOUR)
    assert ledger.leased(("s", "w"))


def test_a_refused_growth_leaves_the_other_leases_targets_as_they_were(tmp_path):
    shape = {
        "a": Pool(reserved=0, max=3, headroom=0),
        "b": Pool(reserved=0, max=8, headroom=0, priority=1),
    }
    policy = AdmissionPolicy.from_pools(PoolSet(slots=8, pools=shape))

    def ranged(name, pool, weight, most=None, user="ann"):
        return LedgerItem(
            "s", name, "work", user, pool, weight, max_weight=most, elastic=most is not None
        )

    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    for x in (ranged("xa", "a", 1), ranged("A", "a", 1, 3), ranged("xb", "b", 3)):
        ledger.try_admit(x, policy, ttl_s=100)
    ledger.try_admit(ranged("B", "b", 1, 4, user="bob"), policy, ttl_s=100)
    assert (sizes(ledger, "A"), sizes(ledger, "B")) == ((2, 2), (2, 2))
    ledger.release(("s", "xa"))
    ledger.release(("s", "xb"))
    ledger.settle(policy)
    assert (sizes(ledger, "A"), sizes(ledger, "B")) == ((2, 3), (2, 3))
    # Pool a fills without a decision: A's growth is refused there, B's offer stands.
    seen = LedgerItem("o", "seen", "observed", "cat", "a", 1)
    assert ledger.impose(seen, policy, holder=Holder.current())
    assert ledger.resize(("s", "A"), 3, policy).status == "refused"
    assert (sizes(ledger, "A"), sizes(ledger, "B")) == ((2, 2), (2, 3))
    assert ledger.resize(("s", "B"), 3, policy).weight == 3


def test_a_held_range_is_made_elastic_and_its_target_worked_out(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    ledger.try_admit(item("other", 2), FOUR, ttl_s=100)
    ledger.try_admit(item("r", 1, 4), FOUR, ttl_s=100)
    ledger.release(("s", "other"))
    ledger.settle(FOUR)
    assert sizes(ledger, "r") == (2, 2)  # a plain range keeps its size
    done = ledger.make_elastic(("s", "r"), FOUR)
    assert (done.status, done.weight, done.target) == ("resized", 2, 3)
    assert ledger.renew(("s", "r")).elastic
    ledger.try_admit(item("fixed", 1), FOUR, ttl_s=100)
    with pytest.raises(ValueError, match="no range"):
        ledger.make_elastic(("s", "fixed"), FOUR)
    assert ledger.make_elastic(("s", "gone"), FOUR).status == "unknown"


def test_a_waiting_request_lowers_the_target_and_a_shrink_admits_it(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    ledger.try_admit(item("e", 1, 4, elastic=True), FOUR, ttl_s=100)
    assert sizes(ledger, "e") == (4, 4)
    assert isinstance(ledger.try_admit(item("w", 2, user="bob"), FOUR, ttl_s=100), Queued)
    assert sizes(ledger, "e") == (4, 2)
    assert not ledger.leased(("s", "w"))  # nothing is taken from a lease: it gives back
    done = ledger.resize(("s", "e"), 2, FOUR)
    assert (done.status, done.weight, done.target) == ("resized", 2, 2)
    assert ledger.leased(("s", "w"))  # admitted in the same transaction


def test_a_fixed_lease_or_a_size_outside_the_range_is_refused(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock())
    ledger.try_admit(item("fixed", 1), FOUR, ttl_s=100)
    with pytest.raises(ValueError, match="not elastic"):
        ledger.resize(("s", "fixed"), 2, FOUR)
    ledger.try_admit(item("e", 2, 3, elastic=True), FOUR, ttl_s=100)
    with pytest.raises(ValueError, match="outside the lease's range 2-3"):
        ledger.resize(("s", "e"), 1, FOUR)
    assert ledger.resize(("s", "gone"), 2, FOUR).status == "unknown"
    with pytest.raises(ValueError, match="only a range"):
        item("e", 2, 2, elastic=True)


def test_a_per_slot_lease_records_the_total_of_its_new_size(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock(), settle_s=0)
    per_slot = LedgerItem(
        "s", "e", "work", "ann", "work", 1, max_weight=4, memory_per_slot=2**30, elastic=True
    )
    ledger.try_admit(per_slot, FOUR, ttl_s=100)
    ledger.try_admit(item("w", 3, user="bob"), FOUR, ttl_s=100)
    ledger.resize(("s", "e"), 1, FOUR)
    [row] = [r for r in ledger.leases() if r["item_id"] == "e"]
    assert (row["weight"], row["memory"]) == (1, 2**30)  # what a 0.3 reader counts


def test_a_ledger_from_before_elastic_leases_gains_their_columns_in_place(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    old_schema = ledger_module._SCHEMA
    for line in ("elastic INTEGER,", "target INTEGER,", "resized_at REAL,"):
        old_schema = old_schema.replace(f"    {line}\n", "")
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO leases (scope, item_id, kind, user, session, host, pool, weight, "
        "holder_pid, holder_birth, holder_ns, beat_at, acquired_at, labels, ttl_s, expires_at, "
        "grant_no, min_weight, max_weight) VALUES ('s', 'held', 'work', 'ann', NULL, 'machine', "
        "'work', 2, 1, NULL, NULL, 0, 0, NULL, 1e12, 1e12, 1, 1, 4)"
    )
    conn.commit()
    assert "elastic" not in {r[1] for r in conn.execute("PRAGMA table_info(leases)")}
    conn.close()
    ledger = Ledger(path, settle_s=0)
    renewal = ledger.renew(("s", "held"))
    assert (renewal.weight, renewal.target, renewal.elastic) == (2, 2, False)
    ledger.settle(FOUR)
    assert ledger.renew(("s", "held")).target == 2  # a range from before: fixed


# --- the daemon ---------------------------------------------------------------------------------


def elastic_config(tmp, sock, **overrides):
    """Four slots, all `work`'s, no headroom, and the shortest hold-down, a second."""
    return machine_config(
        tmp,
        socket=str(sock),
        ceiling={"slots": 4},
        pools={
            "work": {"reserved": 0, "max": 4, "headroom": 0},
            "external": {"reserved": 0, "max": 1, "priority": 1},
            "lend": {"reserved": 0, "max": 1, "priority": 2},
            "observed": {"reserved": 0, "max": 4, "priority": 3},
        },
        elastic={"settle_s": 1},
        **overrides,
    )


@pytest.fixture
def four(tmp_path, sock_dir):
    config = elastic_config(tmp_path, sock_dir / "s.sock")
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        yield d
    finally:
        d.stop()


def target(client: Client, lease_id: str) -> int:
    return client.renew(lease_id)["target"]


def test_the_daemon_offers_slots_takes_them_back_and_says_so(four):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="other", ttl_s=60)
    granted = client.acquire(
        kind="work", weight=1, max_weight=4, elastic=True, lease_id="e", ttl_s=60
    )
    lease = granted["lease"]
    assert (lease["weight"], lease["target"], lease["elastic"]) == (2, 2, True)
    assert granted["elastic"] == {"settle_s": 1.0, "headroom": "keep"}
    assert lease["max_jobs"] == 4
    client.release("other")
    until(lambda: target(client, "e") == 3)
    renewal = client.renew("e")
    assert (renewal["weight"], renewal["target_jobs"], renewal["elastic"]) == (2, 3, True)
    grown = client.resize("e", 3)
    assert (grown["status"], grown["weight"], grown["jobs"]) == ("resized", 3, 3)
    [held] = client.status()["leases"]
    assert (held["weight"], held["elastic"]) == (3, True)
    resized = [e for e in client.events()["events"] if e["type"] == "resized"]
    assert [(e["weight"], e["previous_weight"]) for e in resized] == [(3, 2)]
    # A request that waits lowers the target; giving the slots back lets it in.
    until(lambda: target(client, "e") == 4)
    client.resize("e", 4)
    assert client.acquire(kind="work", weight=2, lease_id="w", ttl_s=60)["status"] == "queued"
    until(lambda: target(client, "e") == 2)
    assert client.resize("e", 2)["weight"] == 2
    assert client.acquire(kind="work", weight=2, lease_id="w", ttl_s=60)["status"] == "granted"


def test_resize_answers_409_when_not_now_and_400_for_a_fixed_lease(four):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="fixed", ttl_s=60)
    client.acquire(kind="work", weight=1, max_weight=4, elastic=True, lease_id="e", ttl_s=60)
    with pytest.raises(ApiError) as above:
        client.resize("e", 4)  # it holds 2 of 4 beside the other's 2; its target is 2
    assert above.value.status == 409 and above.value.payload["error"] == "conflict"
    with pytest.raises(ApiError) as gone:
        client.resize("nobody", 2)
    assert gone.value.status == 409
    with pytest.raises(ApiError) as fixed:
        client.resize("fixed", 1)
    assert fixed.value.status == 400 and "not elastic" in fixed.value.payload["detail"]


def test_the_daemon_makes_a_held_range_elastic(four):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="other", ttl_s=60)
    client.acquire(kind="work", weight=1, max_weight=4, lease_id="r", ttl_s=60)
    answer = client.make_elastic("r")
    assert (answer["status"], answer["elastic"], answer["weight"]) == ("elastic", True, 2)
    with pytest.raises(ApiError) as gone:
        client.make_elastic("nobody")
    assert gone.value.status == 409
    client.release("other")
    until(lambda: target(client, "r") == 3, timeout=20)
    client.acquire(kind="work", weight=1, lease_id="fixed", ttl_s=60)
    with pytest.raises(ApiError) as fixed:
        client.make_elastic("fixed")
    assert fixed.value.status == 400 and "no range" in fixed.value.payload["detail"]


@pytest.mark.parametrize(
    "elastic", [{"settle_s": 0}, {"settle_s": 0.5}, {"headroom": "some"}, {"headroom": 1}]
)
def test_a_hold_down_under_a_second_or_an_unknown_headroom_is_refused(tmp_path, elastic):
    with pytest.raises(ValueError, match="elastic"):
        machine_config(tmp_path, elastic=elastic)


@pytest.mark.parametrize("headroom", ["keep", "leave-one", "use"])
def test_the_headroom_growth_leaves_is_set_in_the_config(tmp_path, headroom):
    config = machine_config(tmp_path, elastic={"headroom": headroom})
    assert isinstance(config, MachineConfig) and config.elastic.headroom == headroom
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    assert agent.ledger.headroom == headroom
    assert MachineConfig.model_validate({"ceiling": {"slots": 2}}).elastic.headroom == "keep"


def test_status_says_how_many_slots_a_lease_is_giving_back():
    row = {
        "scope": "s", "item_id": "e", "kind": "work", "user": "ann", "session": None,
        "pool": "work", "weight": 4, "min_weight": 1, "max_weight": 4, "target": 1,
        "elastic": 1, "acquired_at": 0.0, "grant_no": 1, "revoked_at": None, "labels": None,
        "expires_at": None, "ttl_s": None, "memory": None, "memory_per_slot": None,
        "revoke_reason": None, "revoke_deadline": None,
    }  # fmt: skip
    assert public(row)["giving_back"] == 3
    assert public(row | {"target": 4})["giving_back"] == 0


@pytest.mark.parametrize("body", [{"weight": 2, "elastic": True}, {"elastic": True}])
def test_only_a_range_can_be_elastic(four, body):
    with pytest.raises(ApiError) as refused:
        Client(four.path).acquire(kind="work", lease_id="x", **body)
    assert refused.value.status == 400
    with pytest.raises(ValidationError, match="only a range"):
        AcquireRequest(kind="work", **body)


class _BeforeElastic(BaseModel):
    """`POST /v1/acquire` as a daemon from before elastic leases takes it."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    pool: str | None = None
    weight: int = Field(1, ge=1)
    max_weight: int | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    lease_id: str | None = None
    session: str | None = None
    wait_s: float = 0.0
    ttl_s: float | None = None
    memory: str | None = None
    memory_per_slot: str | None = None


def test_a_daemon_from_before_elastic_leases_grants_a_fixed_range(four, monkeypatch):
    def acquire(agent, req, peer, rest):
        return agent.acquire_wait(AcquireRequest(**req.model_dump()), user=peer.user)

    monkeypatch.setitem(api.ROUTES, ("POST", "/v1/acquire"), api.Route(_BeforeElastic, acquire))
    client = Client(four.path)
    answer = client.acquire(kind="work", weight=1, max_weight=4, elastic=True, lease_id="e")
    assert answer["status"] == "granted"
    assert (answer["lease"]["weight"], answer["lease"]["elastic"]) == (4, False)


def test_leases_shows_an_elastic_leases_target_beside_its_weight():
    lease = {"weight": 5, "min_weight": 2, "max_weight": 5, "target": 3}
    assert views._weight(lease) == "5>3/2-5"
    assert views._weight(lease | {"target": 5}) == "5/2-5"
    assert views._weight({"weight": 2, "min_weight": 2, "max_weight": 5, "queued_at": 0}) == "2-5"


def test_top_shows_an_elastic_leases_target_beside_its_weight():
    assert _weight_cell({"weight": 3, "min_weight": 2, "max_weight": 4, "target": 4}) == "3>4/2-4"
    assert _weight_cell({"weight": 3, "min_weight": 2, "max_weight": 4, "target": 3}) == "3/2-4"
    assert _weight_cell({"weight": 3, "min_weight": 2, "max_weight": 4}) == "3/2-4"


def test_a_resize_is_an_event_with_the_weight_before():
    before = events.Snapshot.of(
        [{"scope": "s", "lease_id": "e", "weight": 2}], [], lend_open=False, reason=""
    )
    after = events.Snapshot.of(
        [{"scope": "s", "lease_id": "e", "weight": 3}], [], lend_open=False, reason=""
    )
    [event] = events.changes(before, after, now=0)
    assert (event["type"], event["weight"], event["previous_weight"]) == ("resized", 3, 2)


# --- the holder ---------------------------------------------------------------------------------


def test_hold_never_asks_for_an_elastic_lease_up_front():
    assert "elastic" not in lease_request(weight=1, max_weight=4)  # made elastic later, if ever


class _FakeClient:
    """A daemon that grants each resize, or refuses each one with `refuse` (a status), and
    makes a lease elastic unless `fixed` (a status)."""

    def __init__(self, refuse: int | None = None, fixed: int | None = None) -> None:
        self.asked: list[int] = []
        self.made_elastic = 0
        self.refuse = refuse
        self.fixed = fixed
        self.weight = 2
        self.target: int | None = None

    def resize(self, lease_id, weight):
        self.asked.append(weight)
        if self.refuse is not None:
            raise ApiError(self.refuse, {"error": "not_found"})
        self.weight = weight
        # The target the daemon asks for, once a test sets it; else the size asked.
        target = weight if self.target is None else self.target
        return {"weight": weight, "jobs": weight, "target": target, "target_jobs": target}

    def make_elastic(self, lease_id):
        self.made_elastic += 1
        if self.fixed is not None:
            raise ApiError(self.fixed, {"error": "not_found"})
        w = self.weight
        return {"elastic": True, "weight": w, "jobs": w, "target": w, "target_jobs": w}


def _in_fifo(server: jobs.Jobserver) -> int:
    """The tokens in the fifo now."""
    count = array.array("i", [0])
    fcntl.ioctl(server._fd, termios.FIONREAD, count)
    return count[0]


def _take(server: jobs.Jobserver, n: int) -> bytes:
    """`n` tokens out of the fifo, as jobs take them."""
    fd = os.open(server.fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return os.read(fd, n)
    finally:
        os.close(fd)


def _give(server: jobs.Jobserver, tokens: bytes) -> None:
    """`tokens` back into the fifo, as jobs give them back."""
    fd = os.open(server.fifo, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, tokens)
    finally:
        os.close(fd)


ELASTIC = {"elastic": True, "weight": 2, "jobs": 2, "target": 2, "target_jobs": 2}


def test_the_jobservers_tokens_follow_the_target_up_and_down():
    client = _FakeClient()
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", ELASTIC, server=server)
        assert _in_fifo(server) == 1
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 4, "target_jobs": 4})
        follower.step()
        assert client.asked == [4] and follower.weight == 4
        assert _in_fifo(server) == 3 and server.tokens == 3
        # Asked back to 1 while a job holds one token: the free ones are taken out and their
        # slots given back, and the job's slot only once it returns its token.
        held = _take(server, 1)
        client.target = 1
        follower.heard({"status": "ok", "weight": 4, "jobs": 4, "target": 1, "target_jobs": 1})
        follower.step()
        assert follower.busy() and client.asked == [4, 2] and server.owed == 1
        assert _in_fifo(server) == 0
        _give(server, held)
        until(lambda: server.owed == 0)
        follower.step()
        assert client.asked == [4, 2, 1] and follower.weight == 1 and not follower.busy()
        assert _in_fifo(server) == 0 and server.tokens == 0


def test_a_lease_becomes_elastic_only_once_a_job_takes_a_token():
    client = _FakeClient()
    granted = {"weight": 2, "jobs": 2, "target": 2, "target_jobs": 2, "elastic": False}
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", granted, server=server)
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 1, "target_jobs": 1})
        follower.step()  # no job has taken a token: it keeps its size
        assert follower.busy() and client.made_elastic == 0 and client.asked == []
        held = _take(server, 1)
        follower.step()
        assert client.made_elastic == 1 and follower.elastic
        _give(server, held)


def test_a_daemon_that_cannot_make_it_elastic_leaves_the_lease_as_it_is():
    client = _FakeClient(fixed=404)
    granted = {"weight": 2, "jobs": 2}
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", granted, server=server)
        _take(server, 1)
        follower.step()
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 4, "target_jobs": 4})
        follower.step()
        assert client.made_elastic == 1 and client.asked == [] and not follower.busy()
        assert follower.weight == 2 and server.tokens == 1


def test_a_daemon_without_resize_leaves_the_lease_as_it_is():
    client = _FakeClient(refuse=404)
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", ELASTIC, server=server)
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 4, "target_jobs": 4})
        follower.step()
        assert client.asked == [4] and follower.weight == 2 and server.tokens == 1


def test_a_slot_is_given_back_only_once_its_jobs_token_comes_back():
    client, said, clock = _FakeClient(), [], Clock()
    with jobs.Jobserver(3) as server:
        follower = follow.Follower(
            client, "e", ELASTIC | {"weight": 3, "jobs": 3}, server=server, say=said.append,
            clock=clock,
        )  # fmt: skip
        held = _take(server, 2)  # two jobs running beside the command's own
        client.target = 1
        follower.heard({"status": "ok", "weight": 3, "jobs": 3, "target": 1, "target_jobs": 1})
        follower.step()
        assert client.asked == [] and server.owed == 2
        assert said == ["giving back 2 of 3 slots as jobs finish and return their tokens"]
        _give(server, held[:1])  # one job ends; the other was killed and never returns its own
        until(lambda: server.owed == 1)
        follower.step()
        assert client.asked == [2] and follower.weight == 2
        for _ in range(3):
            clock.now += 3600
            follower.step()  # however long it waits, the slot of the lost token stays held
        assert client.asked == [2] and follower.weight == 2 and server.owed == 1


def test_tokens_that_come_back_late_never_run_jobs_past_the_target():
    with jobs.Jobserver(6) as server:
        held = _take(server, 5)  # every job running
        server.set(1)
        assert server.owed == 4
        for token in held:
            time.sleep(0.3)
            _give(server, bytes([token]))
            time.sleep(0.05)
            assert _in_fifo(server) <= 1
        until(lambda: server.owed == 0 and _in_fifo(server) == 1)
        assert server.tokens == 1


def test_a_follower_not_yet_elastic_looks_often_only_for_a_while():
    clock = Clock()
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(
            _FakeClient(), "e", {"weight": 2, "jobs": 2}, server=server, clock=clock
        )
        assert follower.busy()
        clock.now += follow.WATCH_S
        assert not follower.busy()  # it looks at each renewal from now on


SLOW_JOBS = """\
LONG := l1 l2 l3
SHORT := $(foreach n,$(shell seq 1 12),s$(n))
all: $(LONG) $(SHORT)
$(LONG):
\t@touch $(RUNNING)/$@; sleep 2; rm $(RUNNING)/$@
$(SHORT):
\t@touch $(RUNNING)/$@; sleep 0.2; rm $(RUNNING)/$@
.PHONY: all $(LONG) $(SHORT)
"""


@pytest.mark.skipif(_make_version() < (4, 4), reason="needs GNU make 4.4 or later")
def test_a_make_never_runs_more_jobs_than_the_lease_holds_while_it_gives_back(tmp_path):
    running = tmp_path / "running"
    running.mkdir()
    (tmp_path / "Makefile").write_text(SLOW_JOBS)
    client = _FakeClient()
    client.weight = 4
    seen: list[tuple[int, int]] = []
    with jobs.Jobserver(4) as server:
        follower = follow.Follower(client, "e", ELASTIC | {"weight": 4, "jobs": 4}, server=server)
        env = CHILD_ENV | {"MAKEFLAGS": jobs.makeflags(None, 4, server.fifo)}
        env["RUNNING"] = str(running)
        make = subprocess.Popen(["make", "-s", "-C", str(tmp_path)], env=env)
        until(lambda: len(list(running.iterdir())) == 4)
        client.target = 1
        follower.heard({"status": "ok", "weight": 4, "jobs": 4, "target": 1, "target_jobs": 1})
        while make.poll() is None:
            follower.step()
            weight = follower.weight  # what the ledger holds, read before the jobs are counted
            seen.append((len(list(running.iterdir())), weight))
            time.sleep(0.02)
    assert make.returncode == 0
    assert all(jobs_now <= weight for jobs_now, weight in seen), seen
    assert client.asked[-1] == 1 and seen[-1][1] == 1


def test_a_token_owed_no_longer_goes_straight_back():
    with jobs.Jobserver(3) as server:
        held = _take(server, 2)
        server.set(0)  # owe both
        assert server.owed == 2
        server.set(2)  # the target rose again first: nothing is owed, nothing written
        assert server.owed == 0 and _in_fifo(server) == 0
        _give(server, held[:1])
        time.sleep(0.2)
        assert _in_fifo(server) == 1  # read by the jobserver's reader, and put back
        _give(server, held[1:])
        until(lambda: _in_fifo(server) == 2)
        assert server.tokens == 2


def test_the_jobservers_reader_stops_when_it_is_left():
    with jobs.Jobserver(2) as server:
        # A job still holds the fifo open for writing, as an orphaned one may: the reader
        # blocked in read() sees no end of file, and must be woken.
        job = os.open(server.fifo, os.O_WRONLY | os.O_NONBLOCK)
        _take(server, 1)
        server.set(0)
        reader = server._reader
        assert reader is not None
        until(lambda: server._reading)  # blocked in read()
        left = time.monotonic()
    assert time.monotonic() - left < 2 and not reader.is_alive()
    os.close(job)


MAKE_BUSY = """\
JOBS := $(foreach n,$(shell seq 1 60),j$(n))
all: $(JOBS)
$(JOBS):
\t@sleep 0.3
.PHONY: all $(JOBS)
"""


@pytest.mark.skipif(_make_version() < (4, 4), reason="needs GNU make 4.4 or later")
def test_tokens_are_won_back_from_a_busy_make(tmp_path):
    (tmp_path / "Makefile").write_text(MAKE_BUSY)
    with jobs.Jobserver(6) as server:
        env = CHILD_ENV | {"MAKEFLAGS": jobs.makeflags(None, 6, server.fifo)}
        make = subprocess.Popen(["make", "-s", "-C", str(tmp_path)], env=env)
        try:
            until(lambda: server.taken() >= 3)
            server.set(1)  # four of the five owed
            until(lambda: server.owed == 0, timeout=5)
        finally:
            make.wait(30)
    assert make.returncode == 0


def test_an_elastic_command_leaves_the_job_count_to_the_jobserver():
    env = jobs.sized_env({}, 2, elastic_jobs=8)
    assert "CMAKE_BUILD_PARALLEL_LEVEL" not in env
    assert (env["CARGO_BUILD_JOBS"], env["LEASEHOLD_JOBS"]) == ("8", "2")
    outer = jobs.sized_env({"LEASEHOLD_LEASE_ID": "outer"}, 4) | {"LEASEHOLD_LEASE_ID": "outer"}
    assert "CMAKE_BUILD_PARALLEL_LEVEL" not in jobs.sized_env(outer, 2, elastic_jobs=8)
    users = jobs.sized_env({"CMAKE_BUILD_PARALLEL_LEVEL": "3"}, 2, elastic_jobs=8)
    assert users["CMAKE_BUILD_PARALLEL_LEVEL"] == "3"
    assert jobs.sized_env({}, 2)["CMAKE_BUILD_PARALLEL_LEVEL"] == "2"


@pytest.mark.parametrize(
    ("argv", "count"),
    [
        (["make", "-j8"], "-j8"),
        (["make", "-sj4", "all"], "-sj4"),
        (["make", "-j", "8"], "-j 8"),
        (["ninja", "--jobs=3"], "--jobs=3"),
        (["sh", "-c", "make -j8 && make install"], "-j8"),
        (["make", "-j"], "-j"),
        (["make", "-j", "all"], "-j"),
        (["make", "--jobs"], "--jobs"),
        (["make", "--jobs="], "--jobs="),
        (["sh", "-c", "make -sj"], "-sj"),
        (["make"], None),
        (["cc", "-obj", "x.o"], None),
        (["cmake", "--build", "."], None),
    ],
)
def test_a_command_lines_own_job_count_is_found(argv, count):
    assert jobs.own_job_count(argv) == count


def _hold(four, *args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "leasehold", "--socket", str(four.path), "hold", *args],
        env=(env or CHILD_ENV) | {contain.ENV: "off"},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip


def test_elastic_without_a_jobserver_is_refused(four):
    done = _hold(four, "--weight", "1-4", "--elastic", "--", sys.executable, "-c", "pass")
    assert done.returncode == 2
    assert "--elastic needs --jobserver" in done.stderr
    assert Client(four.path).status()["leases"] == []


def test_hold_warns_of_a_command_lines_own_job_count(four):
    done = _hold(four, "--weight", "1-2", "--jobserver", "--", sys.executable, "-c", "pass", "-j8")
    assert done.returncode == 0, done.stderr
    assert "gives its own job count (-j8)" in done.stderr


def test_follower_calls_wait_no_longer_than_a_renewal(four, monkeypatch):
    seen = []

    class Seen(follow.Follower):
        def __init__(self, client, *args, **kwargs):
            seen.append(client)
            super().__init__(client, *args, **kwargs)

    monkeypatch.setattr(hold_module.follow, "Follower", Seen)
    monkeypatch.setattr(hold_module, "_say", lambda message: None)
    client = Client(four.path, timeout=30)
    code = hold_module.hold(
        client, [sys.executable, "-c", "pass"], weight=1, max_weight=4, jobserver=True, ttl_s=3
    )
    assert code == 0
    [quick] = seen
    assert quick.timeout <= 1.0  # the renewal interval: settle_s, 1 s here


#: A jobserver client that, until `$STOP` exists, takes every token free, notes how many,
#: holds them a while as jobs do, and puts them back.
TOKENS = """\
import os, re, sys, time
path = re.search(r"--jobserver-auth=fifo:(\\S+)", os.environ["MAKEFLAGS"])[1]
fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
log = open(os.environ["LOG"], "a", buffering=1)
while not os.path.exists(os.environ["STOP"]):
    try:
        got = os.read(fd, 100)
    except BlockingIOError:
        got = b""
    log.write(f"{len(got)}\\n")
    time.sleep(0.2)
    if got:
        os.write(fd, got)
    time.sleep(0.02)
"""


def _last(log: Path) -> int | None:
    lines = log.read_text().split() if log.exists() else []
    return int(lines[-1]) if lines else None


def test_hold_jobserver_tokens_grow_into_free_slots_and_shrink_for_a_waiting_request(
    four, tmp_path
):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="other", ttl_s=60)
    log, stop = tmp_path / "log", tmp_path / "stop"
    env = CHILD_ENV | {"LOG": str(log), "STOP": str(stop), contain.ENV: "off"}
    env.pop("MAKEFLAGS", None)
    argv = [sys.executable, "-m", "leasehold", "--socket", str(four.path), "hold"]
    held = subprocess.Popen(
        [*argv, "--weight", "1-4", "--jobserver", "--", sys.executable, "-c", TOKENS],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: _last(log) == 1, timeout=20)  # granted 2: one token beside its own
        until(lambda: any(x["elastic"] for x in client.status()["leases"]), timeout=20)
        client.release("other")
        until(lambda: _last(log) == 3, timeout=20)  # grown to 4, one slot at a time
        waiter = threading.Thread(
            target=client.acquire,
            kwargs={"kind": "work", "weight": 2, "lease_id": "w", "ttl_s": 60, "wait_s": 20},
        )
        waiter.start()
        until(lambda: client.leases()["queue"] == [] and _last(log) == 1, timeout=20)
        waiter.join(20)
        [mine] = [x for x in client.status()["leases"] if x["lease_id"] != "w"]
        assert mine["weight"] == 2 and mine["elastic"]
    finally:
        stop.touch()
        _, err = held.communicate(timeout=30)
    assert held.returncode == 0, err
    assert "the lease is now 2 slots" in err


#: Until `$STOP` exists, sleep: a command that never takes a jobserver token (a make given its
#: own -jN, say).
IGNORES = """\
import os, time
while not os.path.exists(os.environ["STOP"]):
    time.sleep(0.05)
"""


def test_a_command_that_takes_no_token_keeps_its_size(four, tmp_path):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="other", ttl_s=60)
    stop = tmp_path / "stop"
    env = CHILD_ENV | {"STOP": str(stop), contain.ENV: "off"}
    argv = [sys.executable, "-m", "leasehold", "--socket", str(four.path), "hold"]
    held = subprocess.Popen(
        [*argv, "--weight", "1-4", "--jobserver", "--", sys.executable, "-c", IGNORES],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: len(client.status()["leases"]) == 2, timeout=20)
        client.release("other")
        time.sleep(3)  # three renewals and settle windows
        [mine] = client.status()["leases"]
        assert (mine["weight"], mine["target"], mine["elastic"]) == (2, 2, False)
    finally:
        stop.touch()
        _, err = held.communicate(timeout=30)
    assert held.returncode == 0, err


live = pytest.mark.skipif(
    not contain.probe().enforced, reason=f"no user scopes here ({contain.probe().reason})"
)

#: Until `$STOP` exists, hold a jobserver token as a job does, and note this scope's
#: cpu.weight.
CPU_WEIGHT = """\
import os, pathlib, re, time
path = re.search(r"--jobserver-auth=fifo:(\\S+)", os.environ["MAKEFLAGS"])[1]
token = os.read(os.open(path, os.O_RDONLY), 1)
group = open('/proc/self/cgroup').read().split('::')[1].strip()
weight = pathlib.Path('/sys/fs/cgroup' + group) / 'cpu.weight'
log = open(os.environ["LOG"], "a", buffering=1)
while not os.path.exists(os.environ["STOP"]):
    log.write(weight.read_text().strip() + "\\n")
    time.sleep(0.05)
"""


@live
def test_an_elastic_leases_scope_is_reweighted_as_it_grows(four, tmp_path):
    client = Client(four.path)
    client.acquire(kind="work", weight=2, lease_id="other", ttl_s=60)
    log, stop = tmp_path / "log", tmp_path / "stop"
    env = CHILD_ENV | {"LOG": str(log), "STOP": str(stop)}
    argv = [sys.executable, "-m", "leasehold", "--socket", str(four.path), "hold"]
    held = subprocess.Popen(
        [*argv, "--weight", "2-3", "--jobserver", "--", sys.executable, "-c", CPU_WEIGHT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: _last(log) == 200, timeout=20)
        client.release("other")
        until(lambda: _last(log) == 300, timeout=20)
    finally:
        stop.touch()
        _, err = held.communicate(timeout=30)
    assert held.returncode == 0, err
