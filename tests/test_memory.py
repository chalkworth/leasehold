"""Memory-aware admission in the machine agent and over a real socket: a config with `[memory]`
counts what each request declares (or its weight at the assumed size per slot) against a
budget of the machine's memory less what is kept; the guard then limits new work to the memory
measured free instead of capping pools to their reservations; status and `top` show the
budget; a config without `[memory]` counts nothing, as before."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from support import CHILD_ENV, FACTS, FakeLoad, machine_config, make_agent

from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.cli import main
from leasehold.client import ApiError, Client
from leasehold.machine import MemorySettings
from leasehold.server import Daemon

G = 2**30
#: 4 slots, `work` reserving 2; FACTS has 16 GiB, so the default budget is 12 GiB and an
#: undeclared request counts 3 GiB a slot.
FOUR = {
    "ceiling": {"slots": 4},
    "pools": {
        "work": {"reserved": 2, "max": 4, "priority": 0},
        "background": {"reserved": 0, "max": 2, "priority": 1},
    },
    "kinds": {"work": "work", "external": "background"},
    "lend": {"pools": [], "quiet_s": 0},
}


def ask(agent, lease_id, weight=1, memory=None, user="ann"):
    req = AcquireRequest(kind="work", lease_id=lease_id, weight=weight, memory=memory)
    return agent.acquire(req, user=user)


def test_the_budget_is_the_memory_less_what_is_kept():
    assert MemorySettings().resolve(memory_bytes=16 * G, slots=4).budget == 12 * G
    assert MemorySettings(kept="4g").resolve(memory_bytes=16 * G, slots=4).budget == 12 * G
    fixed = MemorySettings(budget="10g").resolve(memory_bytes=16 * G, slots=5)
    assert (fixed.budget, fixed.kept, fixed.assumed_per_slot) == (10 * G, 0, 2 * G)
    assert (
        MemorySettings(assumed_per_slot="0g").resolve(memory_bytes=G, slots=1).assumed_per_slot == 0
    )
    with pytest.raises(ValueError):
        MemorySettings(kept="120%")


def test_declared_and_assumed_memory_count_against_the_budget(tmp_path):
    agent, _, _ = make_agent(tmp_path, memory={}, **FOUR)
    assert ask(agent, "big", memory="10g")["status"] == "granted"
    queued = ask(agent, "small", weight=1)
    assert queued["status"] == "queued"
    assert queued["blocked"] == (
        "the memory budget is used: 10.0 GiB of 12.0 GiB counted; this counts 3.0 GiB "
        "(weight 1 at 3.0 GiB a slot, none declared)"
    )
    st = agent.status()
    assert st["memory"] == {
        "counted": True,
        "machine_bytes": 16 * G,
        "budget_bytes": 12 * G,
        "kept_bytes": 4 * G,
        "assumed_per_slot_bytes": 3 * G,
        "held_bytes": 10 * G,
        "free_now_bytes": None,
    }
    [lease] = st["leases"]
    assert lease["memory_bytes"] == lease["memory_counted_bytes"] == 10 * G
    [waiting] = st["queue"]
    assert waiting["memory_bytes"] is None and waiting["memory_counted_bytes"] == 3 * G
    assert st["pools"]["work"]["memory_bytes"] == 10 * G
    agent.release("big", user="ann")
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["small"]


def test_more_than_the_budget_or_the_ceiling_is_refused(tmp_path):
    agent, _, _ = make_agent(tmp_path, memory={}, **FOUR)
    over_budget = ask(agent, "a", memory="13g")
    assert over_budget["status"] == "refused"
    assert over_budget["reason"] == "its declared 13.0 GiB is more than the 12.0 GiB memory budget"
    over_ceiling = ask(agent, "b", memory="20g")
    assert over_ceiling["status"] == "refused"
    assert (
        "more than the" in over_ceiling["reason"] and "[ceiling] memory" in over_ceiling["reason"]
    )


def test_without_a_memory_table_nothing_is_counted(tmp_path):
    agent, _, _ = make_agent(tmp_path, **FOUR)
    assert ask(agent, "big", memory="20g")["status"] == "granted"  # past the ceiling: as 0.2
    agent.release("big", user="ann")
    assert ask(agent, "a", memory="10g")["status"] == "granted"
    assert ask(agent, "b", memory="10g")["status"] == "granted"
    st = agent.status()
    assert st["memory"]["counted"] is False and st["memory"]["budget_bytes"] is None
    assert st["leases"][0]["memory_bytes"] == 10 * G
    assert st["leases"][0]["memory_counted_bytes"] == 0


def test_the_guard_limits_new_work_to_memory_free_not_to_reserved_slots(tmp_path):
    """Under pressure nothing that counts memory starts; once pressure eases, a request heavier
    than its pool's reservation starts while the guard is still settling, if what it counts
    fits what is free above the floor less what was just granted."""
    agent, clock, load = make_agent(tmp_path, memory={}, guard={"settle_s": 30}, **FOUR)
    load.set(memory=50.0)
    agent.tick()
    held_back = ask(agent, "heavy", weight=3)  # 3 > work's 2 reserved; counts 9 GiB
    assert held_back["status"] == "queued"
    assert held_back["blocked"] == (
        "memory is short (memory guard on: memory pressure 50% over 20%): 0.0 GiB may start "
        "now; this counts 9.0 GiB (weight 3 at 3.0 GiB a slot, none declared)"
    )
    assert agent.status()["guard"]["pressing"] is True
    load.set(memory=0.0)  # pressure gone, but only 8 GiB available: 7.2 GiB above the floor
    clock.now += 2
    agent.tick()
    st = agent.status()
    assert st["guard"]["on"] and not st["guard"]["pressing"]
    assert st["memory"]["free_now_bytes"] == 8 * G - (16 * G // 20)
    [waiting] = st["queue"]
    assert waiting["blocked"].startswith("memory is short (memory guard settling after memory")
    load.available = 14 * G
    clock.now += 2
    agent.tick()
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["heavy"]
    assert agent.guard.on  # still settling: heavy started before the guard went off
    # What was just granted counts against what is free until it has had settle_s to grow.
    assert agent.memory_free() == 14 * G - 16 * G // 20 - 9 * G


def test_a_config_without_memory_keeps_the_old_guard(tmp_path):
    agent, _, load = make_agent(tmp_path, guard={"settle_s": 0}, **FOUR)
    load.set(memory=50.0)
    agent.tick()
    queued = ask(agent, "heavy", weight=3)
    assert queued["status"] == "queued"
    assert queued["blocked"].startswith("the machine is under pressure: pool work admits only its")


def test_hold_declares_memory_over_the_socket(tmp_path, sock_dir, capsys):
    sock = sock_dir / "m.sock"
    config = machine_config(tmp_path, socket=str(sock), memory={}, **FOUR)
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        code = (
            "import json, os, sys\n"
            "from leasehold.client import Client\n"
            "st = Client(sys.argv[1]).status()\n"
            "print(json.dumps(st['leases']))\n"
        )
        done = subprocess.run(
            [
                sys.executable, "-m", "leasehold", "--socket", str(sock), "hold", "--weight",
                "2", "--memory", "5g", "--", sys.executable, "-c", code, str(sock),
            ],
            env=CHILD_ENV | {"LEASEHOLD_CONTAIN": "off"},
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        assert done.returncode == 0, done.stderr
        [lease] = json.loads(done.stdout)
        assert lease["weight"] == 2 and lease["memory_bytes"] == 5 * G
        assert lease["memory_counted_bytes"] == 5 * G
        client = Client(sock)
        assert client.acquire(kind="work", lease_id="x", weight=1, memory="11g")["status"] == (
            "granted"
        )
        assert main(["--socket", str(sock), "status"]) == 0
        text = capsys.readouterr().out
        assert "memory 11.0/12.0 GiB counted (4.0 GiB kept for you; 3.0 GiB a slot" in text
        bad = None
        try:
            client.acquire(kind="work", lease_id="y", memory="lots")
        except ApiError as exc:
            bad = exc
        assert bad is not None and bad.status == 400  # never retried without its memory
    finally:
        daemon.stop()


def test_a_daemon_that_does_not_take_memory_is_asked_again_without_it(monkeypatch):
    """A 0.2 daemon refuses the unknown field; the client asks again without it."""
    sent = []

    def request(self, method, url, body=None, *, extra_timeout=0.0):
        sent.append(dict(body))
        if "memory" in body:
            raise ApiError(
                400,
                {
                    "error": "bad_request",
                    "detail": [{"type": "extra_forbidden", "loc": ["memory"], "msg": "x"}],
                },
            )
        return {"status": "granted", "lease": {}}

    monkeypatch.setattr(Client, "request", request)
    answer = Client("/nowhere").acquire(kind="work", weight=1, memory="4g", lease_id="a")
    assert answer["status"] == "granted"
    assert sent == [
        {"kind": "work", "weight": 1, "memory": "4g", "lease_id": "a"},
        {"kind": "work", "weight": 1, "lease_id": "a"},
    ]
