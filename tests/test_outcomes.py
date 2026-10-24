"""What a contained command used, sent with its lease's release: kept with what the lease held,
listed by rule and repository, and never what keeps a lease from being released."""

from __future__ import annotations

import contextlib
import json
import math

import pytest
from support import FACTS, FakeLoad, cli, machine_config

from leasehold import api, template
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.server import Daemon

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
            "parallelism": 2.0, "last_ended_at": rows[0]["ended_at"], "learned_bytes": None,
            "learned_runs": None,
        },
        {
            "rule": "cargo", "repo": "/src/app", "runs": 2, "peak_p95_bytes": 3 * GIB,
            "peak_max_bytes": 3 * GIB, "anon_max_bytes": GIB // 2, "oom_runs": 1,
            "high_runs": 0, "parallelism": 1.5, "last_ended_at": rows[1]["ended_at"],
            "learned_bytes": None, "learned_runs": 1,
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


@contextlib.contextmanager
def budgeted(tmp_path, sock_dir, learn: str):
    """A daemon on the 2-slot test machine with an 8 GiB memory budget (4 GiB assumed a slot)
    and `[memory] learn = learn`."""
    memory = {"budget": "8g", "learn": learn}
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"), memory=memory)
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        yield Client(d.path)
    finally:
        d.stop()


def ran(client: Client, repo: str, *runs: dict) -> None:
    """A lease of the `cargo` rule in `repo` for each of `runs`, released with that outcome."""
    for run in runs:
        lease_id = granted(client, rule="cargo", repo=repo)
        client.release(lease_id, outcome=outcome(1, repo=repo, wall_s=60.0) | run)


@pytest.mark.parametrize("learn", ["off", "shadow", "on"])
def test_a_rule_s_past_runs_set_what_its_next_lease_counts_once_learn_is_on(
    tmp_path, sock_dir, learn
):
    learned = math.ceil(GIB * 1.15)  # the median anon of three clean runs, plus 15%
    with budgeted(tmp_path, sock_dir, learn) as client:
        ran(client, "/src/app", *[{"anon_bytes": GIB}] * 3)
        ran(client, "/src/new", {"anon_bytes": GIB}, {"anon_bytes": GIB})  # 2 runs: cold
        ran(client, "/src/new", {"anon_bytes": 5 * GIB, "exit_status": 1})  # failed: not clean
        big = granted(client, memory="6g")
        answer = client.acquire(kind="work", rule="cargo", repo="/src/app", ttl_s=30, wait_s=0)
        if learn == "on":
            # Counted at the learned 1.15 GiB, it fits beside the 6 GiB declared.
            assert answer["status"] == "granted"
            lease = answer["lease"]
            assert (lease["memory_from"], lease["memory_counted_bytes"]) == ("learned", learned)
            assert lease["memory_bytes"] is None  # nothing was declared
        else:
            # Counted at weight 1 x 4 GiB assumed, it waits.
            assert answer["status"] == "queued"
            assert "this counts 4.0 GiB (weight 1 at 4.0 GiB a slot" in answer["blocked"]
            (lease,) = client.leases()["queue"]
            assert (lease["memory_from"], lease["memory_counted_bytes"]) == ("assumed", 4 * GIB)
        assert lease["learned_bytes"] == (None if learn == "off" else learned)
        assert lease["learned_runs"] == (None if learn == "off" else 3)
        shown = {
            "off": "4.0 GiB assumed ",
            "shadow": "4.0 GiB assumed, learned 1.2 GiB ",
            "on": "1.2 GiB learned (3 runs) ",
        }
        assert shown[learn] in cli(client.path, "leases").stdout
        client.release(lease["lease_id"])
        client.release(big)

        cold = client.acquire(kind="work", rule="cargo", repo="/src/new", ttl_s=30, wait_s=0)
        declared = client.acquire(
            kind="work", rule="cargo", repo="/src/app", memory="2g", ttl_s=30, wait_s=0
        )
        assert [x["memory_from"] for x in (cold["lease"], declared["lease"])] == [
            "assumed",
            "declared",
        ]
        assert cold["lease"]["memory_counted_bytes"] == 4 * GIB
        assert cold["lease"]["learned_runs"] == (None if learn == "off" else 2)
        assert declared["lease"]["memory_counted_bytes"] == 2 * GIB
        assert declared["lease"]["learned_bytes"] is None

        groups = {g["repo"]: g for g in client.outcomes()["groups"]}
        assert (groups["/src/app"]["learned_bytes"], groups["/src/new"]["learned_bytes"]) == (
            learned,
            None,
        )
        assert client.status()["memory"]["learn"] == learn


@pytest.mark.parametrize(
    ("learn", "contain", "config", "memory"),
    [
        ('"maybe"', "auto", "fail", None),
        ('"on"', "off", "ok", "warn"),  # nothing is measured, so nothing is learned
        ('"on"', "auto", "ok", None),
    ],
)
def test_doctor_checks_memory_learn(tmp_path, learn, contain, config, memory):
    path = tmp_path / "machine.toml"
    text = template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 * GIB})
    assert 'learn = "shadow"' in text
    text = text.replace('learn = "shadow"', f"learn = {learn}")
    path.write_text(text.replace('mode = "auto"', f'mode = "{contain}"'))
    doctor = cli(tmp_path / "none.sock", "--config", str(path), "doctor", "--json")
    checks = {c["name"]: c for c in json.loads(doctor.stdout)}
    assert checks["config"]["level"] == config
    assert ("learn" in checks["config"]["detail"]) == (config == "fail")
    assert checks.get("memory", {}).get("level") == memory
