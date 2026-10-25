"""The Python API (`leasehold.leasing`) against a real daemon: what a lease is granted, how a wait
ends without one, and what a lease the daemon takes back or forgets tells its holder."""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import threading

import pytest
from support import gone, make_agent, until

import leasehold
from leasehold import contain
from leasehold.agent import AcquireRequest
from leasehold.client import Client


def held(sock) -> set[str]:
    return {x["lease_id"] for x in Client(sock).leases()["leases"]}


def queued(sock) -> list:
    return Client(sock).leases()["queue"]


def test_the_supported_names():
    assert sorted(leasehold.__all__) == [
        "Busy", "Cancelled", "Command", "DaemonError", "Lease", "LeaseError", "LeaseLost",
        "NotStarted", "Refused", "Unavailable", "Waiting", "__version__", "find_daemon", "lease",
    ]  # fmt: skip
    # Callers pass these by keyword, so their names are part of the API.
    assert list(inspect.signature(leasehold.lease).parameters) == [
        "weight", "kind", "pool", "labels", "session", "timeout", "ttl", "memory",
        "memory_per_slot", "if_unavailable", "on_wait", "on_lost", "cancel", "socket", "client",
    ]  # fmt: skip
    with pytest.raises(ValueError, match=r"\(least, most\)"):
        leasehold.lease((3, 1))


@pytest.mark.parametrize(
    ("weight", "granted", "env_weight"),
    [(1, (1, 1, 1, 1), "1"), ((1, 2), (2, 1, 2, 2), "2")],
)
def test_a_lease_is_held_until_its_block_ends(daemon, weight, granted, env_weight):
    with pytest.raises(RuntimeError), leasehold.lease(weight, socket=daemon.path) as lease:
        assert lease.held and lease.lost is None
        assert (lease.weight, lease.min_weight, lease.max_weight, lease.jobs) == granted
        assert lease.env({})["LEASEHOLD_WEIGHT"] == env_weight
        assert lease.env({})["LEASEHOLD_JOBS"] == env_weight
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


@pytest.mark.parametrize("ask", [{"pool": "no-such-pool"}, {"kind": "external", "weight": 9}])
def test_a_request_that_can_never_be_granted_is_refused_with_its_code(daemon, ask):
    with pytest.raises(leasehold.Refused) as refused:
        leasehold.lease(socket=daemon.path, **ask)
    assert refused.value.code == "never_grantable"


def test_with_no_daemon_it_runs_unleased_or_fails(sock_dir, monkeypatch, tmp_path):
    missing = sock_dir / "none.sock"
    lease = leasehold.lease(socket=missing)
    assert not lease.held and lease.lease_id is None
    assert lease.weight is None and lease.jobs is None
    assert "LEASEHOLD_LEASE_ID" not in lease.env({"LEASEHOLD_WANT": "2"})
    with pytest.raises(leasehold.Unavailable):
        leasehold.lease(socket=missing, if_unavailable="fail")

    bad = tmp_path / "bad.toml"
    bad.write_text("mode = 'nonsense'\n")
    monkeypatch.setenv("LEASEHOLD_CONFIG", str(bad))
    lease = leasehold.lease()
    assert not lease.held and lease.lease_id is None
    with pytest.raises(leasehold.Unavailable) as unavailable:
        leasehold.lease(if_unavailable="fail")
    assert str(bad) in str(unavailable.value)
    assert "leasehold doctor" in str(unavailable.value)


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
    with leasehold.lease(ttl=3, socket=daemon.path) as lease:
        Client(daemon.path).release(lease.lease_id)
        until(lambda: lease.lease_id in held(daemon.path))
        (now,) = [
            x for x in Client(daemon.path).leases()["leases"] if x["lease_id"] == lease.lease_id
        ]
        until(lambda: lease.env({})["LEASEHOLD_GRANT"] == str(now["grant"]))
        assert lease.lost is None

        previous = lease.env({})
        client = Client(daemon.path)
        client.release(lease.lease_id)
        blocker = client.acquire(weight=2, kind="work", ttl_s=3, wait_s=0)
        assert blocker["status"] == "granted"
        try:
            until(lambda: any(x["lease_id"] == lease.lease_id for x in queued(daemon.path)))
            assert lease.env({}) == previous
        finally:
            client.release(blocker["lease"]["lease_id"])
        until(lambda: lease.lease_id in held(daemon.path))
        (now,) = [x for x in client.leases()["leases"] if x["lease_id"] == lease.lease_id]
        assert str(now["grant"]) != previous["LEASEHOLD_GRANT"]
        until(lambda: lease.env({})["LEASEHOLD_GRANT"] == str(now["grant"]))
        assert lease.lost is None


def test_a_command_runs_in_the_lease_and_its_leftovers_stop_with_it(daemon):
    with leasehold.lease(socket=daemon.path) as lease:
        mine = ["sh", "-c", f'test "$LEASEHOLD_LEASE_ID" = {lease.lease_id} && exit 4']
        assert lease.run(mine, contain=False) == 4
        flags = [
            "sh",
            "-c",
            'case "$MAKEFLAGS" in *--keep-going*--jobserver-auth*) exit 0;; esac; exit 1',
        ]
        assert (
            lease.run(
                flags,
                jobserver=True,
                contain=False,
                env={**os.environ, "MAKEFLAGS": "--keep-going"},
            )
            == 0
        )
        with lease.popen(["sh", "-c", "sleep 30 & echo $!; exec sleep 30"], contain=False,
                         grace=1, stdout=subprocess.PIPE, text=True) as command:  # fmt: skip
            left = int(command.process.stdout.readline())
    until(lambda: gone(left))


def test_a_revoked_lend_lease_stops_its_command(daemon):
    until(lambda: Client(daemon.path).status()["lend"]["eligible"])
    with leasehold.lease(kind="lend", ttl=0.3, socket=daemon.path) as lease:
        command = lease.popen(["sleep", "30"], contain=False, grace=1)
        daemon.agent.set_lend(False)
        until(lambda: gone(command.pid))  # stopped with no one waiting for it
        assert command.stopped_for == lease.lost
        assert command.wait(timeout=10) == -signal.SIGTERM


@pytest.mark.parametrize(("kills", "certain"), [(1, False), (0, None)])
def test_a_command_killed_for_memory_says_so(daemon, oom_kills, kills, certain):
    with leasehold.lease(socket=daemon.path) as lease:
        command = lease.popen(["sleep", "30"], contain=False)
        oom_kills(kills)
        os.kill(command.pid, signal.SIGKILL)
        assert command.wait(timeout=10) == -signal.SIGKILL  # the exit status is its own
    if certain is None:
        assert command.memory is None
        return
    assert (command.memory.kind, command.memory.certain) == ("kernel", certain)
    assert "not a test failure" in command.memory.agent
    assert lease.memory is command.memory


@pytest.mark.skipif(not contain.probe().enforced, reason="needs a systemd user manager")
def test_a_command_past_its_own_memory_cap_is_stopped_and_told_so(daemon):
    with leasehold.lease(socket=daemon.path) as lease:
        over = [sys.executable, "-c", "b = b'x' * (256 << 20)"]
        assert lease.run(over, memory_max="64m") == -signal.SIGKILL
    assert (lease.memory.kind, lease.memory.certain, lease.memory.limit_bytes) == (
        "cap",
        True,
        64 << 20,
    )


def test_the_daemon_hands_out_its_memory_caps_in_bytes(tmp_path):
    # `[commands] memory_max` goes with every grant; a rule's own cap is served with the rule.
    off = {"programs": ["pytest"], "weight": 1, "memory_max": "off"}
    agent, _, _ = make_agent(tmp_path, commands={"memory_max": "50%", "rule": [off]})
    granted = agent.acquire(AcquireRequest(kind="work"), user="me")
    assert granted["contain"]["memory_max"] == f"{8 << 30}b"  # half of the machine's 16 GiB
    assert agent.commands()["rules"][0]["memory_max"] == "off"
    assert agent.status()["contain"]["memory_cap"]["how"] in ("exact", "none")
