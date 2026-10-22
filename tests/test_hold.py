"""`leasehold hold`'s contract: its exit code for each way its wait in line ends
(`leasehold.exits`), and what happens to its command when the lease is revoked, a signal comes,
or the command ends or takes jobserver tokens."""

from __future__ import annotations

import signal
import subprocess
import sys

import pytest
from support import CHILD_ENV, cli, gone, until

import leasehold
from leasehold.client import Client

#: case: (hold's options, whether a daemon is there, exit code, what it says on stderr). The one
#: `external` slot is held throughout.
CASES = {
    "granted": ([], True, 3, ""),
    "busy": (["--kind", "external", "--timeout", "0.3"], True, 75, "gave up after 0.3s in line"),
    "refused": (["--pool", "nope"], True, 77, "refused: there is no pool 'nope'"),
    "maintenance drains": (
        ["--kind", "maintenance", "--timeout", "0.3"],
        True,
        75,
        "draining: waiting for the 1 lease running to end",
    ),
    "until without maintenance": (["--until", "07:00"], True, 1, "are for --kind maintenance"),
    "no daemon": ([], False, 0, "running without a lease"),
    "no daemon, fail": (["--if-unavailable", "fail"], False, 69, "is a daemon running?"),
}


@pytest.mark.parametrize(("options", "there", "code", "says"), CASES.values(), ids=CASES.keys())
def test_how_a_wait_ends(daemon, sock_dir, options, there, code, says):
    sock = daemon.path if there else sock_dir / "gone.sock"
    with leasehold.lease(kind="external", socket=daemon.path):
        done = cli(sock, "hold", *options, "--", "sh", "-c", f"exit {code if code == 3 else 0}")
    assert done.returncode == code
    assert says in done.stderr


def test_maintenance_drains_the_machine_and_tells_waiters_until_when(daemon):
    client = Client(daemon.path)
    running = client.acquire(kind="external", ttl_s=30, wait_s=0)["lease"]["lease_id"]
    window = ["--label", "name=benchmark-window", "--reason", "benchmark window"]
    taking = hold(daemon.path, "--kind", "maintenance", "--until", "07:00", *window,
                  "--", "sh", "-c", "echo in; read line", stdin=subprocess.PIPE)  # fmt: skip
    until(lambda: (client.status()["maintenance"] or {}).get("state") == "draining")
    # Work that would fit waits behind the drain, the owner's own included.
    after = client.acquire(kind="work", ttl_s=30, wait_s=0)
    assert after["status"] == "queued"
    assert after["blocked"] == "machine draining for maintenance until 07:00 UTC (benchmark window)"
    client.release(running)
    assert taking.stdout.readline() == "in\n"
    st = client.status()
    assert st["maintenance"]["state"] == "held"
    assert [(x["kind"], x["weight"]) for x in st["leases"]] == [("maintenance", st["slots"])]
    assert st["queue"][0]["blocked"].startswith("machine in maintenance until 07:00 UTC")
    refused = cli(daemon.path, "hold", "--timeout", "0.3", "--", "true")
    assert refused.returncode == 75
    until_at = st["maintenance"]["until"]
    assert f"held by benchmark-window (maintenance) until {until_at}" in refused.stderr
    taking.stdin.write("\n")
    taking.stdin.close()
    assert taking.wait(timeout=10) == 0
    until(lambda: [x["kind"] for x in client.leases()["leases"]] == ["work"])
    assert client.status()["maintenance"] is None


def hold(sock, *args, **kw) -> subprocess.Popen:
    """`leasehold hold ARGS` started in a child process, uncontained."""
    env = {**CHILD_ENV, "LEASEHOLD_CONTAIN": "off"}
    return subprocess.Popen(
        [sys.executable, "-m", "leasehold", "--socket", str(sock), "hold", *args],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw,
    )  # fmt: skip


def test_a_revoked_lease_stops_the_command(daemon):
    until(lambda: Client(daemon.path).status()["lend"]["eligible"])
    run = hold(daemon.path, "--kind", "lend", "--ttl", "0.3", "--grace", "1", "--", "sleep", "30")
    until(lambda: Client(daemon.path).leases()["leases"])
    daemon.agent.set_lend(False)
    assert run.wait(timeout=10) == 76
    assert "lease revoked" in run.stderr.read()


def test_a_signal_reaches_the_command_and_its_end_is_holds(daemon):
    run = hold(
        daemon.path, "--", "sh", "-c", "trap 'exit 7' TERM; echo up; while :; do sleep 0.1; done"
    )
    assert run.stdout.readline() == "up\n"
    run.send_signal(signal.SIGTERM)
    assert run.wait(timeout=10) == 7


def test_what_the_command_leaves_in_its_group_is_stopped(daemon):
    run = hold(daemon.path, "--grace", "1", "--", "sh", "-c", "sleep 30 & echo $!")
    left = int(run.stdout.readline())
    assert run.wait(timeout=10) == 0
    until(lambda: gone(left))


#: A job that takes one token from the jobserver `MAKEFLAGS` names, keeps it until told, and
#: gives it back: the sign a range follows its jobserver.
TAKE_TOKEN = """
import os, re, sys
fifo = re.search(r"--jobserver-auth=fifo:(\\S+)", os.environ["MAKEFLAGS"]).group(1)
fd = os.open(fifo, os.O_RDWR)
token = os.read(fd, 1)
print("took", flush=True)
sys.stdin.readline()
os.write(fd, token)
"""


def test_a_range_whose_jobs_take_tokens_becomes_elastic(daemon):
    run = hold(
        daemon.path, "--weight", "1-2", "--jobserver", "--", sys.executable, "-c", TAKE_TOKEN,
        stdin=subprocess.PIPE,
    )  # fmt: skip
    assert run.stdout.readline() == "took\n"
    until(lambda: [x["elastic"] for x in Client(daemon.path).leases()["leases"]] == [True])
    run.stdin.write("\n")
    run.stdin.close()
    assert run.wait(timeout=10) == 0
