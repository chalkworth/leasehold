"""`leasehold hold`'s contract: its exit code for each way its wait in line ends
(`leasehold.exits`), and what happens to its command when the lease is revoked, a signal comes,
or the command ends or takes jobserver tokens."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from support import CHILD_ENV, cli, gone, until

import leasehold
from leasehold import contain
from leasehold.client import Client

#: case: (hold's options, whether a daemon is there, exit code, what it says on stderr, its
#: environment beyond the tests'). The one `external` slot is held throughout.
CASES = {
    "granted": ([], True, 3, "", {}),
    "busy": (
        ["--kind", "external", "--timeout", "0.3"], True, 75, "gave up after 0.3s in line", {}
    ),
    "busy under an agent's tool timeout": (
        ["--kind", "external"], True, 75, "LEASEHOLD_TOOL_TIMEOUT_S",
        {"LEASEHOLD_TOOL_TIMEOUT_S": "0.6"},
    ),
    "refused": (["--pool", "nope"], True, 77, "refused: there is no pool 'nope'", {}),
    "maintenance drains": (
        ["--kind", "maintenance", "--timeout", "0.3"],
        True,
        75,
        "whole machine (maintenance): place 1; draining: waiting for the 1 lease running to end",
        {},
    ),
    "until without maintenance": (["--until", "07:00"], True, 1, "are for --kind maintenance", {}),
    "a cap that is no size": (["--memory-max", "lots"], True, 2, "give a size such as", {}),
    "a cap with no daemon": (["--memory-max", "1g"], False, 0, "no memory cap of 1.0 GiB", {}),
    "no daemon": ([], False, 0, "running without a lease", {}),
    "no daemon, fail": (["--if-unavailable", "fail"], False, 69, "is a daemon running?", {}),
}  # fmt: skip


@pytest.mark.parametrize(
    ("options", "there", "code", "says", "env"), CASES.values(), ids=CASES.keys()
)
def test_how_a_wait_ends(daemon, sock_dir, options, there, code, says, env):
    sock = daemon.path if there else sock_dir / "gone.sock"
    with leasehold.lease(kind="external", socket=daemon.path):
        done = cli(
            sock, "hold", *options, "--", "sh", "-c", f"exit {code if code == 3 else 0}",
            env={**CHILD_ENV, **env},
        )  # fmt: skip
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


#: Writes 512 MiB, killable by the OOM killer whatever `oom_score_adj` the tests run with.
WRITE_512M = """echo 0 > /proc/self/oom_score_adj; exec "$1" -c "b = b'x' * (512 << 20)" """


#: case: (hold's options, whether the command's scope goes in a session slice held to 64 MiB
#: with no swap, what the memory verdict says).
OOM_CASES = {
    "a limit above it": ([], True, "OOM killer"),
    "its own cap": (["--memory-max", "64m"], False, "leasehold's cap of 64 MiB for this command"),
}


@pytest.mark.skipif(not contain.probe().enforced, reason="needs a systemd user manager")
@pytest.mark.parametrize(("options", "slice_", "says"), OOM_CASES.values(), ids=OOM_CASES.keys())
def test_a_command_the_oom_killer_ends_is_named(daemon, options, slice_, says):
    # A session slice is what `leasehold run` makes: the command's scope goes in it.
    capped = contain.session_slice("oomtest", os.urandom(3).hex())
    limits = ["--runtime", capped, "MemoryMax=64M", "MemorySwapMax=0"]
    if slice_:
        subprocess.run(["systemctl", "--user", "set-property", *limits], check=True)
    try:
        done = cli(
            daemon.path, "hold", *options, "--", "sh", "-c", WRITE_512M, "sh", sys.executable,
            env={**CHILD_ENV, contain.ENV_SLICE: capped} if slice_ else CHILD_ENV,
        )  # fmt: skip
        peak_available = (
            Path("/sys/fs/cgroup") / contain.root_slice_cgroup() / "memory.peak"
        ).is_file()
    finally:
        if slice_:
            subprocess.run(["systemctl", "--user", "stop", capped], check=False)
            revert = ["systemctl", "--user", "revert", capped]
            subprocess.run(revert, check=False, capture_output=True)
    assert done.returncode == 128 + signal.SIGKILL  # the command's own
    last = done.stderr.splitlines()[-1]  # the memory verdict
    assert last.startswith("leasehold: ") and "not a test failure" in last
    assert says in last
    # A command its own cap stopped is told to change, not to declare more memory.
    assert ("leasehold counted no memory for it" in done.stderr) == slice_
    if peak_available:
        assert ("Declare at least --memory " in done.stderr) == slice_


@pytest.mark.skipif(not contain.probe().enforced, reason="needs a systemd user manager")
def test_a_command_under_a_memory_limit_stays_there_and_its_leftovers_lose_their_limits(daemon):
    # Run from inside a scope held to 1 GiB, as Claude Code's tool memory limit holds its
    # commands, the command stays in that scope, so a cap of its own does not hold.
    outer = f"lh-outer-{os.urandom(3).hex()}.scope"
    inside = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={outer}"]
    inside += ["-p", "MemoryMax=1G"]
    done = subprocess.run(
        [*inside, sys.executable, "-m", "leasehold", "--socket", str(daemon.path), "hold",
         "--memory-max", "64m", "--", "cat", "/proc/self/cgroup"],
        env=CHILD_ENV, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 0
    assert done.stdout.strip().endswith(f"/{outer}")
    assert "no memory cap of 64 MiB on cat: caller under a memory limit of 1.0 GiB" in done.stderr
    # A server the command starts outlives it in its scope, without the lease's cap.
    started = cli(daemon.path, "hold", "--memory-max", "64m", "--", "sh", "-c", "setsid sleep 2 &")
    assert started.returncode == 0
    assert "left sleep running (pid " in started.stderr


@pytest.mark.skipif(not contain.probe().enforced, reason="needs a systemd user manager")
def test_a_contained_command_is_not_exempt_from_the_oom_killer(daemon):
    # A negative value is raised to 0 and anything else is left as it was.
    inherited = int(Path("/proc/self/oom_score_adj").read_text())
    done = cli(daemon.path, "hold", "--", "cat", "/proc/self/oom_score_adj", env=CHILD_ENV)
    assert done.stdout.strip() == str(max(0, inherited))
