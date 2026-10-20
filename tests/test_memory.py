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
from support import CHILD_ENV, FACTS, Clock, FakeLoad, machine_config, make_agent

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
        "limit_bytes": None,
        "limit_cgroup": None,
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
    assert st["pools"]["work"]["memory_counted_bytes"] == 10 * G
    agent.release("big", user="ann")
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["small"]


def test_more_than_the_budget_or_the_ceiling_is_refused(tmp_path):
    agent, _, _ = make_agent(tmp_path, memory={}, **FOUR)
    over_budget = ask(agent, "a", memory="13g")
    assert over_budget["status"] == "refused"
    assert over_budget["reason"] == "its declared 13.0 GiB is more than the 12.0 GiB memory budget"
    over_ceiling = ask(agent, "b", memory="20g")
    assert over_ceiling["status"] == "refused"
    assert over_ceiling["reason"] == (
        "memory 20g is more than the 16.0 GiB one lease may be given (the machine's memory)"
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
        "memory is short (memory guard on: memory pressure 50% over 20%): nothing new starts "
        "until the pressure eases"
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


@pytest.mark.parametrize(("memory", "declared"), [({}, "1b"), ({"assumed_per_slot": "0g"}, None)])
def test_under_pressure_nothing_starts_even_what_counts_no_memory(tmp_path, memory, declared):
    agent, _, load = make_agent(tmp_path, memory=memory, **FOUR)
    load.set(memory=50.0)
    agent.tick()
    answer = ask(agent, "heavy", weight=4, memory=declared)
    assert answer["status"] == "queued"
    assert "nothing new starts until the pressure eases" in answer["blocked"]


def test_the_settling_is_measured_on_a_clock_that_never_steps(tmp_path):
    """A wall-clock jump neither ends the guard's settling nor ages a grant out of the settle
    window: both are measured on the monotonic clock."""
    wall, mono, load = Clock(), Clock(50.0), FakeLoad()
    config = machine_config(tmp_path, memory={}, guard={"settle_s": 30}, **FOUR)
    agent = MachineAgent(config, facts=FACTS, load=load, clock=wall, monotonic=mono)
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=0.0)
    load.available = 14 * G
    mono.now += 1
    agent.tick()
    assert ask(agent, "a", weight=1)["status"] == "granted"
    wall.now -= 3600  # the wall clock steps back an hour
    agent.tick()
    assert agent.guard.on
    assert agent.memory_free() == 14 * G - 16 * G // 20 - 3 * G  # "a" is still recent
    mono.now += 31
    wall.now += 31
    agent.tick()
    assert not agent.guard.on  # settled after 30 s, though the wall clock is an hour behind
    assert agent.memory_free() == 14 * G - 16 * G // 20  # "a" has had settle_s to grow


def test_rules_are_served_at_a_weight_whose_assumed_memory_fits_beside_standing_memory(
    tmp_path,
):
    """Standing memory (7 of 12 GiB) leaves room for weight 1 at 3 GiB a slot: the built-in
    rules are served at that weight, so what a shim asks for is granted, not refused."""
    standing = [{"name": "db", "pool": "background", "weight": 1, "memory": "7g"}]
    agent, _, _ = make_agent(tmp_path, memory={}, standing=standing, **FOUR)
    pytest_rule = next(r for r in agent.commands()["rules"] if "pytest" in r["programs"])
    assert pytest_rule["weight"] == 1
    assert ask(agent, "a", weight=pytest_rule["weight"])["status"] == "granted"
    assert ask(agent, "b", weight=2)["status"] == "refused"


def test_a_budget_of_0_or_one_standing_memory_fills_is_not_a_config(tmp_path):
    with pytest.raises(ValueError, match="a budget of 0"):
        machine_config(tmp_path, memory={"budget": "0g"}, **FOUR)
    standing = [{"name": "db", "pool": "background", "weight": 1, "memory": "8g"}]
    with pytest.raises(ValueError, match="filled by"):
        machine_config(tmp_path, memory={"budget": "8g"}, standing=standing, **FOUR)


def test_doctor_checks_the_budget_worked_out_for_the_machine(tmp_path):
    from leasehold import doctor

    nothing = machine_config(tmp_path, memory={"kept": "20g"}, **FOUR)
    check = doctor.check_memory(nothing, 16 * G)
    assert check.level == "fail" and "leaves no budget" in check.detail
    full = machine_config(
        tmp_path, memory={}, standing=[{"name": "db", "pool": "background", "weight": 1,
        "memory": "12g"}], **FOUR,
    )  # fmt: skip
    check = doctor.check_memory(full, 16 * G)
    assert check.level == "fail" and "nothing that counts memory can start" in check.detail
    standing = [{"name": "db", "pool": "background", "weight": 1, "memory": "7g"}]
    cut = machine_config(tmp_path, memory={}, standing=standing, **FOUR)
    check = doctor.check_memory(cut, 16 * G)
    assert check.level == "warn" and "are cut to it" in check.detail
    zero = machine_config(tmp_path, memory={"assumed_per_slot": "0g"}, **FOUR)
    check = doctor.check_memory(zero, 16 * G)
    assert check.level == "warn" and "assumed_per_slot is 0" in check.detail
    assert doctor.check_memory(machine_config(tmp_path, memory={}, **FOUR), 16 * G) is None
    assert doctor.check_memory(machine_config(tmp_path, **FOUR), 16 * G) is None


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


@pytest.mark.parametrize("memory", ["99999999999999999999g", "9223372036854775808b", "2000000t"])
def test_a_size_too_large_for_any_machine_is_a_bad_request_without_a_memory_table(
    tmp_path, sock_dir, memory
):
    """Over a real socket, with no [memory] table: a size past `MEMORY_MAX` is answered 400
    `bad_request`, not a fault, and the daemon goes on answering."""
    sock = sock_dir / "big.sock"
    config = machine_config(tmp_path, socket=str(sock), **FOUR)
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        client = Client(str(sock))
        with pytest.raises(ApiError) as caught:
            client.acquire(kind="work", lease_id="x", memory=memory)
        assert caught.value.status == 400
        assert caught.value.payload["error"] == "bad_request"
        assert client.acquire(kind="work", lease_id="y", memory="1t")["status"] == "granted"
    finally:
        daemon.stop()


def test_a_ledger_item_with_memory_out_of_range_is_a_value_error():
    from leasehold.ledger import LedgerItem
    from leasehold.units import MEMORY_MAX

    item = {"scope": "s", "item_id": "i", "kind": "work", "user": None, "pool": "work"}
    assert LedgerItem(**item, weight=1, memory=MEMORY_MAX).memory == MEMORY_MAX
    for bad in (-1, MEMORY_MAX + 1, 2**63):
        with pytest.raises(ValueError, match="not between 0"):
            LedgerItem(**item, weight=1, memory=bad)


def _generated(tmp_path, **contain):
    """The config `init --user` writes for an 8-CPU, 16 GiB machine: 6 slots, work reserving
    3, the built-in heavy rules at weight 4, memory counted."""
    import tomllib

    from leasehold import template

    raw = tomllib.loads(template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 * G}))
    raw.pop("name")
    if contain:
        raw["contain"] = raw["contain"] | contain
    return machine_config(tmp_path, **raw)


def test_a_default_heavy_job_starts_under_the_guard_once_its_memory_fits(tmp_path):
    """On the generated 6-slot config a build or test run weighs 4, more than work's 3 reserved
    slots: under pressure it waits, and while the guard settles it starts as soon as the 8 GiB
    it counts (4 slots at 2 GiB) fits what is free, before the guard goes off."""
    from leasehold import doctor

    config = _generated(tmp_path)
    assert config.ceiling.slots == 6 and config.pool_set().pools["work"].reserved == 3
    facts = {"cpus_logical": 8, "memory_bytes": 16 * G}
    load, clock = FakeLoad(), Clock()
    agent = MachineAgent(config, facts=facts, load=load, clock=clock)
    pytest_rule = next(r for r in agent.commands()["rules"] if "pytest" in r["programs"])
    assert pytest_rule["weight"] == 4
    assert doctor.check_guard_weights(config, 16 * G) is None
    load.set(memory=50.0)
    agent.tick()
    answer = ask(agent, "build", weight=4)
    assert answer["status"] == "queued"
    assert "nothing new starts" in answer["blocked"]
    load.set(memory=0.0)
    load.available = 8 * G  # 7.2 GiB above the floor: not yet
    clock.now += 2
    agent.tick()
    [waiting] = agent.status()["queue"]
    assert "may start now; this counts 8.0 GiB" in waiting["blocked"]
    load.available = 10 * G
    clock.now += 2
    agent.tick()
    assert [x["lease_id"] for x in agent.leases()["leases"]] == ["build"]
    assert agent.guard.on


def test_doctor_says_the_kept_cpus_are_kept_only_from_well_behaved_tools(tmp_path):
    from leasehold import doctor

    check = doctor.check_hard_cpu(_generated(tmp_path))
    assert check is not None and check.level == "warn"
    assert "a tool that ignores it can still fill every CPU" in check.detail
    assert "hard_cpu = true" in check.fix
    assert doctor.check_hard_cpu(_generated(tmp_path, hard_cpu=True)) is None
    system = machine_config(tmp_path, mode="system")
    assert doctor.check_hard_cpu(system) is None


def test_the_generated_config_runs_one_heavy_job_and_offers_two_as_an_opt_in(tmp_path):
    from leasehold import template

    config = _generated(tmp_path)
    assert (config.commands.heavy_weight, config.commands.light_weight) == (4, 2)
    assert "heavy_weight" not in config.commands.model_fields_set
    text = template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 * G})
    assert "# heavy_weight = 3\n# light_weight = 2\n" in text
    assert "declare memory for memory-heavy work" in " ".join(text.split("\n# "))
    wide = template.sized("user", {"cpus_logical": 16, "memory_bytes": 16 * G})
    assert "# heavy_weight" not in wide  # 14 slots run three at weight 4 already


def _rules(agent) -> tuple[dict, dict]:
    """The served rules by first program: (grantable, never)."""
    served = agent.commands()["rules"]
    return (
        {r["programs"][0]: r for r in served if not r.get("never")},
        {r["programs"][0]: r for r in served if r.get("never")},
    )


STANDING_7G = [{"name": "db", "pool": "background", "weight": 1, "memory": "7g"}]


def test_commands_marks_a_rule_whose_declared_memory_cannot_fit_as_never(tmp_path):
    """A declared size above the budget, or above the room beside the standing memory, could
    never be granted: such a rule is served at its own weight with `never`, the reason (not
    cut to weight 1). One that fits keeps its weight."""
    rules = {
        "defaults": False,
        "rule": [
            {"programs": ["huge"], "weight": 2, "memory": "13g"},  # over the 12 GiB budget
            {"programs": ["big"], "weight": 2, "memory": "6g"},  # over 12 - 7 GiB of room
            {"programs": ["fits"], "weight": 2, "memory": "5g"},
        ],
    }
    config = machine_config(tmp_path, memory={}, standing=STANDING_7G, commands=rules, **FOUR)
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    served, never = _rules(agent)
    assert set(served) == {"fits"} and served["fits"]["weight"] == 2
    assert set(never) == {"huge", "big"} and never["big"]["weight"] == 2
    assert "13.0 GiB" in never["huge"]["never"]
    assert "7.0 GiB is held by standing work" in never["big"]["never"]
    assert ask(agent, "x", weight=2, memory="6g")["status"] == "refused"  # what it would get


def test_commands_marks_a_rule_whose_assumed_memory_has_no_room_as_never(tmp_path):
    """Undeclared, a rule counts its weight at 3 GiB a slot; beside 10 GiB of standing memory
    in a 12 GiB budget not even weight 1 fits, so it is never, not served at 1."""
    standing = [{"name": "db", "pool": "background", "weight": 1, "memory": "10g"}]
    rules = {"defaults": False, "rule": [{"programs": ["build"], "weight": 2}]}
    config = machine_config(tmp_path, memory={}, standing=standing, commands=rules, **FOUR)
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    served, never = _rules(agent)
    assert served == {} and set(never) == {"build"}
    assert ask(agent, "x", weight=1)["status"] == "refused"


def test_a_rule_that_cannot_fit_a_fixed_budget_fails_the_config(tmp_path):
    rules = {"defaults": False, "rule": [{"programs": ["big"], "weight": 2, "memory": "6g"}]}
    with pytest.raises(ValueError, match=r"commands.rule\[0\]: memory 6g cannot fit"):
        machine_config(
            tmp_path, memory={"budget": "12g"}, standing=STANDING_7G, commands=rules, **FOUR
        )


def test_doctor_fails_a_rule_that_cannot_fit_beside_the_standing_memory(tmp_path):
    from leasehold import doctor

    rules = {"rule": [{"programs": ["big"], "weight": 2, "memory": "6g"}]}
    config = machine_config(tmp_path, memory={}, standing=STANDING_7G, commands=rules, **FOUR)
    check = doctor.check_rules(config, 16 * G)
    assert check.level == "fail"
    assert "commands.rule[0] (big) is never granted" in check.detail
    assert "is refused" in check.detail


@pytest.mark.parametrize("size", ["0g", "0b", "0m"])
def test_a_declared_size_of_nothing_is_refused_everywhere(tmp_path, size):
    """A request, a rule and `hold --memory` may not declare 0; `assumed_per_slot` may."""
    with pytest.raises(ValueError, match="is 0"):
        AcquireRequest(kind="work", lease_id="z", weight=1, memory=size)
    rules = {"rule": [{"programs": ["x"], "weight": 1, "memory": size}]}
    with pytest.raises(ValueError, match="is 0"):
        machine_config(tmp_path, commands=rules, **FOUR)
    with pytest.raises(SystemExit) as exit_:
        main(["hold", "--memory", size, "--", "true"])
    assert exit_.value.code == 2
    machine_config(tmp_path, memory={"assumed_per_slot": "0g"}, **FOUR)  # allowed


def test_an_auto_budget_of_nothing_stops_the_daemon_starting(tmp_path):
    """`kept` at or over the machine's memory leaves an `auto` budget of 0: as with a budget of
    0 given as a size, the daemon does not start, rather than admitting nothing."""
    config = machine_config(tmp_path, memory={"kept": "16g"}, **FOUR)
    with pytest.raises(ValueError, match="leaves no budget"):
        MachineAgent(config, facts=FACTS, load=FakeLoad())


def test_small_arrivals_backfill_only_until_the_short_head_has_waited_settle_s(tmp_path):
    """While the guard stays on (pressure between its two thresholds), an item short only of
    the memory free lets smaller ones by, but only for `settle_s`: after that it blocks its
    pool, the recent grants age out, and it starts, however steadily small items arrive."""
    agent, clock, load = make_agent(
        tmp_path, memory={}, guard={"settle_s": 30}, lease_ttl_s=300, **FOUR
    )
    floor = 16 * G // 20
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=10.0)  # under 20% (not pressing), over 5% (never settles)
    load.available = floor + 7 * G  # 7 GiB free above the floor, less recent grants
    clock.now += 1
    agent.tick()
    assert agent.guard.on and not agent.guard.pressing
    held: dict[str, float] = {}
    assert ask(agent, "small-0", user="bob")["status"] == "granted"  # counts 3 GiB
    held["small-0"] = clock.now
    clock.now += 1
    queued_at = clock.now
    assert ask(agent, "head", weight=2)["status"] == "queued"  # 6 GiB > 4 GiB free
    started, backfilled, n = None, [], 0
    while started is None and clock.now - queued_at < 300:
        clock.now += 5
        for lid, at in list(held.items()):
            if clock.now - at >= 25:  # each small runs 25 s, so two overlap
                agent.release(lid, user="bob")
                del held[lid]
        if round(clock.now - queued_at) % 15 == 10:  # a new small every 15 s
            n += 1
            if ask(agent, f"small-{n}", user="bob")["status"] == "granted":
                held[f"small-{n}"] = clock.now
                backfilled.append(clock.now - queued_at)
        agent.tick()
        if any(x["lease_id"] == "head" for x in agent.leases()["leases"]):
            started = clock.now - queued_at
    assert started is not None and started <= 30 + 25
    assert backfilled and min(backfilled) < 30  # it did let small ones by at first
    assert all(t <= 30 for t in backfilled)  # but none once it had waited settle_s


def _settling(agent, clock, load) -> None:
    """Pressure, then none: the guard on and settling, 7 GiB free above the floor."""
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=0.0)
    load.available = 16 * G // 20 + 7 * G
    clock.now += 1
    agent.tick()
    assert agent.guard.on and not agent.guard.pressing


def test_a_restart_still_counts_what_was_granted_within_the_settle_window(tmp_path):
    """A daemon started again places the leases it finds by their wall-clock `acquired_at`:
    one granted within `settle_s` still counts as recent, an older one does not."""
    clock, load = Clock(), FakeLoad()
    config = machine_config(tmp_path, memory={}, guard={"settle_s": 30}, **FOUR)
    first = MachineAgent(config, facts=FACTS, load=load, clock=clock)
    assert ask(first, "old")["status"] == "granted"  # 3 GiB
    clock.now += 100
    assert ask(first, "new")["status"] == "granted"
    clock.now += 5  # the daemon restarts 5 s after "new" was granted
    again = MachineAgent(config, facts=FACTS, load=load, clock=clock)
    _settling(again, clock, load)
    assert again.memory_free() == 7 * G - 3 * G  # "new" only
    clock.now += 30
    assert again.memory_free() == 7 * G


def test_observed_leases_are_not_counted_as_recent_grants(tmp_path):
    """An observed lease records a process already running: `MemAvailable` already leaves out
    its memory, so the settle window does not count it again."""
    from leasehold.agent import OBSERVED_SCOPE
    from leasehold.ledger import Holder, LedgerItem

    agent, clock, load = make_agent(tmp_path, memory={}, guard={"settle_s": 30}, **FOUR)
    _settling(agent, clock, load)
    item = LedgerItem(
        scope=OBSERVED_SCOPE, item_id="p1", kind="observed", user="ann", pool="work", weight=2
    )
    assert agent.ledger.impose(item, agent.policy(), holder=Holder.current())
    agent.tick()
    assert agent.memory_free() == 7 * G
    assert ask(agent, "a")["status"] == "granted"
    assert agent.memory_free() == 7 * G - 3 * G


def test_doctor_blames_memory_only_when_memory_cut_the_heavy_rules(tmp_path):
    """Standing work holding slots (not memory) cuts the heavy rules by slots: that is no
    memory problem, so the memory check says nothing; standing memory that cuts them is."""
    from leasehold import doctor

    by_slots = [{"name": "svc", "pool": "work", "weight": 2}]
    config = machine_config(tmp_path, memory={}, standing=by_slots, **FOUR)
    assert config.policy().heaviest("work") == 2  # cut, by slots
    assert doctor.check_memory(config, 16 * G) is None
    by_memory = [{"name": "db", "pool": "background", "weight": 1, "memory": "5g"}]
    config = machine_config(tmp_path, memory={}, standing=by_memory, **FOUR)
    check = doctor.check_memory(config, 16 * G)
    assert check.level == "warn" and "[[standing]] memory (5.0 GiB)" in check.detail


@pytest.mark.parametrize("step", ["forward", "back"])
def test_how_long_a_short_head_has_waited_is_measured_on_the_monotonic_clock(tmp_path, step):
    """A step of the wall clock does not change whether a short head lets small items by:
    an hour forward, after 1 s, it still does; an hour back, after 35 s, it no longer does."""
    wall, mono, load = Clock(), Clock(50.0), FakeLoad()
    config = machine_config(tmp_path, memory={}, guard={"settle_s": 30}, lease_ttl_s=100000, **FOUR)
    agent = MachineAgent(config, facts=FACTS, load=load, clock=wall, monotonic=mono)
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=10.0)  # the guard stays on, not pressing
    load.available = 16 * G // 20 + 7 * G

    def advance(seconds: float) -> None:
        wall.now += seconds
        mono.now += seconds
        agent.tick()

    advance(1)
    assert ask(agent, "small-0", user="bob")["status"] == "granted"  # 4 GiB free left
    advance(1)
    assert ask(agent, "head", weight=2)["status"] == "queued"  # needs 6 GiB
    if step == "forward":
        wall.now += 3600
        advance(1)
        assert ask(agent, "small-1", user="bob")["status"] == "granted"  # it still lets by
    else:
        advance(20)
        assert ask(agent, "small-1", user="bob")["status"] == "granted"  # 20 s: lets by
        advance(15)  # head has waited 35 s; small-0 has aged out, small-1 not: 4 GiB free
        wall.now -= 3600
        agent.tick()
        assert ask(agent, "small-2", user="bob")["status"] == "queued"  # it holds the pool


def test_the_overdue_stamps_are_safe_from_several_threads(tmp_path):
    """`policy()` runs unlocked from status and the views while acquire runs it under the lock:
    two threads forgetting the same item that left the line must not race (a race is a
    KeyError, a 500 in the daemon). A slow delete opens the window every time."""
    import threading
    import time

    class SlowDelete(dict):
        def __delitem__(self, key):
            time.sleep(0.05)
            super().__delitem__(key)

    agent, _, _ = make_agent(tmp_path, memory={}, **FOUR)
    agent._queued_at = SlowDelete({("leasehold", "gone"): 0.0})  # no longer in line
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def overdue() -> None:
        start.wait()
        try:
            agent._overdue()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=overdue) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and dict(agent._queued_at) == {}
