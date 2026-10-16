"""Elastic leases in the ledger, the daemon and `hold`: a lease is offered free slots after its
hold-down and takes them with `POST /v1/resize`, is asked back when a request waits for them and
gives them back, and `hold --jobserver` moves its jobserver's tokens and its scope with it."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field
from support import CHILD_ENV, FACTS, Clock, FakeLoad, machine_config, until

from leasehold import api, contain, events, follow, jobs
from leasehold import ledger as ledger_module
from leasehold.admission import AdmissionPolicy
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.client import ApiError, Client
from leasehold.hold import lease_request
from leasehold.ledger import Admitted, Ledger, LedgerItem, Queued
from leasehold.model import Pool, PoolSet
from leasehold.server import Daemon
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
    """Four slots, all `work`'s, no headroom, and no hold-down."""
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
        elastic={"settle_s": 0},
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
    assert granted["elastic"] == {"settle_s": 0.0}
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


@pytest.mark.parametrize("body", [{"weight": 2, "elastic": True}, {"elastic": True}])
def test_only_a_range_can_be_elastic(four, body):
    with pytest.raises(ApiError) as refused:
        Client(four.path).acquire(kind="work", lease_id="x", **body)
    assert refused.value.status == 400


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


def test_hold_asks_for_elastic_only_with_a_range_and_a_jobserver():
    assert lease_request(weight=1, max_weight=4, elastic=True)["elastic"] is True
    assert lease_request(weight=2, max_weight=2, elastic=True)["elastic"] is None
    assert lease_request(weight=1, max_weight=4)["elastic"] is None  # shims and hooks


class _FakeClient:
    """A daemon that grants each resize, or refuses each one with `refuse` (a status)."""

    def __init__(self, refuse: int | None = None) -> None:
        self.asked: list[int] = []
        self.refuse = refuse

    def resize(self, lease_id, weight):
        self.asked.append(weight)
        if self.refuse is not None:
            raise ApiError(self.refuse, {"error": "not_found"})
        return {"weight": weight, "jobs": weight, "target": weight, "target_jobs": weight}


def _free(server: jobs.Jobserver) -> int:
    """The tokens in the fifo now, put back afterwards."""
    fd = os.open(server.fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        try:
            got = os.read(fd, 100)
        except BlockingIOError:
            got = b""
    finally:
        os.close(fd)
    server.add(len(got))
    server.tokens -= len(got)
    return len(got)


def test_the_jobservers_tokens_follow_the_target_up_and_down():
    client = _FakeClient()
    granted = {"weight": 2, "jobs": 2, "target": 2, "target_jobs": 2}
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", granted, server=server)
        assert _free(server) == 1
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 4, "target_jobs": 4})
        follower.step()
        assert client.asked == [4] and follower.weight == 4
        assert _free(server) == 3 and server.tokens == 3
        # Asked back to 1 while a job holds one token: the free ones go first, and the lease
        # is given back only once the job returns its token.
        fd = os.open(server.fifo, os.O_RDONLY | os.O_NONBLOCK)
        held = os.read(fd, 1)
        follower.heard({"status": "ok", "weight": 4, "jobs": 4, "target": 1, "target_jobs": 1})
        follower.step()
        assert follower.busy() and client.asked == [4] and server.tokens == 1
        os.write(os.open(server.fifo, os.O_WRONLY | os.O_NONBLOCK), held)
        os.close(fd)
        follower.step()
        assert client.asked == [4, 1] and follower.weight == 1 and not follower.busy()
        assert _free(server) == 0 and server.tokens == 0


def test_a_daemon_without_resize_leaves_the_lease_as_it_is():
    client = _FakeClient(refuse=404)
    granted = {"weight": 2, "jobs": 2}
    with jobs.Jobserver(2) as server:
        follower = follow.Follower(client, "e", granted, server=server)
        follower.heard({"status": "ok", "weight": 2, "jobs": 2, "target": 4, "target_jobs": 4})
        follower.step()
        assert client.asked == [4] and follower.weight == 2 and server.tokens == 1


#: A jobserver client that, until `$STOP` exists, takes every token free, notes how many,
#: and puts them back.
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
    if got:
        os.write(fd, got)
    time.sleep(0.05)
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
    assert "the lease is now 2 slots (was 4)" in err


live = pytest.mark.skipif(
    not contain.probe().enforced, reason=f"no user scopes here ({contain.probe().reason})"
)

#: Until `$STOP` exists, note this scope's cpu.weight.
CPU_WEIGHT = """\
import os, pathlib, time
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
    client.acquire(kind="work", weight=3, lease_id="other", ttl_s=60)
    log, stop = tmp_path / "log", tmp_path / "stop"
    env = CHILD_ENV | {"LOG": str(log), "STOP": str(stop)}
    argv = [sys.executable, "-m", "leasehold", "--socket", str(four.path), "hold"]
    held = subprocess.Popen(
        [*argv, "--weight", "1-2", "--elastic", "--", sys.executable, "-c", CPU_WEIGHT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        until(lambda: _last(log) == 100, timeout=20)
        client.release("other")
        until(lambda: _last(log) == 200, timeout=20)
    finally:
        stop.touch()
        _, err = held.communicate(timeout=30)
    assert held.returncode == 0, err
