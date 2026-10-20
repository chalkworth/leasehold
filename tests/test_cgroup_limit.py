"""The cgroup memory limit (leasehold.cgroup, .load) over fixture trees, and what it does to a
user daemon: an `auto` budget worked out under the limit, the guard's available memory cut to
what the limit leaves, and over a real socket, a request that fits the machine but not the
limit waiting."""

from __future__ import annotations

from pathlib import Path

import pytest
from support import FACTS, FakeLoad, machine_config, until

from leasehold import cgroup
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.client import Client
from leasehold.load import LoadReader, available
from leasehold.machine import MemorySettings
from leasehold.server import Daemon

G = 2**30
#: The scope the daemon runs in, inside a slice: as a benchmark or a person starts it.
OWN = "/user.slice/user-1000.slice/user@1000.service/app.slice/bench.scope"
FOUR = {
    "ceiling": {"slots": 4},
    "pools": {"work": {"reserved": 2, "max": 4, "priority": 0}},
    "kinds": {"work": "work"},
    "lend": {"pools": [], "quiet_s": 0},
}


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def trees(tmp_path: Path, limits: dict[str, str], *, used: int = G, inactive: int = 0):
    """(proc, cgroup root): the process in `OWN`, `MemAvailable` 12 GiB of 16, no PSI, and
    each cgroup in `limits` given those files (`memory.max` and so on). Every cgroup on the
    path uses `used`, `inactive` of it inactive file cache."""
    proc = write(
        tmp_path / "proc",
        {
            "self/cgroup": f"0::{OWN}\n",
            "meminfo": "MemTotal: 16777216 kB\nMemAvailable: 12582912 kB\n",
            "loadavg": "0.10 0.10 0.10 1/100 1\n",
        },
    )
    root = write(tmp_path / "cgroup", {"cgroup.controllers": "cpu memory io\n"})
    group = Path(OWN.lstrip("/"))
    for part in (group, *group.parents):
        if str(part) != ".":
            write(
                root / part,
                {"memory.current": f"{used}\n", "memory.stat": f"inactive_file {inactive}\n"},
            )
    for name, files in limits.items():
        write(root / name.lstrip("/"), files)
    return proc, root


def test_own_cgroup_reads_proc(tmp_path):
    proc = write(tmp_path, {"self/cgroup": "12:memory:/v1/path\n0::/a.slice/b.scope\n"})
    assert cgroup.own_cgroup(proc) == "/a.slice/b.scope"
    v1 = write(tmp_path / "v1", {"self/cgroup": "12:memory:/v1/path\n1:name=systemd:/x\n"})
    assert cgroup.own_cgroup(v1) is None


def test_the_lowest_limit_on_the_path_wins_and_max_is_none(tmp_path):
    _, root = trees(
        tmp_path,
        {
            "/user.slice/user-1000.slice": {"memory.max": "max\n", "memory.high": f"{10 * G}\n"},
            OWN: {"memory.max": f"{7 * G}\n", "memory.high": "max\n"},
            "/user.slice": {"memory.max": "max\n"},
        },
    )
    assert cgroup.limit(root, OWN) == cgroup.MemoryLimit(7 * G, OWN)
    _, lower = trees(tmp_path / "b", {"/user.slice": {"memory.high": f"{6 * G}\n"}})
    assert cgroup.limit(lower, OWN) == cgroup.MemoryLimit(6 * G, "/user.slice")


def test_no_limit_v1_or_a_path_outside_the_root_is_none(tmp_path):
    _, root = trees(tmp_path, {OWN: {"memory.max": "max\n", "memory.high": "max\n"}})
    assert cgroup.limit(root, OWN) is None
    (root / "cgroup.controllers").unlink()
    write(root, {OWN.lstrip("/") + "/memory.max": f"{G}\n"})
    assert cgroup.limit(root, OWN) is None  # not cgroup v2
    assert cgroup.limit(tmp_path, "/../etc") is None


def test_a_containers_own_root_limit_counts(tmp_path):
    """Inside a cgroup namespace the process is at `/`, and the mount's root is the
    container's cgroup, which has a `memory.max`."""
    root = write(tmp_path, {"cgroup.controllers": "memory\n", "memory.max": f"{4 * G}\n"})
    assert cgroup.limit(root, "/") == cgroup.MemoryLimit(4 * G, "/")


def test_used_leaves_out_the_inactive_file_cache(tmp_path):
    _, root = trees(tmp_path, {}, used=5 * G, inactive=2 * G)
    assert cgroup.used(root, OWN) == 3 * G
    (root / OWN.lstrip("/") / "memory.stat").unlink()
    assert cgroup.used(root, OWN) == 5 * G
    assert cgroup.used(root, "/nowhere") is None


def test_the_load_reader_reports_the_limit_only_when_asked(tmp_path):
    proc, root = trees(tmp_path, {OWN: {"memory.max": f"{7 * G}\n"}}, used=6 * G, inactive=G)
    reading = LoadReader(proc=proc, cgroup_root=root, memory_limit=True).read()
    assert reading["memory_limit"] == {"cgroup": OWN, "limit_bytes": 7 * G, "used_bytes": 5 * G}
    assert reading["mem_available_bytes"] == 12 * G
    assert available(reading) == 2 * G  # the limit leaves less than MemAvailable
    plain = LoadReader(proc=proc, cgroup_root=root).read()
    assert plain["memory_limit"] is None and available(plain) == 12 * G


def test_available_is_the_smaller_of_the_two():
    bound = {"cgroup": "/", "limit_bytes": 10 * G, "used_bytes": G}
    assert available({"mem_available_bytes": 4 * G, "memory_limit": bound}) == 4 * G
    assert available({"mem_available_bytes": None, "memory_limit": bound}) == 9 * G
    over = bound | {"used_bytes": 11 * G}
    assert available({"mem_available_bytes": 4 * G, "memory_limit": over}) == 0
    unread = bound | {"used_bytes": None}
    assert available({"mem_available_bytes": 4 * G, "memory_limit": unread}) == 4 * G
    assert available(None) is None


@pytest.mark.parametrize(
    ("kept", "limit", "budget"),
    [
        ("25%", 7 * G, 7 * G * 3 // 4),  # the same share of the limit
        ("4g", 8 * G, 6 * G),  # 4 GiB is a quarter of the machine: a quarter of the limit
        ("25%", 20 * G, 12 * G),  # a limit over the machine's memory changes nothing
        ("25%", None, 12 * G),
    ],
)
def test_an_auto_budget_is_worked_out_under_the_limit(kept, limit, budget):
    got = MemorySettings(kept=kept).resolve(memory_bytes=16 * G, slots=6, limit_bytes=limit)
    assert got.budget == budget
    assert got.assumed_per_slot == budget // 6


def test_a_budget_given_as_a_size_is_left_alone():
    got = MemorySettings(budget="10g").resolve(memory_bytes=16 * G, slots=4, limit_bytes=7 * G)
    assert got.budget == 10 * G


def test_the_benchmarks_7_gib_scope_on_16_gib_with_6_slots(tmp_path):
    """The case that OOM-killed jobs: a user daemon in a 7 GiB scope on a 16 GiB machine, 6
    slots, `kept` 25%. The budget is 5.25 GiB, not 12, and a slot counts 0.875 GiB."""
    proc, root = trees(tmp_path, {OWN: {"memory.max": f"{7 * G}\n"}})
    six = FOUR | {"ceiling": {"slots": 6}, "pools": {"work": {"reserved": 3, "max": 6}}}
    config = machine_config(tmp_path, memory={}, **six)
    load = LoadReader(proc=proc, cgroup_root=root, memory_limit=True)
    agent = MachineAgent(config, facts=FACTS | {"cpus_logical": 8}, load=load)
    assert agent.budget is not None
    assert agent.budget.budget == 7 * G * 3 // 4
    assert agent.budget.assumed_per_slot == 7 * G * 3 // 4 // 6
    st = agent.status()["memory"]
    assert st["limit_bytes"] == 7 * G and st["limit_cgroup"] == OWN
    assert st["budget_bytes"] == 7 * G * 3 // 4


def test_the_guard_sees_what_the_limit_leaves(tmp_path):
    """MemAvailable shows 12 GiB, but the 7 GiB limit has 0.5 GiB left: under the guard's
    floor (5% of 16 GiB), so the guard goes on and nothing new starts."""
    proc, root = trees(tmp_path, {OWN: {"memory.max": f"{7 * G}\n"}}, used=7 * G - G // 2)
    config = machine_config(tmp_path, memory={}, **FOUR)
    load = LoadReader(proc=proc, cgroup_root=root, memory_limit=True)
    agent = MachineAgent(config, facts=FACTS, load=load)
    agent.tick()
    assert agent.guard.on and agent.guard.pressing
    assert "0.5 GiB of memory available" in agent.guard.reason
    asked = agent.acquire(AcquireRequest(kind="work", lease_id="a", weight=1), user="ann")
    assert asked["status"] == "queued"


def test_the_memory_free_while_settling_is_under_the_limit(tmp_path):
    agent_config = machine_config(tmp_path, memory={}, guard={"settle_s": 30}, **FOUR)
    load = FakeLoad()
    load.available = 12 * G
    load.memory_limit = {"cgroup": OWN, "limit_bytes": 8 * G, "used_bytes": 7 * G}
    agent = MachineAgent(agent_config, facts=FACTS, load=load)
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=0.0)
    agent.tick()  # settling: what is free is what the limit leaves, above the floor
    assert agent.memory_free() == G - 16 * G // 20


def test_a_system_daemon_ignores_its_own_cgroup(tmp_path):
    config = machine_config(tmp_path, mode="system", memory={}, **FOUR)
    agent = MachineAgent(config, facts=FACTS, load=None)
    assert agent.loader.memory_limit is False
    (tmp_path / "u").mkdir()
    user = MachineAgent(machine_config(tmp_path / "u", memory={}, **FOUR), facts=FACTS)
    assert user.loader.memory_limit is True


def test_over_a_socket_what_fits_the_machine_but_not_the_limit_waits(tmp_path, sock_dir):
    """An 8 GiB limit on a 16 GiB machine: the budget is 6 GiB, not 12. 5 GiB is granted; 3
    GiB more would fit the machine's budget but not the limit's, so it waits, and starts when
    the first is released."""
    proc, root = trees(tmp_path, {"/user.slice": {"memory.max": f"{8 * G}\n"}})
    sock = sock_dir / "limit.sock"
    config = machine_config(tmp_path, socket=str(sock), memory={}, **FOUR)
    load = LoadReader(proc=proc, cgroup_root=root, memory_limit=True)
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=load))
    daemon.start()
    try:
        client = Client(sock)
        st = client.status()["memory"]
        assert st["budget_bytes"] == 6 * G and st["limit_bytes"] == 8 * G
        assert st["limit_cgroup"] == "/user.slice"
        assert client.acquire(kind="work", lease_id="a", memory="5g")["status"] == "granted"
        waiting = client.acquire(kind="work", lease_id="b", memory="3g")
        assert waiting["status"] == "queued"
        assert waiting["blocked"].startswith("the memory budget is used: 5.0 GiB of 6.0 GiB")
        client.release("a")
        until(lambda: [x["lease_id"] for x in client.status()["leases"]] == ["b"])
    finally:
        daemon.stop()
