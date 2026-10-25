"""The memory net (`[memory_net] mode = "park"`) on a fake cgroup tree: which lease it parks and
when, what admission holds back while a lease is parked or settling, what the holder hears,
what status and the outcome record, what a daemon that stops and one that starts do with a
parked command, and the config and doctor answers around it."""

from __future__ import annotations

import functools
import json
import os
import re
import time

import pytest
from support import FACTS, Clock, FakeLoad, machine_config, until

from leasehold import contain, doctor, machine, net
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.machine import MemoryNetSettings
from leasehold.server import Daemon

MIB = 1 << 20
GIB = 1 << 30


class Tree:
    """A cgroup tree as the net reads it: this user's `leasehold.slice`, which carries the
    memory limit (the cap), and a lease scope in it for each command."""

    def __init__(self, root, *, limit: int = 4 * GIB):
        self.root = root / "cgroup"
        self.slice = self.root / contain.root_slice_cgroup()
        self.slice.mkdir(parents=True)
        (self.root / "cgroup.controllers").write_text("cpu memory\n")
        (self.slice / "memory.reclaim").write_text("")
        self.proc = root / "proc"
        (self.proc / "pressure").mkdir(parents=True)
        (self.proc / "meminfo").write_text(
            "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\nSwapTotal: 8388608 kB\n"
        )
        self.stall = 0
        self.cap(used=limit // 2)
        (self.slice / "memory.max").write_text(f"{limit}\n")

    def cap(self, *, used: int, stalled_s: float = 0.0) -> None:
        self.stall += int(stalled_s * 1e6)
        (self.slice / "memory.current").write_text(f"{used}\n")
        (self.slice / "memory.pressure").write_text(
            f"some avg10=0.00 avg60=0.00 avg300=0.00 total={self.stall}\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        )

    def scope(self, lease_id: str, *, used: int = GIB, swap: int = 0):
        """Make the scope of `lease_id`, or set what it uses now."""
        path = self.slice / contain.lease_unit(lease_id)
        if not path.exists():
            path.mkdir()
            for name, value in [("cgroup.freeze", 0), ("memory.swap.max", "max")]:
                (path / name).write_text(f"{value}\n")
            (path / "memory.reclaim").write_text("")
        (path / "memory.current").write_text(f"{used}\n")
        (path / "memory.swap.current").write_text(f"{swap}\n")

    def read(self, lease_id: str, name: str) -> str:
        return (self.slice / contain.lease_unit(lease_id) / name).read_text().strip()


def start(tmp_path, sock_dir, tree: Tree, clock: Clock, mode: str = "park") -> Daemon:
    """A started daemon over `tree`, with the net in `mode`, its ledger in `tmp_path`."""
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "s.sock"),
        ceiling={"slots": 4},
        pools={"work": {"reserved": 2, "max": 4}},
        kinds={"work": "work"},
        lend={"pools": []},
        memory={"budget": "8g"},
        guard={"enabled": False},
        memory_net={"mode": mode},
    )
    agent = MachineAgent(
        config,
        facts=FACTS,
        load=FakeLoad(),
        clock=clock,
        net=functools.partial(net.MemoryNet, cgroup_root=tree.root, proc=tree.proc),
    )
    daemon = Daemon(config, agent)
    daemon.start()
    return daemon


@pytest.fixture
def parking(tmp_path, sock_dir):
    """A daemon with the net on over `Tree`, its clock moved by hand: (client, tree, clock)."""
    tree, clock = Tree(tmp_path), Clock()
    daemon = start(tmp_path, sock_dir, tree, clock)
    try:
        yield Client(daemon.path), tree, clock
    finally:
        daemon.stop()


def granted(client: Client, clock: Clock) -> str:
    clock.now += 1  # granted later than the one before: younger
    answer = client.acquire(kind="work", weight=1, ttl_s=3600, wait_s=0)
    assert answer["status"] == "granted"
    return answer["lease"]["lease_id"]


def net_status(client: Client) -> dict:
    return client.status()["memory_net"]


def park_younger(client: Client, tree: Tree, clock: Clock) -> tuple[str, str]:
    """Two heavy leases, the younger spilling fast while the cap stalls, until it is parked:
    (older, younger)."""
    older = granted(client, clock)
    tree.scope(older)
    until(lambda: net_status(client)["watched"] == 1)
    younger = granted(client, clock)
    tree.scope(younger)
    until(lambda: net_status(client)["watched"] == 2)
    swap = 0

    def spilling() -> bool:
        nonlocal swap
        swap += 64 * MIB
        clock.now += 0.3
        tree.cap(used=4 * GIB, stalled_s=0.1)
        tree.scope(younger, swap=swap)
        return bool(net_status(client)["parked"])

    until(spilling)
    return older, younger


def test_the_youngest_heavy_lease_is_parked_and_new_work_waits_until_it_settles(parking):
    client, tree, clock = parking
    older = granted(client, clock)
    tree.scope(older)
    until(lambda: net_status(client)["watched"] == 1)
    assert tree.read(older, "memory.swap.max") == "max"  # alone: nothing would be parked
    client.release(older)
    (tree.slice / contain.lease_unit(older)).rename(tree.slice / "gone-0")

    older, younger = park_younger(client, tree, clock)
    status = net_status(client)
    assert [p["lease_id"] for p in status["parked"]] == [younger]
    assert status["parks"] == 1
    assert tree.read(younger, "cgroup.freeze") == "1"
    assert tree.read(younger, "memory.reclaim") == str(GIB)
    assert tree.read(older, "cgroup.freeze") == "0"
    assert tree.read(older, "memory.swap.max") == "max"  # it runs alone now
    assert (client.renew(younger)["parked"], client.renew(older)["parked"]) == (True, False)

    # New work that counts memory waits behind the parked lease.
    waiting = client.acquire(kind="work", weight=1, ttl_s=3600, wait_s=0)
    assert waiting["status"] == "queued"
    why = f"the memory net holds new work: lease {younger}"
    assert waiting["blocked"] == f"{why} is parked until memory frees"

    # Long after, memory has not freed and the older one runs: the younger stays parked.
    clock.now += 300
    time.sleep(4 * net.TICK_S)
    assert [p["lease_id"] for p in net_status(client)["parked"]] == [younger]
    assert tree.read(younger, "cgroup.freeze") == "1"

    # The older command ends: the parked one runs again, and settles before work starts.
    client.release(older)
    (tree.slice / contain.lease_unit(older)).rename(tree.slice / "gone-1")
    until(lambda: net_status(client)["settling"])
    assert tree.read(younger, "cgroup.freeze") == "0"
    assert client.renew(younger)["parked"] is False
    queue = client.leases()["queue"]
    assert [q["blocked"] for q in queue] == [f"{why} is resuming after a park"]

    def calm() -> bool:
        clock.now += 0.5
        return not net_status(client)["settling"]

    until(calm)
    again = client.acquire(
        kind="work", weight=1, ttl_s=3600, wait_s=5, lease_id=waiting["lease_id"]
    )
    assert again["status"] == "granted"
    client.release(younger, outcome={"wall_s": 12.0})
    rows = client.outcomes()["outcomes"]
    assert [(r["parks"], r["parked_s"] >= 300) for r in rows] == [(1, True)]


def test_a_parked_command_stays_paused_across_a_restart_and_waits_its_turn(tmp_path, sock_dir):
    tree, clock = Tree(tmp_path), Clock()
    daemon = start(tmp_path, sock_dir, tree, clock)
    try:
        older, younger = park_younger(Client(daemon.path), tree, clock)
    finally:
        daemon.stop()
    # The stopped daemon left the parked command frozen and lifted the swap limits it set.
    assert (tree.read(younger, "cgroup.freeze"), tree.read(older, "memory.swap.max")) == (
        "1",
        "max",
    )

    daemon = start(tmp_path, sock_dir, tree, clock)
    try:
        client = Client(daemon.path)
        until(lambda: net_status(client)["parked"])
        assert [p["lease_id"] for p in net_status(client)["parked"]] == [younger]
        waiting = client.acquire(kind="work", weight=1, ttl_s=3600, wait_s=0)
        assert waiting["blocked"] == (
            f"the memory net holds new work: lease {younger} is parked until memory frees"
        )
    finally:
        daemon.stop()
    assert tree.read(younger, "cgroup.freeze") == "1"

    # With the net turned off, a daemon lets go of what the net before it paused.
    daemon = start(tmp_path, sock_dir, tree, clock, mode="off")
    daemon.stop()
    assert tree.read(younger, "cgroup.freeze") == "0"


def stepped(tmp_path, leases: str = "ab") -> tuple[Tree, list[float], net.MemoryNet]:
    """A net over a 6 GiB cap with a heavy lease for each letter of `leases`, each younger than
    the one before, stepped by hand: (tree, [now], net)."""
    tree, now = Tree(tmp_path, limit=6 * GIB), [0.0]
    memory_net = net.MemoryNet(
        MemoryNetSettings(mode="park"),
        leases=lambda: [(lease, float(i)) for i, lease in enumerate(leases)],
        cgroup_root=tree.root,
        proc=tree.proc,
        clock=lambda: now[0],
        monotonic=lambda: now[0],
    )
    for lease in leases:
        tree.scope(lease, used=3 * GIB)
    memory_net.step()
    return tree, now, memory_net


@pytest.mark.parametrize(
    ("cap_used", "spill_per_s", "parked"),
    [
        (6 * GIB, 7 * MIB, ["b"]),  # slow, until b's overflow fills
        (6 * GIB, 400 * MIB, ["b"]),  # fast
        (3 * GIB, 400 * MIB, []),  # b over its own limit while the cap has room
    ],
)
def test_what_spill_parks_the_younger_lease(tmp_path, cap_used, spill_per_s, parked):
    tree, now, memory_net = stepped(tmp_path)
    swap = 0
    for _ in range(int(160 / 0.25)):
        now[0] += 0.25
        if memory_net.state("b") != "parked":
            swap = min(swap + int(spill_per_s * 0.25), GIB)
            tree.scope("b", used=3 * GIB, swap=swap)
        tree.cap(used=cap_used, stalled_s=0.125)
        memory_net.step()
    assert [p["lease_id"] for p in memory_net.status()["parked"]] == parked


def test_one_spill_parks_one_lease(tmp_path):
    tree, now, memory_net = stepped(tmp_path, "abc")
    swap = 0
    while not memory_net.status()["parked"]:
        now[0] += 0.05
        swap += 10 * MIB
        for lease in "abc":
            tree.scope(lease, used=2 * GIB, swap=swap)
        tree.cap(used=6 * GIB, stalled_s=0.025)
        memory_net.step()
    for _ in range(4):  # the spill and the stall have stopped, the cap still full
        now[0] += 0.05
        tree.cap(used=6 * GIB)
        memory_net.step()
    assert memory_net.status()["parks"] == 1


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"contain": {"mode": "off"}}, '[contain] mode is "off"'),
        ({"mode": "system"}, "runs only in a user's own daemon"),
        ({"memory_net": {"mode": "park", "overflow": "0g"}}, "an overflow of 0"),
    ],
)
def test_a_net_that_could_not_work_is_a_config_error(tmp_path, overrides, error):
    raw = {"ceiling": {"slots": 2}, "memory_net": {"mode": "park"}} | overrides
    with pytest.raises(ValueError, match=re.escape(error)):
        machine.MachineConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("swaps", "kernel", "says"),
    [
        ("", "6.12.0", "there is no swap"),
        ("/swapfile file 4194300 0 -2\n", "5.15.0-91-generic", "Linux 5.19 added"),
    ],
)
def test_doctor_says_when_the_net_could_park_nothing(tmp_path, swaps, kernel, says):
    config = tmp_path / "machine.toml"
    config.write_text('[ceiling]\nslots = 2\n\n[memory_net]\nmode = "park"\n')
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel").mkdir(parents=True)
    (proc / "swaps").write_text(f"Filename\tType\tSize\tUsed\tPriority\n{swaps}")
    (proc / "meminfo").write_text("MemTotal: 16777216 kB\n")
    (proc / "sys" / "kernel" / "osrelease").write_text(f"{kernel}\n")
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "HOME": str(tmp_path)} | {
        k: v for k, v in os.environ.items() if k == "PATH"
    }
    checks = doctor.run(socket=None, config=str(config), env=env, roots=doctor.Roots(proc=proc))
    found = [c.to_dict() for c in checks if c.name == "memory_net"]
    assert [(c["level"], says in c["detail"]) for c in found] == [("warn", True)]
    assert json.dumps(found)
