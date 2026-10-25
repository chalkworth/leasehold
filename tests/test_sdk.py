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
    CHILD_ENV,
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
from leasehold import claude_code, host
from leasehold import watchdog as watchdog_module
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.client import ApiError, Client
from leasehold.host.linux import Linux
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
    anonymous memory and start time, `commands` by pid, `available`, `swap` (total and free),
    `stall` (the recent share every task stalled) and `frees` (what stopping a process would
    free, by pid: its anonymous memory where not set)."""

    def __init__(self) -> None:
        super().__init__()
        self.done: list[tuple[str, int]] = []
        self.procs: dict[int, host.Proc] = {}
        self.commands: dict[int, str] = {}
        self.available = 12 << 30
        self.swap = (0, 0)
        self.stall = 0.0
        self.frees: dict[int, int] = {}

    def freeable(self, proc: host.Proc) -> int | None:
        return self.frees.get(proc.pid, proc.anon)

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

    def pause(self, tree: host.Tree) -> host.Done:
        self.done.append(("pause", tree.root))
        return host.Done("sigstop")

    def resume(self, tree: host.Tree) -> host.Done:
        self.done.append(("resume", tree.root))
        return host.Done("sigcont")

    def kill(self, tree: host.Tree, grace_s: float) -> host.Done:
        self.done.append(("kill", tree.root))
        self.procs.pop(tree.root, None)
        return host.Done("signal")


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
        ("a runaway by its own growth, over two windows, before a newer tree", [
            (8, calm, 0, "ok"), (6.5, calm, 0, "ok"), (5, calm, 0, "ok"), (3.5, calm, 0, "ok"),
            (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "cargo test", [1, 2, 3, 4, 5]),
            (102, 100, 120, "pytest -n 4", [1, 1, 1, 1, 1]),
        ], (101, "runaway", [])),
        ("no runaway that grows slower than memory falls elsewhere", [
            (12, calm, 0, "ok"), (10, calm, 0, "ok"), (7.5, calm, 0, "ok"), (5, calm, 0, "ok"),
            (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "pytest -n 4", [1, 1.1, 1.2, 1.3, 1.4]),
            (102, 100, 120, "make -j8", [1, 1, 1, 1, 1]),
        ], (102, "newest", [])),
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
            (101, 100, 60, "sh -c pytest -n 4 >/tmp/code", [1, 1]),  # a path is no program
            (102, 100, 120, "node my-dev-server.js", [2, 2]),  # [watchdog] protect
            (103, 100, 5, "python -m some_lsp", [2, 2]),  # started with its agent
            (104, 100, 90, "npx -y @modelcontextprotocol/server-x", [2, 2]),
            (105, 100, 100, "sleep 60", [0.1, 0.1]),  # under min_size
            (108, 100, 110, "leasehold mcp", [0.4, 0.4]),  # only leasehold's own
            (109, 100, 115, "sh -c open-docs", [0.1, 0.1]),
            (110, 109, 116, "/usr/bin/firefox --new-tab x", [2, 2]),  # what its shell runs
            (111, 100, 117, "make -j8", [0.1, 0.1]),
            (112, 111, 118, "claude -p fix it", [2, 2]),  # an agent, anywhere in the tree
        ], (101, "newest", [])),
        ("an end-to-end suite's headless browser protects nothing", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "pytest -n 4", [1, 1]),
            (102, 100, 120, "sh -c npx playwright test", [0.01, 0.01]),
            (103, 102, 121, "node /x/node_modules/.bin/playwright test", [0.3, 0.3]),
            (104, 103, 122, "/x/ms-playwright/chromium-1140/chrome-linux/chrome --headless=new",
             [1, 1]),
        ], (102, "newest", [])),
        ("nor a path with mcp in it, nor a headless browser it runs", [
            (6, calm, 0, "ok"), (1.5, calm, 0, "critical"),
        ], [
            (101, 100, 60, "python3 ../mcp-x/run.py", [1, 1]),
            (102, 100, 120, "python mcpbench/run.py", [1, 1]),
            (103, 100, 130, "firefox -headless -marionette", [1, 1]),
        ], (103, "newest", [])),
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
                if watchdog["level"] != "critical":  # it acts at critical by default
                    assert watchdog["would"] is None, name
            assert levels == [r[3] for r in readings], name
            would = watchdog["would"]
            assert (would["action"], would["pid"], would["why"]) == ("pause", chosen, why), name
            assert would["spared"] == spared, name
            events = Client(daemon.path).events()["events"]
            paused = [(e["pid"], e["would"]) for e in events if e["type"] == "paused"]
            assert paused == [(chosen, True)], name
            assert not [e for e in events if e["type"] == "held"], name
            assert watchdog["paused"] == watchdog["stopped"] == [], name  # watch mode only says
        finally:
            daemon.stop()
    # Where the host cannot read the machine's memory, there is no level, and status says why.
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"))
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad(), platform=Posix)
    agent.watch_memory()
    watchdog = agent.status()["watchdog"]
    assert (watchdog["level"], watchdog["would"]) == (None, None)
    assert watchdog["reason"].startswith("the watchdog cannot see memory here")


def test_the_watchdog_pauses_stops_and_lets_run_and_says_so(sock_dir, tmp_path, monkeypatch):
    # Acting: at critical it pauses the newest tree, and stops it once it has been paused, and
    # the level critical, for 5 s. The next is paused only while memory falls again. Once memory
    # has been fine for a while the oldest paused runs again, and new work waits until it has
    # been flat for 30 s. A stop that frees nothing means the shortage is elsewhere: nothing
    # more is paused or stopped. Each step: (available GiB, trees alive, what the host was told
    # to do, what is paused, why new work waits).
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    paused_now, settling = "is paused until memory frees", "new work waits until it settles"
    steps = [
        (6, [101, 102], [], [], None),
        (1.5, [101, 102], [("pause", 102)], [102], paused_now),
        (1.5, [101, 102], [], [102], paused_now),
        (1.5, [101, 102], [("kill", 102)], [], "memory is short"),
        (2.5, [101], [], [], None),  # the stop freed memory
        (1.5, [101], [("pause", 101)], [101], paused_now),  # falling again
        *[(10, [101], [], [101], paused_now)] * 4,
        (10, [101], [("resume", 101)], [101], settling),
        *[(10, [101], [], [101], settling)] * 11,
        (10, [101], [], [], None),  # flat for 30 s
        (1.5, [101, 103], [("pause", 103)], [103], paused_now),
        (1.5, [101, 103], [], [103], paused_now),
        (1.5, [101, 103], [("kill", 103)], [], "memory is short"),
        (1.5, [101], [], [], "memory is short"),  # not falling: nothing more is paused
        # 101 ended, and stopping 104 would free little: its workers share their pages.
        (1.5, [104], [], [], "outside leasehold's sessions"),
        (1.5, [104], [], [], "outside leasehold's sessions"),
    ]  # fmt: skip
    fake, mono = FakeHost(), Clock(0.0)
    fake.procs[100] = host.Proc(100, "100", "fake-agent", parent=1, started=1000.0, anon=GIB)
    config = machine_config(
        tmp_path, socket=str(sock_dir / "s.sock"), watchdog={"mode": "on", "agents": ["x"]}
    )
    agent = MachineAgent(
        config, facts=FACTS, load=FakeLoad(), monotonic=mono, platform=lambda: fake
    )
    daemon = Daemon(config, agent)
    daemon.start()
    try:
        client = Client(daemon.path)
        client.register_session("cc-mine", pid=100, agent="fake-agent", source="hook")
        # As `hold` and the shims ask: the rule is also a label.
        pytest = {"rule": "pytest", "labels": {"rule": "pytest"}}
        lease = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=0, **pytest)
        lid = lease["lease"]["lease_id"]
        trees = {
            101: ("make -j8", 60, None),
            102: ("python3 -m pytest -n 4", 120, lid),
            103: ("cargo build", 130, None),
            104: ("python3 -m pytest -n 8", 140, None),
        }
        fake.frees[104] = 100 << 20
        for n, (free, alive, told, paused, why) in enumerate(steps):
            for pid in set(fake.procs) - {100} - set(alive):
                del fake.procs[pid]
            for pid in alive:
                command, after, under = trees[pid]
                fake.procs.setdefault(pid, host.Proc(
                    pid, str(pid), command.split()[0], parent=100, started=1000.0 + after,
                    anon=GIB, lease=under,
                ))  # fmt: skip
                fake.commands[pid] = command
            fake.available = int(free * GIB)
            fake.done.clear()
            mono.now += 2.5
            agent.tick()
            agent.watch_memory()
            watchdog = client.status()["watchdog"]
            assert fake.done == told, n
            assert [p["pid"] for p in watchdog["paused"]] == paused, n
            assert (watchdog["held"] is None) == (why is None), n
            assert why is None or why in watchdog["held"], n
            if n == 1:
                assert client.renew(lid)["paused"] is True
                # The session's next hook call passes the notice on to the agent, once.
                reply = claude_code.Reply()
                claude_code.tell({"session_id": "mine"}, os.environ, reply, "pre")
                told = reply.output["hookSpecificOutput"]["additionalContext"]
                assert "has been paused" in told and "do not start it again" in told
                again = claude_code.Reply()
                claude_code.tell({"session_id": "mine"}, os.environ, again, "pre")
                assert again.output is None
                waiting = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=0)
                assert waiting["status"] == "queued"
                blocked = waiting["blocked"]
                assert blocked.startswith("the watchdog holds new work"), blocked
                client.release(waiting["lease_id"])
            if n == 3:
                stopped = client.renew(lid)["stopped"]
                assert (stopped["why"], stopped["runaway"], stopped["rerun"], stopped["test"]) == (
                    "critical",
                    False,
                    True,
                    True,
                )
                (record,) = watchdog["stopped"]
                assert (record["pid"], record["stopped_for"]) == (102, "critical")
        events = [(e["type"], e.get("pid")) for e in client.events()["events"]]
        assert [e for e in events if e[0] in ("paused", "resumed", "stopped")] == [
            ("paused", 102), ("stopped", 102), ("paused", 101), ("resumed", 101),
            ("paused", 103), ("stopped", 103),
        ]  # fmt: skip
        reply = claude_code.Reply()
        claude_code.tell({"session_id": "mine"}, os.environ, reply, "post")
        assert "outside leasehold's sessions" in reply.output["systemMessage"]
        # Once memory is fine, the rule's next command runs alone: it waits while another lease
        # is held.
        fake.available = 10 * GIB
        mono.now += 2.5
        agent.watch_memory()
        again = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=0, **pytest)
        assert again["status"] == "queued"
        assert again["blocked"].startswith("it runs alone"), again["blocked"]
        client.release(lid)
        agent.tick()
        assert again["lease_id"] in held(daemon.path)
    finally:
        daemon.stop()
    # A real tree, as an agent's Bash tool runs one: `hold` under `sh -c`, its command in a scope
    # where commands get one, else not. The watchdog pauses and stops the command, never `hold`
    # or the shells it runs under, so `hold` lives to report the stop.
    monkeypatch.setattr(watchdog_module, "AGENT_START_S", 0.0)
    monkeypatch.setattr(watchdog_module, "KILL_AFTER_S", 1.0)
    for contain in ["off"] if NO_SCOPE else ["off", "auto"]:
        _stop_a_real_hold(sock_dir, tmp_path / f"real-{contain}", contain)
    # A program that runs `hold` (a test run calling a shimmed tool) holds memory of its own: it
    # is stopped with the command, and only `hold` is spared.
    _stop_a_real_hold(sock_dir, tmp_path / "real-wrapped", "off", wrapped=True)


class _Short(Linux):
    """This machine, with every task read as stalled on memory once `low` is set (with swap
    room, so that available memory does not count, and any tree would help)."""

    low = False

    def pressure(self) -> host.Pressure:
        return host.Pressure(
            "exact", available=8 * GIB, total=16 * GIB, swap_total=8 * GIB, swap_used=0,
            stall_full_recent=50.0 if self.low else 0.0,
        )  # fmt: skip


def _stop_a_real_hold(sock_dir, where, contain: str, wrapped: bool = False) -> None:
    real = _Short()
    where.mkdir()
    config = machine_config(
        where,
        socket=str(sock_dir / "r.sock"),
        watchdog={"mode": "on", "agents": [], "min_size": "1k"},
    )
    daemon = Daemon(
        config, MachineAgent(config, facts=FACTS, load=FakeLoad(), platform=lambda: real)
    )
    daemon.start()
    hold = f"{sys.executable} -m leasehold --socket {daemon.path} hold -- sleep 300"
    if wrapped:
        wrapper = where / "wrapper.py"
        wrapper.write_text(
            "import pathlib, subprocess, sys\n"
            "code = subprocess.call(sys.argv[2:])\n"
            "pathlib.Path(sys.argv[1]).write_text(str(code))\n"
        )
        hold = f"{sys.executable} {wrapper} {where}/wrapper-ended {hold}"
    tool = f"sh -c '{hold} 2>{where}/said; echo $? >{where}/code'; sleep 60"
    env = {**CHILD_ENV} | ({"LEASEHOLD_CONTAIN": "off"} if contain == "off" else {})
    shell = subprocess.Popen(["sh", "-c", tool], env=env, start_new_session=True)
    code = where / "code"
    try:
        client = Client(daemon.path)
        client.register_session("real", pid=shell.pid, agent="sh")
        until(
            lambda: any(
                (s["processes"] or 0) >= 3 for s in client.status()["watchdog"]["sessions"]
            ),
            timeout=20,
        )
        real.low = True
        said = where / "said"
        until(lambda: "leasehold's watchdog" in said.read_text(), timeout=30)  # `hold` reports
        if wrapped:  # stopped with the shell it ran under, not spared
            sessions = client.status
            until(lambda: sessions()["watchdog"]["sessions"][0]["processes"] == 1)  # `sleep 60`
            assert not (where / "wrapper-ended").exists()
        else:  # the shell `hold` ran under was spared
            until(code.exists)
            assert code.read_text().strip() == "137", contain
        assert shell.poll() is None, contain  # the agent's shell runs on
        (stopped,) = client.status()["watchdog"]["stopped"]
        assert stopped["stopped_for"] == "critical"
    finally:
        os.killpg(shell.pid, signal.SIGKILL)
        daemon.stop()
