"""The memory guard's contract: what pressure turns it on, as `status` reports it."""

from __future__ import annotations

from support import FACTS, FakeLoad, machine_config, until

from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.server import Daemon


def test_pressure_inside_the_memory_limit_turns_the_guard_on(tmp_path, sock_dir):
    load = FakeLoad()  # the machine's own pressure stays at 0
    load.memory_limit = {"cgroup": "/", "limit_bytes": 7 << 30, "used_bytes": 1 << 30}
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"))
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=load))
    daemon.start()
    try:
        client = Client(daemon.path)
        guard = client.status()["guard"]
        assert (guard["memory_pressure_on"], guard["memory_pressure_off"]) == (10.0, 2.5)
        assert "." in guard["pressure_cgroups"]
        assert not guard["on"]
        # The peak the memtight bench measured inside its cap, which the machine never showed.
        load.by_cgroup["."] = {"memory": 13.0}
        until(lambda: client.status()["guard"]["on"])
        assert client.status()["guard"]["reason"] == "memory pressure 13% over 10%"
    finally:
        daemon.stop()
