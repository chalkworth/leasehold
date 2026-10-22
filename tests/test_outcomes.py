"""What a contained command used, sent with its lease's release: kept with what the lease held,
listed by rule and repository, and never what keeps a lease from being released."""

from __future__ import annotations

from support import cli

from leasehold import api
from leasehold.client import Client

GIB = 1 << 30


def outcome(peak_gib: float, **fields) -> dict:
    return {
        "rule": "cargo",
        "repo": "/src/app",
        "peak_bytes": int(peak_gib * GIB),
        "anon_bytes": GIB // 2,
        "oom_kills": 0,
        "high_events": 0,
        "cpu_usec": 4_000_000,
        "wall_s": 2.0,
        "exit_status": 0,
    } | fields


def granted(client: Client, **request) -> str:
    answer = client.acquire(kind="work", ttl_s=30, wait_s=0, **request)
    assert answer["status"] == "granted"
    return answer["lease"]["lease_id"]


def test_a_release_keeps_its_outcome_and_the_listing_sums_them(daemon):
    client = Client(daemon.path)
    first = granted(client, weight=1, max_weight=2, memory_per_slot="1g")
    client.release(first, outcome=outcome(1, oom_kills=1, future_field="ignored"))
    client.release(granted(client), outcome=outcome(3, cpu_usec=2_000_000))
    client.release(granted(client), outcome=outcome(1, rule=None, repo=None, peak_bytes=None))
    client.release(granted(client))  # no outcome: released, nothing kept
    gone = granted(client)
    client.release(gone)
    client.release(gone, outcome=outcome(9))  # no longer held: nothing kept

    assert client.leases()["leases"] == []
    listing = client.outcomes()
    rows = listing["outcomes"]
    assert [r["peak_bytes"] for r in rows] == [None, 3 * GIB, GIB]
    assert {k: rows[-1][k] for k in ("weight", "min_weight", "max_weight", "memory_per_slot")} == {
        "weight": 2,
        "min_weight": 1,
        "max_weight": 2,
        "memory_per_slot": GIB,
    }
    assert listing["groups"] == [
        {
            "rule": None, "repo": None, "runs": 1, "peak_p95_bytes": None,
            "peak_max_bytes": None, "anon_max_bytes": GIB // 2, "oom_runs": 0, "high_runs": 0,
            "parallelism": 2.0, "last_ended_at": rows[0]["ended_at"],
        },
        {
            "rule": "cargo", "repo": "/src/app", "runs": 2, "peak_p95_bytes": 3 * GIB,
            "peak_max_bytes": 3 * GIB, "anon_max_bytes": GIB // 2, "oom_runs": 1,
            "high_runs": 0, "parallelism": 1.5, "last_ended_at": rows[1]["ended_at"],
        },
    ]  # fmt: skip
    shown = cli(daemon.path, "outcomes")
    assert shown.returncode == 0
    assert "cargo  /src/app  2     3.0 GiB" in shown.stdout


def test_a_daemon_that_refuses_the_outcome_still_releases(daemon, monkeypatch):
    # The release route of a daemon from before outcomes, which forbids the field.
    def release(agent, req, peer, rest):
        return agent.release(req.lease_id, user=peer.user)

    before = api.Route(api._LeaseRequest, release)
    monkeypatch.setitem(api.ROUTES, ("POST", "/v1/release"), before)
    client = Client(daemon.path)
    lease_id = granted(client)
    assert client.release(lease_id, outcome=outcome(1))["status"] == "released"
    assert client.leases()["leases"] == []
