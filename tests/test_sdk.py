"""The Python API (`leasehold.leasing`) against a real daemon: what a lease is granted, how a wait
ends without one, and what a lease the daemon takes back or forgets tells its holder."""

from __future__ import annotations

import inspect
import threading

import pytest
from support import until

import leasehold
from leasehold.client import Client


def held(sock) -> set[str]:
    return {x["lease_id"] for x in Client(sock).leases()["leases"]}


def queued(sock) -> list:
    return Client(sock).leases()["queue"]


def test_the_supported_names():
    assert sorted(leasehold.__all__) == [
        "Busy", "Cancelled", "DaemonError", "Lease", "LeaseError", "LeaseLost", "Refused",
        "Unavailable", "Waiting", "__version__", "find_daemon", "lease",
    ]  # fmt: skip
    assert list(inspect.signature(leasehold.lease).parameters) == [
        "weight", "kind", "pool", "labels", "session", "timeout", "ttl", "memory",
        "memory_per_slot", "if_unavailable", "on_wait", "on_lost", "cancel", "socket", "client",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("weight", "granted", "env_weight"),
    [(1, (1, 1, 1), "1"), ((1, 2), (2, 1, 2), "2")],
)
def test_a_lease_is_held_until_its_block_ends(daemon, weight, granted, env_weight):
    with pytest.raises(RuntimeError), leasehold.lease(weight, socket=daemon.path) as lease:
        assert lease.held and lease.lost is None
        assert (lease.weight, lease.min_weight, lease.max_weight) == granted
        assert lease.env({})["LEASEHOLD_WEIGHT"] == env_weight
        assert lease.lease_id in held(daemon.path)
        raise RuntimeError("the block fails")
    assert lease.lease_id not in held(daemon.path)


def test_a_wait_that_times_out_is_busy_and_gives_its_place_back(daemon):
    seen = []
    with leasehold.lease(kind="external", socket=daemon.path):
        with pytest.raises(leasehold.Busy) as busy:
            leasehold.lease(kind="external", timeout=0.3, socket=daemon.path, on_wait=seen.append)
        assert busy.value.position == 1
        assert seen and seen[0].position == 1
        assert queued(daemon.path) == []


def test_a_cancelled_wait_gives_its_place_back(daemon):
    cancel = threading.Event()
    with leasehold.lease(kind="external", socket=daemon.path):
        threading.Timer(0.3, cancel.set).start()
        with pytest.raises(leasehold.Cancelled):
            leasehold.lease(kind="external", cancel=cancel, socket=daemon.path)
        assert queued(daemon.path) == []


def test_a_refusal_is_refused(daemon):
    with pytest.raises(leasehold.Refused):
        leasehold.lease(pool="no-such-pool", socket=daemon.path)


def test_with_no_daemon_it_runs_unleased_or_fails(sock_dir):
    missing = sock_dir / "none.sock"
    lease = leasehold.lease(socket=missing)
    assert not lease.held and lease.lease_id is None
    assert "LEASEHOLD_LEASE_ID" not in lease.env({"LEASEHOLD_WANT": "2"})
    with pytest.raises(leasehold.Unavailable):
        leasehold.lease(socket=missing, if_unavailable="fail")


def test_a_revoked_lend_lease_is_lost(daemon):
    heard = []
    until(lambda: Client(daemon.path).status()["lend"]["eligible"])
    with leasehold.lease(kind="lend", ttl=0.3, socket=daemon.path, on_lost=heard.append) as lease:
        daemon.agent.set_lend(False)
        until(lambda: lease.lost is not None)
        assert heard == [lease.lost]
        with pytest.raises(leasehold.LeaseLost):
            lease.check()


def test_a_work_lease_the_daemon_forgot_is_asked_for_again(daemon):
    with leasehold.lease(ttl=0.3, socket=daemon.path) as lease:
        Client(daemon.path).release(lease.lease_id)
        until(lambda: lease.lease_id in held(daemon.path))
        assert lease.lost is None
