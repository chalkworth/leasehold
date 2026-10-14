"""What happened on the machine (leasehold.events): events worked out from ledger snapshots,
the ring that keeps them, and the agent and API that serve them."""

from __future__ import annotations

import json

from support import make_agent

from leasehold.agent import AcquireRequest
from leasehold.api import Peer, handle
from leasehold.client import Client
from leasehold.events import EventLog, EventType, Snapshot, changes


def row(lease_id, **kw):
    return {"lease_id": lease_id, "kind": "work", "pool": "work", "user": "ann", "weight": 1} | kw


def types(events) -> list[tuple[str, str | None]]:
    return [(e["type"], e.get("lease_id")) for e in events]


def test_event_types_are_the_names_clients_see():
    assert [str(t) for t in EventType] == [
        "queued",
        "granted",
        "left",
        "released",
        "ended",
        "revoked",
        "lend_opened",
        "lend_closed",
        "pool_max",
        "lend_switch",
    ]
    assert json.dumps({"type": EventType.LEND_SWITCH}) == '{"type": "lend_switch"}'


def test_the_ring_hands_out_event_types_as_plain_strings():
    log = EventLog()
    log.emit([{"type": EventType.GRANTED, "lease_id": "w"}], now=1.0)
    (event,) = log.since(0)["events"]
    assert type(event["type"]) is str and event["type"] == "granted"
    assert list(event) == ["seq", "time", "type", "lease_id"]


def test_changes_name_every_way_a_lease_starts_and_ends():
    before = Snapshot.of(
        [row("kept"), row("given-back"), row("timed-out", expires_at=10.0), row("orphan")],
        [row("waiting"), row("gave-up")],
        lend_open=False,
        reason="busy",
    )
    after = Snapshot.of(
        [row("kept", revoked=True, revoke_reason="needed"), row("waiting"), row("new")],
        [row("fresh", position=1)],
        lend_open=True,
        reason="idle for 600s",
    )
    found = changes(before, after, now=20.0, released=frozenset({(None, "given-back")}))
    assert types(found) == [
        ("lend_opened", None),
        ("revoked", "kept"),
        ("released", "given-back"),
        ("ended", "timed-out"),
        ("ended", "orphan"),
        ("left", "gave-up"),
        ("granted", "waiting"),
        ("granted", "new"),
        ("queued", "fresh"),
    ]
    by_id = {e.get("lease_id"): e for e in found}
    assert by_id["waiting"]["waited"] is True and by_id["new"]["waited"] is False
    assert by_id["timed-out"]["reason"] == "expired"
    assert by_id["orphan"]["reason"] == "holder gone"
    assert by_id["kept"]["reason"] == "needed" and by_id["fresh"]["position"] == 1


def test_the_ring_hands_out_events_after_a_seq_and_says_when_some_were_dropped():
    log = EventLog(capacity=3)
    log.emit([{"type": "a"}, {"type": "b"}], now=1.0)
    page = log.since(0)
    assert [e["seq"] for e in page["events"]] == [1, 2] and page["next"] == 2
    assert log.since(page["next"]) == {"events": [], "next": 2, "dropped": False}
    log.emit([{"type": c} for c in "cdef"], now=2.0)
    late = log.since(2)
    assert [e["type"] for e in late["events"]] == ["d", "e", "f"] and late["dropped"]
    assert log.since(6)["dropped"] is False
    # A `since` the daemon never reached (it restarted): everything it has.
    assert [e["seq"] for e in log.since(999)["events"]] == [4, 5, 6]


def test_the_agent_records_grants_queues_releases_revocations_and_lending(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()  # lending opens: the machine is idle and quiet_s is 0
    lend = AcquireRequest(kind="lend", lease_id="L")
    work = AcquireRequest(kind="work", weight=2, lease_id="W")
    agent.acquire(lend, user="ann")
    agent.acquire(work, user="bob")  # waits; lending stops and L is revoked
    agent.release("L", user="ann")
    agent.acquire(work, user="bob")
    agent.set_pool_max("external", 1)
    found = agent.events.since(0)["events"]
    assert types(found) == [
        ("lend_opened", None),
        ("granted", "L"),
        ("lend_closed", None),
        ("revoked", "L"),
        ("queued", "W"),
        ("released", "L"),
        ("granted", "W"),
        ("pool_max", None),
    ]
    assert found[-1] == found[-1] | {"pool": "external", "max": 1, "overridden": True}
    assert [e["seq"] for e in found] == list(range(1, len(found) + 1))


def test_a_restarted_agent_does_not_report_old_leases_as_new(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.acquire(AcquireRequest(kind="work", lease_id="W"), user="ann")
    again, _, _ = make_agent(tmp_path)
    again.tick()
    assert [e["type"] for e in again.events.since(0)["events"]] == ["lend_opened"]


def test_events_are_served_over_the_api_and_the_socket(tmp_path, daemon):
    agent, _, _ = make_agent(tmp_path)
    agent.acquire(AcquireRequest(kind="work", lease_id="W"), user="ann")
    code, page = handle(agent, "GET", "/v1/events?since=0&limit=1", None, Peer(uid=0, user="x"))
    assert code == 200 and len(page["events"]) == 1
    code, _ = handle(agent, "GET", "/v1/events?since=-1", None, Peer(uid=0, user="x"))
    assert code == 400
    client = Client(daemon.path)
    client.acquire(kind="work", lease_id="S", weight=1)
    served = client.events(0)
    assert ("granted", "S") in types(served["events"])
    assert json.dumps(served)  # plain JSON all the way down
    assert client.events(served["next"])["events"] == []
