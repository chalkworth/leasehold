"""What an agent is told when the machine stayed busy (leasehold.busy): who holds it, why the
command waits and its place in line."""

from __future__ import annotations

from leasehold import busy


def lease(weight, *, pool="work", cmd=None, session=None, kind="work", **labels) -> dict:
    labels = labels | ({"cmd": cmd} if cmd else {})
    return {
        "lease_id": "l1", "pool": pool, "weight": weight, "labels": labels, "session": session,
        "kind": kind,
    }  # fmt: skip


QUEUED = {"status": "queued", "position": 1, "blocked": "pool work holds 14 of 14; this needs 4"}


def test_the_heaviest_holder_in_the_pool_is_named_with_the_rest_and_the_place_in_line():
    leases = [
        lease(2, cmd="make", session="s1"),
        lease(8, cmd="cargo test", session="cc-ab12"),
        lease(4, cmd="pytest"),
        lease(9, pool="external", cmd="ci job"),
    ]
    assert busy.reason(QUEUED, leases, pool="work", waited_s=20) == (
        "machine busy for 20s: cargo test (session cc-ab12) holds 8 slots, 2 other leases "
        "hold 6; pool work holds 14 of 14; this needs 4; queued #1"
    )


def test_a_lone_holder_a_shim_and_a_lease_with_no_labels():
    assert busy.reason(QUEUED, [lease(1, shim="make")], pool="work").startswith(
        "machine busy: make holds 1 slot; pool work"
    )
    one_more = [lease(3, kind="external"), lease(1, cmd="x")]
    assert "external lease l1 holds 3 slots, 1 other lease holds 1;" in busy.reason(
        QUEUED, one_more, pool="work"
    )


def test_with_nothing_in_its_pool_the_machines_heaviest_leases_are_named():
    blocked = {"position": 2, "blocked": "0 of 8 slots are free; this needs 2"}
    leases = [lease(6, pool="external", cmd="ci job")]
    assert busy.reason(blocked, leases, pool="work") == (
        "machine busy: ci job holds 6 slots; 0 of 8 slots are free; this needs 2; queued #2"
    )


def test_with_nothing_known_it_still_says_the_machine_is_busy():
    assert busy.reason({}, [], pool="work") == "machine busy: no room"


def test_told_lists_nothing_when_no_daemon_answers(sock_dir):
    from leasehold.client import Client

    assert busy.told(Client(sock_dir / "none.sock"), QUEUED, pool="work", waited_s=1) == (
        "machine busy for 1s: pool work holds 14 of 14; this needs 4; "
        "gave up its place in line (#1)"
    )
