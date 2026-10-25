"""The Python API (`leasehold.leasing`) against a real daemon: what a lease is granted, how a wait
ends without one, what a lease the daemon takes back or forgets tells its holder, and the agent
sessions the daemon watches, with what its watchdog would pause."""

from __future__ import annotations

import inspect
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import replace

import pytest
from support import (
    FACTS,
    NO_SCOPE,
    Clock,
    FakeLoad,
    Orphan,
    gone,
    machine_config,
    make_agent,
    until,
)

import leasehold
from leasehold import host
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.client import ApiError, Client
from leasehold.host.posix import Posix
from leasehold.server import Daemon


def held(sock) -> set[str]:
    return {x["lease_id"] for x in Client(sock).leases()["leases"]}


def queued(sock) -> list:
    return Client(sock).leases()["queue"]


def test_the_supported_names():
    assert sorted(leasehold.__all__) == [
        "Busy", "Cancelled", "Command", "DaemonError", "Lease", "LeaseError", "LeaseLost",
        "NotStarted", "Refused", "Session", "Unavailable", "Waiting", "__version__",
        "find_daemon", "lease", "session",
    ]  # fmt: skip
    # Callers pass these by keyword, so their names are part of the API.
    assert list(inspect.signature(leasehold.lease).parameters) == [
        "weight", "kind", "pool", "labels", "session", "timeout", "ttl", "memory",
        "memory_per_slot", "if_unavailable", "on_wait", "on_lost", "cancel", "socket", "client",
    ]  # fmt: skip
    assert list(inspect.signature(leasehold.session).parameters) == [
        "name", "agent", "socket", "client", "env",
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


@pytest.mark.skipif(NO_SCOPE, reason="needs a systemd user manager")
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
    status = agent.status()
    assert status["contain"]["memory_cap"]["how"] in ("exact", "none")
    # The status says what the host can do, by operation, the cap among them.
    assert list(status["host"]) == list(host.OPERATIONS)
    assert status["host"]["memory_hard"]["how"] == status["contain"]["memory_cap"]["how"]


def test_a_session_is_watched_with_its_footprint_and_an_agent_is_found_by_name(sock_dir, tmp_path):
    config = machine_config(
        tmp_path, socket=str(sock_dir / "s.sock"), watchdog={"agents": ["lh-fake-agent"]}
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    fake = tmp_path / "lh-fake-agent"
    shutil.copy(shutil.which("sleep"), fake)
    agent = Orphan([str(fake), "30"])
    try:

        def watched() -> dict:
            found = Client(daemon.path).status()["watchdog"]["sessions"]
            return {s["session"]: s for s in found}

        with leasehold.session("sdk-test", socket=daemon.path) as joined:
            assert (joined.id, joined.registered) == ("sdk-test", True)
            assert os.environ["LEASEHOLD_SESSION"] == "sdk-test"
            work = subprocess.Popen(["sleep", "30"])
            until(lambda: (watched()["sdk-test"]["footprint_bytes"] or 0) > 0)
            mine = watched()["sdk-test"]
            assert (mine["source"], mine["agent_pid"], mine["how"]) == (
                "sdk",
                os.getpid(),
                "polled",
            )
            assert mine["trees"] >= 1
            work.kill()
            work.wait()
        assert "LEASEHOLD_SESSION" not in os.environ
        until(lambda: f"auto-{agent.pid}" in watched())
        found = watched()[f"auto-{agent.pid}"]
        assert (found["source"], found["agent_pid"]) == ("auto", agent.pid)
        assert "sdk-test" not in watched()
        with pytest.raises(ApiError) as unknown:
            Client(daemon.path).unregister_session("sdk-test")
        assert (unknown.value.status, unknown.value.payload["error"]) == (404, "unknown_session")
    finally:
        agent.kill()
        daemon.stop()


class FakeHost(Posix):
    """A machine whose processes and memory a test sets: `procs` by pid, each with its
    anonymous memory and start time, `commands` by pid, `available`, `swap` (total and free)
    and `stall` (the recent share every task stalled)."""

    def __init__(self) -> None:
        super().__init__()
        self.procs: dict[int, host.Proc] = {}
        self.commands: dict[int, str] = {}
        self.available = 12 << 30
        self.swap = (0, 0)
        self.stall = 0.0

    def support(self, operation: str) -> host.Support:
        if operation in ("members", "pressure", "session_processes"):
            return host.Support("exact", via="fake")
        return super().support(operation)

    def members(self, tree: host.Tree) -> list[host.Proc]:
        return [p for p in self.procs.values() if p.pid == tree.root]

    def session_processes(self, session: host.SessionRef) -> list[host.Proc]:
        procs, found = dict(self.procs), []
        for p in procs.values():
            up = p
            while up is not None and up.pid != session.agent_pid:
                up = procs.get(up.parent)
            if up is not None:
                found.append(replace(p, role="agent" if p.pid == session.agent_pid else "tool"))
        return found

    def named(self, names) -> list[host.Proc]:
        return [p for p in dict(self.procs).values() if p.name in names]

    def pressure(self) -> host.Pressure:
        total, free = self.swap
        return host.Pressure(
            "exact", available=self.available, total=16 << 30, swap_total=total,
            swap_used=total - free, stall_full_recent=self.stall,
        )  # fmt: skip

    def command_line(self, proc: host.Proc) -> str | None:
        return self.commands.get(proc.pid, proc.name)


GIB = 1 << 30


def test_the_watchdog_says_what_it_would_do(sock_dir, tmp_path):
    # On a 16 GiB machine, two agents: 100 registers, 200 is only found by name. Each reading is
    # (available GiB, swap total and free GiB, the recent stall %, the level it gives); each
    # tree (pid, parent, start after its agent in s, command, anonymous GiB at each reading).
    calm, swap = (0, 0), (8, 6)
    cases = [
        ("the newest", [
            (10, calm, 0, "ok"), (6, calm, 0, "ok"), (2.5, calm, 0, "warn"),
            (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "make -j8", [1, 1, 1, 1]),
            (102, 100, 120, "python3 -m pytest -n 4", [1, 1, 1, 1]),
        ], (102, "newest", [])),
        ("a runaway by its own growth, before a newer tree", [
            (8, calm, 0, "ok"), (6, calm, 0, "ok"), (4, calm, 0, "ok"),
            (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "cargo test", [1, 2, 3, 4]),
            (102, 100, 120, "pytest -n 4", [1, 1, 1, 1]),
        ], (101, "runaway", [])),
        ("a tree over a quarter of memory, before a newer tree", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "bazel build //...", [5, 5]),
            (102, 100, 120, "pytest -n 4", [1, 1]),
        ], (101, "large", [])),
        ("with swap free, only stalls are critical", [
            (6, swap, 0, "ok"), (1.5, swap, 0, "ok"), (1.5, swap, 30, "critical"),
        ], [
            (101, 100, 60, "make -j8", [1, 1, 1]),
            (102, 100, 120, "pytest -n 4", [1, 1, 1]),
        ], (102, "newest", [])),
        ("leasehold's own process is spared, the command is not", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "pytest -n 4", [1, 1]),
            (106, 100, 120, "leasehold hold -- make -j8", [0.05, 0.05]),
            (107, 106, 121, "make -j8", [1, 1]),
        ], (106, "newest", [106])),
        ("never a protected tree", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "pytest -n 4", [1, 1]),
            (102, 100, 120, "node my-dev-server.js", [2, 2]),  # [watchdog] protect
            (103, 100, 5, "python -m some_lsp", [2, 2]),  # started with its agent
            (104, 100, 90, "npx -y @modelcontextprotocol/server-x", [2, 2]),
            (105, 100, 100, "sleep 60", [0.1, 0.1]),  # under min_size
            (108, 100, 110, "leasehold mcp", [0.4, 0.4]),  # only leasehold's own
        ], (101, "newest", [])),
        ("never a session found by name", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "pytest -n 4", [1, 1]),
            (201, 200, 120, "cargo build", [3, 3]),
        ], (101, "newest", [])),
    ]  # fmt: skip
    for n, (name, readings, trees, (chosen, why, spared)) in enumerate(cases):
        fake, mono = FakeHost(), Clock(0.0)
        for pid in (100, 200):
            fake.procs[pid] = host.Proc(
                pid, str(pid), "fake-agent", parent=1, started=1000.0, anon=GIB
            )
        (tmp_path / str(n)).mkdir()
        config = machine_config(
            tmp_path / str(n),
            socket=str(sock_dir / "s.sock"),
            watchdog={"agents": ["fake-agent"], "protect": ["dev-server"]},
        )
        agent = MachineAgent(
            config, facts=FACTS, load=FakeLoad(), monotonic=mono, platform=lambda f=fake: f
        )
        daemon = Daemon(config, agent)
        daemon.start()
        try:
            Client(daemon.path).register_session("mine", pid=100, agent="fake-agent")
            levels = []
            for step, (free, (total, unused), stall, _) in enumerate(readings):
                for pid, parent, after, command, sizes in trees:
                    fake.procs[pid] = host.Proc(
                        pid, str(pid), command.split()[0], parent=parent,
                        started=1000.0 + after, anon=int(sizes[step] * GIB),
                    )  # fmt: skip
                    fake.commands[pid] = command
                fake.available, fake.stall = int(free * GIB), stall
                fake.swap = (total * GIB, unused * GIB)
                mono.now += 2.5
                agent.tick()
                agent.watch_memory()
                watchdog = Client(daemon.path).status()["watchdog"]
                levels.append(watchdog["level"])
                if watchdog["level"] == "warn":
                    assert watchdog["would"] == {"action": "hold"}, name
            assert levels == [r[3] for r in readings], name
            would = watchdog["would"]
            assert (would["action"], would["pid"], would["why"]) == ("pause", chosen, why), name
            assert would["spared"] == spared, name
            events = Client(daemon.path).events()["events"]
            paused = [(e["pid"], e["would"]) for e in events if e["type"] == "paused"]
            assert paused == [(chosen, True)], name
            held = [e["would"] for e in events if e["type"] == "held"]
            assert held == ([True] if "warn" in levels else []), name
        finally:
            daemon.stop()
    # Where the host cannot read the machine's memory, there is no level, and status says why.
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"))
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad(), platform=Posix)
    agent.watch_memory()
    watchdog = agent.status()["watchdog"]
    assert (watchdog["level"], watchdog["would"]) == (None, None)
    assert watchdog["reason"].startswith("the watchdog cannot see memory here")
