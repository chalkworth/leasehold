"""The memory net (`[memory_net] mode = "park"`) against a real daemon on a fake cgroup tree: which
lease it parks, what admission holds back while a lease is parked or settling, what the holder
hears, what status and the outcome record, and the config and doctor answers around it."""

from __future__ import annotations

import functools
import json
import os
import re

import pytest
from support import FACTS, Clock, FakeLoad, machine_config, until

from leasehold import contain, doctor, machine, net
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.server import Daemon

MIB = 1 << 20
GIB = 1 << 30


class Tree:
    """A cgroup tree as the net reads it: this user's `leasehold.slice`, which carries the
    memory limit (the cap), and a lease scope in it for each command."""

    def __init__(self, root):
        self.root = root / "cgroup"
        self.slice = self.root / contain.root_slice_cgroup()
        self.slice.mkdir(parents=True)
        (self.root / "cgroup.controllers").write_text("cpu memory\n")
        self.proc = root / "proc"
        (self.proc / "pressure").mkdir(parents=True)
        (self.proc / "meminfo").write_text(
            "MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\nSwapTotal: 8388608 kB\n"
        )
        self.stall = 0
        self.cap(used=2 * GIB)
        (self.slice / "memory.max").write_text(f"{4 * GIB}\n")

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


@pytest.fixture
def parking(tmp_path, sock_dir):
    """A daemon with the net on over `Tree`, its clock moved by hand: (client, tree, clock)."""
    tree = Tree(tmp_path)
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "s.sock"),
        ceiling={"slots": 4},
        pools={"work": {"reserved": 2, "max": 4}},
        kinds={"work": "work"},
        lend={"pools": []},
        memory={"budget": "8g"},
        guard={"enabled": False},
        memory_net={"mode": "park"},
    )
    clock = Clock()
    agent = MachineAgent(
        config,
        facts=FACTS,
        load=FakeLoad(),
        clock=clock,
        net=functools.partial(net.MemoryNet, cgroup_root=tree.root, proc=tree.proc),
    )
    daemon = Daemon(config, agent)
    daemon.start()
    try:
        yield Client(daemon.path), tree, clock
    finally:
        daemon.stop()


def granted(client: Client, clock: Clock) -> str:
    clock.now += 1  # granted later than the one before: younger
    answer = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=0)
    assert answer["status"] == "granted"
    return answer["lease"]["lease_id"]


def net_status(client: Client) -> dict:
    return client.status()["memory_net"]


def test_the_youngest_heavy_lease_is_parked_and_new_work_waits_until_it_settles(parking):
    client, tree, clock = parking
    older = granted(client, clock)
    tree.scope(older)
    younger = granted(client, clock)
    tree.scope(younger)
    until(lambda: net_status(client)["watched"] == 2)
    assert tree.read(younger, "memory.swap.max") == str(GIB)  # its overflow

    # The cap overflows: the younger command spills into swap while the cap stalls.
    swap = 0

    def spilling() -> bool:
        nonlocal swap
        swap += 64 * MIB
        clock.now += 0.3
        tree.cap(used=4 * GIB, stalled_s=0.1)
        tree.scope(younger, swap=swap)
        return bool(net_status(client)["parked"])

    until(spilling)
    status = net_status(client)
    assert [p["lease_id"] for p in status["parked"]] == [younger]
    assert status["parks"] == 1
    assert tree.read(younger, "cgroup.freeze") == "1"
    assert tree.read(younger, "memory.reclaim") == str(GIB)
    assert tree.read(older, "cgroup.freeze") == "0"
    assert (client.renew(younger)["parked"], client.renew(older)["parked"]) == (True, False)

    # New work that counts memory waits behind the parked lease.
    waiting = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=0)
    assert waiting["status"] == "queued"
    why = f"the memory net holds new work: lease {younger}"
    assert waiting["blocked"] == f"{why} is parked until memory frees"

    # The older command ends: the parked one runs again, and settles before work starts.
    clock.now += 10
    client.release(older)
    (tree.slice / contain.lease_unit(older)).rename(tree.slice / "gone")
    until(lambda: net_status(client)["settling"])
    assert tree.read(younger, "cgroup.freeze") == "0"
    assert tree.read(younger, "memory.swap.max") == str(GIB)
    assert client.renew(younger)["parked"] is False
    queue = client.leases()["queue"]
    assert [q["blocked"] for q in queue] == [f"{why} is resuming after a park"]

    def calm() -> bool:
        clock.now += 0.5
        return not net_status(client)["settling"]

    until(calm)
    again = client.acquire(kind="work", weight=1, ttl_s=60, wait_s=5, lease_id=waiting["lease_id"])
    assert again["status"] == "granted"
    client.release(younger, outcome={"wall_s": 12.0})
    rows = client.outcomes()["outcomes"]
    assert [(r["parks"], r["parked_s"] >= 10) for r in rows] == [(1, True)]


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


def test_doctor_says_when_there_is_no_swap_to_park_in(tmp_path):
    config = tmp_path / "machine.toml"
    config.write_text('[ceiling]\nslots = 2\n\n[memory_net]\nmode = "park"\n')
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "swaps").write_text("Filename\tType\tSize\tUsed\tPriority\n")
    (proc / "meminfo").write_text("MemTotal: 16777216 kB\n")
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "HOME": str(tmp_path)} | {
        k: v for k, v in os.environ.items() if k == "PATH"
    }
    checks = doctor.run(socket=None, config=str(config), env=env, roots=doctor.Roots(proc=proc))
    found = [c.to_dict() for c in checks if c.name == "memory_net"]
    assert [(c["level"], "there is no swap" in c["detail"]) for c in found] == [("warn", True)]
    assert json.dumps(found)
