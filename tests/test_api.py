"""The machine agent's HTTP/JSON API as a function (leasehold.api.handle), without a socket: the
routes and their status codes, and the administrator-only routes `/v1/lend` and
`/v1/pools/<name>`, which answer 403 to anyone else."""

from __future__ import annotations

import json
import os

import pytest
from support import make_agent

from leasehold.api import Peer, handle

ADMIN = Peer(uid=os.getuid(), user="svc")
#: A uid that is neither root nor this test's own, and a user in no admin group.
OUTSIDER = Peer(uid=os.getuid() + 12345, user="nobody-here")


@pytest.fixture
def agent(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    agent.tick()
    return agent


def post(agent, path: str, body: dict, peer: Peer = ADMIN) -> tuple[int, dict]:
    return handle(agent, "POST", path, json.dumps(body).encode(), peer)


@pytest.mark.parametrize(
    ("path", "body"),
    [("/v1/lend", {"open": False}), ("/v1/pools/work", {"max": 1})],
)
@pytest.mark.parametrize("peer", [OUTSIDER, Peer()], ids=["another user", "unknown peer"])
def test_the_admin_routes_are_forbidden_to_a_non_administrator(agent, path, body, peer):
    code, answer = post(agent, path, body, peer)
    assert code == 403 and answer["error"] == "forbidden"
    assert "only root, the daemon's user" in answer["detail"]
    assert agent.status()["lend"]["switch"] is True
    assert agent.status()["pools"]["work"]["max"] == 2


def test_the_forbidden_answer_names_the_admin_group_when_there_is_one(tmp_path):
    agent, _, _ = make_agent(tmp_path, admin_group="operators")
    code, answer = post(agent, "/v1/lend", {"open": False}, OUTSIDER)
    assert code == 403 and "a member of group 'operators'" in answer["detail"]


def test_an_administrator_switches_lending(agent):
    code, answer = post(agent, "/v1/lend", {"open": False})
    assert code == 200 and answer["switch"] is False
    code, answer = post(agent, "/v1/lend", {"open": True})
    assert code == 200 and answer["switch"] is True


def test_an_administrator_sets_and_resets_a_pools_max(agent):
    code, answer = post(agent, "/v1/pools/work", {"max": 1})
    assert code == 200
    assert answer == {"pool": "work", "max": 1, "configured_max": 2, "overridden": True}
    code, answer = post(agent, "/v1/pools/work/", {"max": None})
    assert code == 200 and answer["max"] == 2 and answer["overridden"] is False


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/pools/work", {"max": 3}),  # above the machine's 2 slots
        ("/v1/pools/work", {"max": 0}),
        ("/v1/pools/gone", {"max": 1}),
        ("/v1/pools/work", {}),
        ("/v1/pools/work", {"max": 1, "min": 0}),
        ("/v1/lend", {"open": "yes please"}),
    ],
)
def test_a_bad_admin_request_is_400(agent, path, body):
    code, answer = post(agent, path, body)
    assert code == 400 and answer["error"] == "bad_request"


def test_observed_leases_cannot_be_acquired(agent):
    code, answer = post(agent, "/v1/acquire", {"kind": "observed"}, OUTSIDER)
    assert code == 200 and answer["status"] == "refused"
    code, answer = post(agent, "/v1/acquire", {"kind": "work", "pool": "observed"}, OUTSIDER)
    assert code == 200 and answer["status"] == "refused"
    assert agent.leases() == {"leases": [], "queue": []}


def test_anyone_may_acquire_renew_and_release_their_own_lease(agent):
    code, answer = post(agent, "/v1/acquire", {"kind": "work", "lease_id": "w"}, OUTSIDER)
    assert code == 200 and answer["status"] == "granted"
    assert answer["lease"]["user"] == OUTSIDER.user
    assert post(agent, "/v1/renew", {"lease_id": "w"}, OUTSIDER)[1]["status"] == "ok"
    code, answer = post(agent, "/v1/release", {"lease_id": "w"}, Peer(uid=1, user="someone"))
    assert code == 403  # not theirs
    assert post(agent, "/v1/release", {"lease_id": "w"}, OUTSIDER)[1]["status"] == "released"


def test_the_pool_route_needs_a_pool_name_and_post(agent):
    assert post(agent, "/v1/pools/", {"max": 1})[0] == 404
    assert post(agent, "/v1/pools", {"max": 1})[0] == 404
    assert handle(agent, "GET", "/v1/pools/work", None, ADMIN)[0] == 404
    code, answer = post(agent, "/v1/pools/nope", {"max": 1})
    assert code == 400 and "nope" in answer["detail"]


def test_malformed_bodies_are_400_and_unknown_routes_404(agent):
    assert handle(agent, "POST", "/v1/acquire", b"not json", ADMIN)[0] == 400
    assert handle(agent, "POST", "/v1/acquire", b"[1, 2]", ADMIN)[0] == 400
    assert handle(agent, "GET", "/v1/share", None, ADMIN)[0] == 404
    assert handle(agent, "POST", "/v1/share", b'{"slots": 1}', ADMIN)[0] == 404
    code, answer = handle(agent, "GET", "/v1/status?pretty=1", None, OUTSIDER)
    assert code == 200 and answer["schema"] == 2


@pytest.mark.parametrize(
    ("path", "body"), [("/v1/lend", {"open": "yes please"}), ("/v1/pools/work", {})]
)
def test_a_non_administrator_is_forbidden_whatever_the_body(agent, path, body):
    code, answer = post(agent, path, body, OUTSIDER)
    assert code == 403 and answer["error"] == "forbidden"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/lend", {"open": "no"}),
        ("/v1/lend", {"open": 0}),
        ("/v1/pools/work", {"max": True}),
        ("/v1/pools/work", {"max": "1"}),
    ],
)
def test_admin_bodies_are_read_strictly(agent, path, body):
    code, answer = post(agent, path, body)
    assert code == 400 and answer["error"] == "bad_request"
    assert agent.status()["lend"]["switch"] is True


def test_a_ledger_that_cannot_be_used_is_503(agent, monkeypatch, tmp_path):
    from leasehold.ledger import LedgerUnavailable

    def broken(*_a, **_k):
        raise LedgerUnavailable("the ledger is gone", problem="missing", path=tmp_path)

    monkeypatch.setattr(agent, "status", broken)
    code, answer = handle(agent, "GET", "/v1/status", None, ADMIN)
    assert (code, answer["error"]) == (503, "ledger_unavailable")
    assert "gone" in answer["detail"]
