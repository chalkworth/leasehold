"""A lease handed between processes (`leasehold acquire --keep`, `release`), with real processes
against a daemon on a real socket: the lease outlives the process that asked for it, a later
process releases it, and when nobody does, the TTL still ends it -- the renewer killed, its
watched pid gone, or `--max-hold` reached."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import CHILD_ENV, cli, until

from leasehold.cli import main
from leasehold.client import Client, Unavailable
from leasehold.exits import EXIT_BUSY, EXIT_UNAVAILABLE
from leasehold.handoff import keep
from leasehold.process import process_birth, process_matches


@pytest.fixture
def keepers():
    """Pids of renewers a test started, killed afterwards whatever happened."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def held(client: Client) -> list[str]:
    return [x["lease_id"] for x in client.leases()["leases"]]


def running(pid: int) -> bool:
    birth = process_birth(pid)
    return birth is not None and process_matches(pid, birth)


def acquire(sock: Path, keepers: list[int], *args: str) -> dict:
    done = cli(sock, "acquire", *args)
    assert done.returncode == 0, done.stderr
    lease = json.loads(done.stdout)
    if "keeper_pid" in lease:
        keepers.append(lease["keeper_pid"])
    return lease


def test_a_kept_lease_outlives_its_acquirer_until_another_process_releases_it(daemon, keepers):
    client = Client(daemon.path)
    lease = acquire(
        daemon.path, keepers, "--lease-id", "job.1", "--weight", "1", "--label", "consumer=ci",
        "--ttl", "2.0", "--keep",
    )  # fmt: skip
    assert lease["lease_id"] == "job.1" and lease["labels"] == {"consumer": "ci"}
    assert lease["kind"] == "work"  # the default kind
    # Over two TTLs after the acquiring process exited. The TTL leaves a loaded test machine
    # room for one late renewal: at 0.6 s a single slow keeper tick could end the lease.
    time.sleep(5.0)
    assert held(client) == ["job.1"]
    assert running(lease["keeper_pid"])
    done = cli(daemon.path, "release", "job.1")
    assert done.returncode == 0, done.stderr
    assert held(client) == []
    until(lambda: not running(lease["keeper_pid"]))  # it saw the release and stopped
    assert held(client) == []


def test_without_a_release_the_ttl_still_ends_the_lease(daemon, keepers):
    client = Client(daemon.path)
    # The renewer killed: nothing renews, so the lease ends one TTL later.
    lease = acquire(daemon.path, keepers, "--lease-id", "a", "--ttl", "0.6", "--keep")
    os.kill(lease["keeper_pid"], signal.SIGKILL)
    until(lambda: "a" not in held(client), timeout=5)
    # The watched process gone.
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        lease = acquire(
            daemon.path, keepers, "--lease-id", "b", "--ttl", "2", "--keep",
            "--watch-pid", str(sleeper.pid),
        )  # fmt: skip
        # Longer than the keeper's renewal period (TTL/3), well inside the TTL, so a loaded
        # machine that delays one renewal does not lose the lease.
        time.sleep(1.0)
        assert held(client) == ["b"]
        sleeper.kill()
        sleeper.wait()
        until(lambda: "b" not in held(client), timeout=5)
        until(lambda: not running(lease["keeper_pid"]))
    finally:
        sleeper.kill()
    # --max-hold reached.
    acquire(daemon.path, keepers, "--lease-id", "c", "--ttl", "0.6", "--keep", "--max-hold", "1")
    until(lambda: "c" not in held(client), timeout=5)
    # And no --keep at all: one TTL.
    acquire(daemon.path, keepers, "--lease-id", "d", "--ttl", "0.6")
    until(lambda: "d" not in held(client), timeout=5)


def test_acquire_waits_in_line_and_exits_75_at_its_timeout(daemon, keepers):
    client = Client(daemon.path)
    acquire(daemon.path, keepers, "--lease-id", "first", "--kind", "external", "--keep")
    done = cli(
        daemon.path, "acquire", "--lease-id", "second", "--kind", "external", "--timeout", "0.5"
    )
    assert done.returncode == EXIT_BUSY and done.stdout == ""
    assert "waiting for 1 slot (external): place 1; pool external holds 1 of 1" in done.stderr
    assert "gave up" in done.stderr
    assert client.leases()["queue"] == []  # the place in line was given back
    # Released, the next in line gets it.
    waiting = subprocess.Popen(
        [sys.executable, "-m", "leasehold", "--socket", str(daemon.path), "acquire",
         "--lease-id", "second", "--kind", "external", "--timeout", "20"],
        env=CHILD_ENV, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    until(lambda: client.leases()["queue"] != [])
    assert cli(daemon.path, "release", "first").returncode == 0
    out, _ = waiting.communicate(timeout=20)
    assert waiting.returncode == 0 and json.loads(out)["lease_id"] == "second"


def test_the_session_comes_from_the_flag_or_the_environment(daemon, monkeypatch, capsys):
    sock = str(daemon.path)
    assert main(["--socket", sock, "acquire", "--lease-id", "a", "--session", "term-1"]) == 0
    assert json.loads(capsys.readouterr().out)["session"] == "term-1"
    monkeypatch.setenv("LEASEHOLD_SESSION", "term-2")
    assert main(["--socket", sock, "acquire", "--lease-id", "b"]) == 0
    assert json.loads(capsys.readouterr().out)["session"] == "term-2"


def test_no_daemon_is_exit_69_for_acquire_and_release(sock_dir):
    missing = sock_dir / "absent.sock"
    assert cli(missing, "acquire", "--lease-id", "x").returncode == EXIT_UNAVAILABLE
    assert cli(missing, "release", "x").returncode == EXIT_UNAVAILABLE


class _FakeClient:
    """A client whose renewals answer from a script (an exception is raised), then `ok`."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.renewals = 0

    def renew(self, lease_id, ttl_s=None):
        self.renewals += 1
        answer = self.answers.pop(0) if self.answers else {"status": "ok"}
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_keep_rides_out_an_unreachable_daemon_and_stops_on_anything_but_ok():
    now = [0.0]

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    client = _FakeClient([Unavailable("down"), {"status": "ok"}, {"status": "revoked"}])
    reason = keep(client, "x", ttl_s=3, clock=clock, sleep=sleep)
    assert reason == "the lease is revoked" and client.renewals == 3
    client = _FakeClient([])
    reason = keep(client, "x", ttl_s=3, max_hold_s=10, clock=clock, sleep=sleep)
    assert reason == "held for --max-hold 10s" and client.renewals == 10
