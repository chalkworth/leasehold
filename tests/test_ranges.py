"""Ranged leases against a real daemon: a request for MIN-MAX slots waits for MIN, is granted a
size it keeps, and every answer, view and event says both the size and the range; a client
asking a daemon from before ranges is granted the least; the config's rules and the MCP server
take ranges too."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field
from support import FACTS, FakeLoad, machine_config, make_agent
from test_memory import FOUR

from leasehold import api, shell, top, weights
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.client import ApiError, Client
from leasehold.commands import CommandRule, CommandSettings
from leasehold.ledger import Admitted, LedgerItem
from leasehold.mcp_server import Held, Leases, acquire_lease
from leasehold.server import Daemon
from leasehold.top.render import _needs, _weight_cell

G = 2**30


def test_a_range_is_granted_a_size_it_keeps_and_every_answer_says_so(daemon):
    client = Client(daemon.path)
    answer = client.acquire(kind="work", weight=1, max_weight=2, lease_id="r", ttl_s=30)
    assert answer["status"] == "granted"
    lease = answer["lease"]
    assert (lease["weight"], lease["min_weight"], lease["max_weight"]) == (2, 1, 2)
    assert lease["jobs"] == 2  # the job count of the size granted
    renewal = client.renew("r")
    assert (renewal["weight"], renewal["min_weight"], renewal["max_weight"]) == (2, 1, 2)
    [held] = client.status()["leases"]
    assert (held["weight"], held["min_weight"], held["max_weight"]) == (2, 1, 2)
    assert client.status()["pools"]["work"]["held_weight"] == 2
    queued = client.acquire(kind="work", weight=1, max_weight=2, lease_id="q", ttl_s=30)
    assert queued["status"] == "queued"
    assert queued["blocked"] == "pool work holds 2 of 2; this needs at least 1 (asks up to 2)"
    [waiting] = client.leases()["queue"]
    assert (waiting["weight"], waiting["min_weight"], waiting["max_weight"]) == (1, 1, 2)
    granted = [e for e in client.events()["events"] if e["type"] == "granted"]
    assert [(e["weight"], e["min_weight"], e["max_weight"]) for e in granted] == [(2, 1, 2)]
    # A fixed weight is the range of one, as before.
    client.release("r")
    client.release("q")
    fixed = client.acquire(kind="work", weight=1, lease_id="f")["lease"]
    assert (fixed["weight"], fixed["min_weight"], fixed["max_weight"]) == (1, 1, 1)


def test_the_most_asked_is_cut_to_what_one_lease_could_ever_hold(daemon):
    lease = Client(daemon.path).acquire(kind="work", weight=1, max_weight=50, lease_id="r")
    assert (lease["lease"]["weight"], lease["lease"]["max_weight"]) == (2, 2)


@pytest.mark.parametrize(
    "body",
    [
        {"weight": 3, "max_weight": 2},
        {"weight": 1, "memory": "1g", "memory_per_slot": "1g"},
        {"weight": 1, "max_weight": 0},
    ],
)
def test_an_impossible_range_or_two_memories_is_a_bad_request(daemon, body):
    with pytest.raises(ApiError) as caught:
        Client(daemon.path).acquire(kind="work", lease_id="x", **body)
    assert caught.value.status == 400


def test_memory_per_slot_is_counted_at_the_granted_size_and_refused_by_its_least(
    tmp_path, sock_dir
):
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "m.sock"),
        ceiling={"slots": 4, "memory": "6g"},
        memory={"budget": "9g", "assumed_per_slot": "1g"},
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        client = Client(daemon.path)
        # 3 GiB a slot: the ceiling's 6 GiB holds 2 slots of the 1-4 asked, and the budget
        # holds them beside the headroom's slot at that size.
        lease = client.acquire(kind="work", weight=1, max_weight=4, memory_per_slot="3g")
        assert (lease["lease"]["weight"], lease["lease"]["max_weight"]) == (2, 2)
        assert lease["lease"]["memory_per_slot_bytes"] == 3 * G
        status = client.status()
        assert status["leases"][0]["memory_counted_bytes"] == 6 * G
        refused = client.acquire(kind="work", weight=3, max_weight=4, memory_per_slot="3g")
        assert (
            refused["status"] == "refused"
            and "memory_per_slot 3g at weight 3" in (refused["reason"])
        )
    finally:
        daemon.stop()


def test_the_memory_headroom_at_the_ranges_size_keeps_a_range_to_its_least(tmp_path, sock_dir):
    # A budget of 8 GiB: 2 slots at 3 GiB and the headroom's slot at 3 GiB is 9, so the 1-4 is
    # granted 1 (its max cut to 2), and a 1 at 3 GiB a slot then starts.
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "h.sock"),
        ceiling={"slots": 4, "memory": "6g"},
        memory={"budget": "8g", "assumed_per_slot": "1g"},
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        client = Client(daemon.path)
        lease = client.acquire(kind="work", weight=1, max_weight=4, memory_per_slot="3g")
        assert (lease["lease"]["weight"], lease["lease"]["max_weight"]) == (1, 2)
        after = client.acquire(kind="work", weight=1, memory_per_slot="3g")
        assert after["status"] == "granted"
    finally:
        daemon.stop()


class _BeforeRanges(BaseModel):
    """`POST /v1/acquire` as a daemon from before ranges takes it."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    pool: str | None = None
    weight: int = Field(1, ge=1)
    labels: dict[str, str] = Field(default_factory=dict)
    lease_id: str | None = None
    session: str | None = None
    wait_s: float = 0.0
    ttl_s: float | None = None
    memory: str | None = None


def test_a_daemon_from_before_ranges_grants_the_least_to_a_new_client(daemon, monkeypatch):
    def acquire(agent, req, peer, rest):
        return agent.acquire_wait(AcquireRequest(**req.model_dump()), user=peer.user)

    monkeypatch.setitem(api.ROUTES, ("POST", "/v1/acquire"), api.Route(_BeforeRanges, acquire))
    client = Client(daemon.path)
    answer = client.acquire(kind="work", weight=1, max_weight=2, lease_id="r")
    assert answer["status"] == "granted" and answer["lease"]["weight"] == 1
    # A per-slot size is declared as the memory of the least it is granted.
    answer = client.acquire(kind="work", weight=1, memory_per_slot="1g", lease_id="s")
    assert answer["lease"]["memory_bytes"] == G and answer["lease"]["memory_per_slot_bytes"] is None


def test_the_client_asks_again_without_each_field_an_older_daemon_does_not_take(monkeypatch):
    sent = []
    taken = [{"kind", "weight", "lease_id", "memory"}, {"kind", "weight", "lease_id"}]

    def request(self, method, url, body=None, *, extra_timeout=0.0):
        sent.append(dict(body))
        unknown = sorted(set(body) - taken[0])
        if unknown:
            detail = [{"type": "extra_forbidden", "loc": [name], "msg": "x"} for name in unknown]
            raise ApiError(400, {"error": "bad_request", "detail": detail})
        return {"status": "granted", "lease": {}}

    monkeypatch.setattr(Client, "request", request)
    Client("/nowhere").acquire(
        kind="work", weight=2, max_weight=4, memory_per_slot="1g", lease_id="a"
    )
    assert sent[1:] == [{"kind": "work", "weight": 2, "lease_id": "a", "memory": f"{2 * G}b"}]
    sent.clear()
    taken.pop(0)  # a daemon older still, which takes no memory either
    Client("/nowhere").acquire(
        kind="work", weight=2, max_weight=4, memory_per_slot="1g", lease_id="a"
    )
    assert sent[-1] == {"kind": "work", "weight": 2, "lease_id": "a"}


# --- rules ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("weight", "least", "most"),
    [(4, 4, 4), ("4", 4, 4), ("2-4", 2, 4), (" 2 - 4 ", 2, 4)],
)
def test_a_rules_weight_is_a_count_or_a_range(weight, least, most):
    rule = CommandRule(programs=["x"], weight=weight)
    assert (rule.weight, rule.most) == (least, most)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"weight": "4-2"}, "below the least"),
        ({"weight": "0-2"}, "at least 1"),
        ({"weight": "two"}, "not a weight"),
        ({"weight": 2, "max_weight": 4}, "max_weight is not a key"),
        ({"weight": 2, "memory": "1g", "memory_per_slot": "1g"}, "one or the other"),
        ({"weight": 2, "memory_per_slot": "0b"}, "is 0"),
    ],
)
def test_a_rule_with_a_bad_range_or_memory_does_not_load(fields, message):
    with pytest.raises(ValueError, match=message):
        CommandRule(programs=["x"], **fields)


def test_the_daemon_serves_each_rules_range_cut_to_what_could_be_granted(tmp_path):
    config = machine_config(
        tmp_path,
        commands={
            "defaults": False,
            "rule": [
                {"programs": ["a"], "weight": "1-9"},
                {"programs": ["b"], "weight": "3-4"},
                {"programs": ["c"], "weight": 2, "memory_per_slot": "1g"},
            ],
        },
    )
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    served = {r["programs"][0]: r for r in agent.commands()["rules"]}
    assert (served["a"]["weight"], served["a"]["max_weight"]) == (1, 2)
    assert (served["b"]["weight"], served["b"]["max_weight"]) == (2, 2)  # cut as before
    assert (served["c"]["weight"], served["c"]["max_weight"]) == (2, 2)
    assert served["c"]["memory_per_slot"] == "1g"
    assert {r["ceiling"] for r in served.values()} == {2}
    rules = shell.served(agent.commands()).rules
    assert {r.max_weight for r in rules} == {2}
    assert {r.ceiling for r in rules} == {2}


def test_a_per_slot_rule_is_also_served_a_total_for_clients_from_before_it(tmp_path):
    config = machine_config(
        tmp_path,
        commands={
            "defaults": False,
            "rule": [
                {"programs": ["c"], "weight": "1-2", "memory_per_slot": "1g"},
                {"programs": ["d"], "weight": 1, "memory": "3g"},
            ],
        },
    )
    agent = MachineAgent(config, facts=FACTS, load=FakeLoad())
    served = {r["programs"][0]: r for r in agent.commands()["rules"]}
    # A 0.3 client reads `memory` and declares the least's worth; a newer one the size per slot.
    assert served["c"]["memory"] == f"{G}b"
    assert served["d"]["memory"] == "3g" and served["d"]["memory_per_slot"] is None
    rules = {r.programs: r for r in shell.served(agent.commands()).rules}
    assert (rules[frozenset({"c"})].memory, rules[frozenset({"c"})].memory_per_slot) == (None, "1g")
    assert rules[frozenset({"d"})].memory == "3g"


def test_a_rule_from_a_daemon_before_ranges_asks_its_weight_alone():
    old = shell.Rule.of({"programs": ["pytest"], "weight": 3})
    assert (old.weight, old.max_weight, old.memory_per_slot) == (3, 3, None)
    assert shell.Rule.of({"programs": ["x"], "weight": 3, "max_weight": 2}).max_weight == 3


def test_rules_are_chosen_by_their_least_the_first_of_equals():
    settings = CommandSettings(
        defaults=False,
        rule=[
            {"programs": ["x"], "weight": "2-8"},
            {"programs": ["x"], "weight": 3},
            {"programs": ["x"], "args": "fast", "weight": "3-4"},
        ],
    )
    rules = settings.shell_rules()
    assert shell.match(rules, "x").rule.weight == 3
    found = shell.match(rules, "x fast").rule
    assert (found.weight, found.max_weight) == (3, 3)  # the first rule of least 3


def test_leasehold_want_replaces_the_rules_size_and_a_bad_one_is_said():
    said = []
    assert weights.asked({"LEASEHOLD_WANT": "2-6"}, 4, 4, said.append) == (2, 6)
    assert weights.asked({"LEASEHOLD_WANT": "3"}, 4, 8, said.append) == (3, 3)
    assert weights.asked({}, 4, None, said.append) == (4, 4)
    assert said == []
    cut: list[str] = []
    # Past what the rule's lease could ever be granted: cut, and said, not refused.
    assert weights.asked({"LEASEHOLD_WANT": "6-9"}, 1, 2, cut.append, 4) == (4, 4)
    assert weights.asked({"LEASEHOLD_WANT": "2-9"}, 1, 2, cut.append, 4) == (2, 4)
    assert cut == [
        "LEASEHOLD_WANT=6-9 cut to 4, the most one lease could be granted",
        "LEASEHOLD_WANT=2-9 cut to 2-4, the most one lease could be granted",
    ]
    assert weights.asked({"LEASEHOLD_WANT": "9"}, 1, 2, cut.append, None) == (9, 9)
    assert weights.asked({"LEASEHOLD_WANT": "6-2"}, 2, 4, said.append) == (2, 4)
    assert said and "LEASEHOLD_WANT ignored" in said[0] and "2-4" in said[0]


# --- views ----------------------------------------------------------------------------------------


def test_top_shows_the_size_granted_of_the_range_and_what_a_waiting_range_needs():
    lease = {"weight": 3, "min_weight": 2, "max_weight": 4}
    assert _weight_cell(lease) == "3/2-4"
    assert _weight_cell({"weight": 3, "min_weight": 3, "max_weight": 3}) == "3"
    assert _weight_cell({"weight": 3}) == "3"  # a daemon from before ranges
    assert _needs({"weight": 2, "min_weight": 2, "max_weight": 4}) == "2-4 slots"
    assert _needs({"weight": 1}) == "1 slot"


def test_leasehold_leases_shows_the_size_granted_of_the_range(daemon, capsys):
    from leasehold.cli import main

    client = Client(daemon.path)
    client.acquire(kind="work", weight=1, max_weight=2, lease_id="r")
    client.acquire(kind="work", weight=1, max_weight=2, lease_id="q", ttl_s=30)
    assert main(["--socket", str(daemon.path), "leases"]) == 0
    out = capsys.readouterr().out
    rows = {line.split()[0]: line.split() for line in out.splitlines()[1:]}
    assert rows["r"][3] == "2/1-2" and rows["q"][3] == "1-2"


# --- the MCP server ---------------------------------------------------------------------------


def test_the_mcp_server_says_the_range_the_daemon_cut_not_the_one_asked(daemon):
    leases = Leases(Client(daemon.path))
    try:
        answer = acquire_lease(leases, weight=1, max_weight=50, wait_s=0)
        assert (answer.data["min_weight"], answer.data["max_weight"]) == (1, 2)
        assert "(of 1-2)" in answer.text
    finally:
        leases.close()


def test_a_range_granted_while_away_takes_the_daemons_sizes_from_the_renewal(daemon):
    leases = Leases(Client(daemon.path))
    try:
        first = acquire_lease(leases, weight=2, wait_s=0).data["lease_id"]
        queued = acquire_lease(leases, weight=1, max_weight=50, wait_s=0).data
        # The range as the daemon stored and cut it: one lease could never hold 50 here.
        assert (queued["status"], queued["min_weight"], queued["max_weight"]) == ("queued", 1, 2)
        leases.client.release(first)
        leases.forget(first, "released")
        Client(daemon.path).status()  # a tick admits it
        held = next(h for h in leases.items() if h.lease_id == queued["lease_id"])
        until_granted(leases, held)
        public = held.public()
        assert public["state"] == "granted"
        assert (public["weight"], public["min_weight"], public["max_weight"]) == (2, 1, 2)
        assert (public["pool"], public["jobs"]) == ("work", 2)  # from the renewal
    finally:
        leases.close()


def test_a_grant_from_a_daemon_that_says_no_range_shows_none():
    # A daemon from before ranges grants the least, and its answers carry no range.
    held = Held("mcp.x", "work", None, 1, "", max_weight=4)
    held.state, held.lease = "granted", {"weight": 1, "pool": "work", "jobs": 1}
    public = held.public()
    assert public["weight"] == 1 and "min_weight" not in public and "max_weight" not in public
    held = Held("mcp.y", "work", None, 1, "", max_weight=4)
    assert "max_weight" not in held.public()  # queued, and the answer said no range


def until_granted(leases, held, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while held.state != "granted":
        assert time.monotonic() < deadline, "never granted"
        leases.renew_all()
        time.sleep(0.05)


def test_the_mcp_server_asks_a_range_when_given_a_max_and_a_weight_otherwise(daemon):
    leases = Leases(Client(daemon.path))
    try:
        answer = acquire_lease(leases, weight=1, max_weight=2, wait_s=0)
        assert answer.data["weight"] == 2 and answer.data["max_weight"] == 2
        assert "weight 2 (of 1-2)" in answer.text
        leases.client.release(answer.data["lease_id"])
        answer = acquire_lease(leases, weight=1, wait_s=0)
        assert answer.data["weight"] == 1 and "max_weight" not in answer.data
    finally:
        leases.close()


#: `FOUR`'s pools, with no headroom in `work`.
ROOMY_FOUR = FOUR["pools"] | {"work": FOUR["pools"]["work"] | {"headroom": 0}}


def test_a_waiting_range_is_cut_when_admitted_not_when_asked(tmp_path):
    # ann may run one lease in work at a time, so r waits for h however the pool changes.
    pools = ROOMY_FOUR | {"work": ROOMY_FOUR["work"] | {"reserved": 0, "per_user_max_running": 1}}
    agent, _, _ = make_agent(tmp_path, **(FOUR | {"pools": pools}))
    agent.set_pool_max("work", 1)
    agent.acquire(AcquireRequest(kind="work", lease_id="h", weight=1), user="ann")
    req = AcquireRequest(kind="work", lease_id="r", weight=1, max_weight=50)
    queued = agent.acquire(req, user="ann")
    assert (queued["status"], queued["min_weight"], queued["max_weight"]) == ("queued", 1, 1)
    [waiting] = agent.leases()["queue"]
    assert waiting["max_weight"] == 1  # as admission would cut it now; 50 is kept
    agent.set_pool_max("work", None)
    agent.release("h", user="ann")
    [lease] = agent.leases()["leases"]
    assert (lease["lease_id"], lease["weight"], lease["max_weight"]) == ("r", 4, 4)


def _hooked(tmp_path):
    """8 slots and 4 GiB a lease, no memory budget: work (headroom 2) and ci (headroom 0)."""
    return make_agent(
        tmp_path,
        ceiling={"slots": 8, "memory": "4g"},
        pools={
            "work": {"reserved": 0, "max": 8, "priority": 0, "headroom": 2},
            "ci": {"reserved": 0, "max": 8, "priority": 1, "headroom": 0},
        },
        kinds={"work": "work", "external": "ci"},
        lend={"pools": [], "quiet_s": 0},
        commands={
            "defaults": False,
            "rule": [
                {"programs": ["big"], "weight": "1-8", "memory_per_slot": "1g"},
                {"programs": ["cib"], "weight": "1-8", "pool": "ci"},
            ],
        },
    )


def test_the_hook_ceiling_is_what_the_range_alone_would_be_granted(tmp_path):
    agent, _, _ = _hooked(tmp_path)
    served = {r["programs"][0]: r for r in agent.commands()["rules"]}
    # 4 GiB a lease at 1 GiB a slot: 4, not 8 less the headroom.
    assert (served["big"]["ceiling"], served["big"]["hook_ceiling"]) == (4, 4)
    # ci has no headroom of its own, but the machine keeps work's 2.
    assert (served["cib"]["ceiling"], served["cib"]["hook_ceiling"]) == (8, 6)
    fixed = AcquireRequest(kind="work", lease_id="f", weight=4, memory_per_slot="1g")
    assert agent.acquire(fixed, user="ann")["lease"]["weight"] == 4
    agent.release("f", user="ann")
    ranged = AcquireRequest(kind="work", lease_id="r", weight=1, max_weight=8, memory_per_slot="1g")
    assert agent.acquire(ranged, user="ann")["lease"]["weight"] == 4
    agent.release("r", user="ann")
    lone = AcquireRequest(kind="external", lease_id="c", weight=1, max_weight=8)
    assert agent.acquire(lone, user="ann")["lease"]["weight"] == 6


def test_one_leases_memory_limits_a_per_slot_size_with_no_budget_too(tmp_path):
    agent, _, _ = _hooked(tmp_path)
    over = AcquireRequest(kind="work", lease_id="o", weight=5, memory_per_slot="1g")
    answer = agent.acquire(over, user="ann")
    assert answer["status"] == "refused"
    assert answer["reason"] == (
        "memory_per_slot 1g at weight 5 (5.0 GiB) is more than the 4g one lease may be given "
        "([ceiling] memory)"
    )


def test_with_no_ceiling_memory_the_refusal_names_the_machines_memory(tmp_path):
    agent, _, _ = make_agent(tmp_path, ceiling={"slots": 8})
    over = AcquireRequest(kind="work", lease_id="o", weight=2, memory_per_slot="10g")
    answer = agent.acquire(over, user="ann")
    assert answer["reason"] == (
        "memory_per_slot 10g at weight 2 (20.0 GiB) is more than the 16.0 GiB one lease may be "
        "given (the machine's memory)"
    )


def test_a_per_slot_rule_whose_least_is_over_one_leases_memory_is_never_granted(tmp_path):
    # 4-8 at 6 GiB a slot is 24 GiB at its least, over the 16 GiB one lease may be given: with
    # no budget too, as acquire refuses it, the rule is served as never rather than cut below 4.
    agent, _, _ = make_agent(
        tmp_path,
        ceiling={"slots": 8, "memory": "16g"},
        commands={
            "defaults": False,
            "rule": [{"programs": ["huge"], "weight": "4-8", "memory_per_slot": "6g"}],
        },
    )
    [rule] = agent.commands()["rules"]
    assert "more than the 16g one lease may be given" in rule["never"]
    assert rule["weight"] == 4
    asked = AcquireRequest(kind="work", lease_id="h", weight=4, max_weight=8, memory_per_slot="6g")
    assert agent.acquire(asked, user="ann")["status"] == "refused"


def test_the_memory_guard_counts_a_recent_range_at_its_granted_size(tmp_path):
    agent, clock, load = make_agent(tmp_path, memory={}, **(FOUR | {"pools": ROOMY_FOUR}))
    load.available = 14 * G
    req = AcquireRequest(kind="work", lease_id="r", weight=1, max_weight=4, memory_per_slot="1g")
    assert agent.acquire(req, user="ann")["lease"]["weight"] == 4
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=0.0)
    clock.now += 1
    agent.tick()
    assert agent.guard.on
    assert agent.memory_free() == 14 * G - 16 * G // 20 - 4 * G  # 4 slots at 1 GiB, not 1


def test_while_the_memory_guard_is_on_a_range_is_granted_its_least(tmp_path):
    agent, clock, load = make_agent(tmp_path, memory={}, **(FOUR | {"pools": ROOMY_FOUR}))
    load.set(memory=50.0)
    agent.tick()
    load.set(memory=0.0)
    load.available = 14 * G
    clock.now += 1
    agent.tick()
    assert agent.guard.on
    req = AcquireRequest(kind="work", lease_id="r", weight=1, max_weight=4, memory_per_slot="1g")
    assert agent.acquire(req, user="ann")["lease"]["weight"] == 1


def test_top_renders_a_ranged_lease_and_a_waiting_range():
    status = json.loads((Path(__file__).parent / "fixtures/top/status-busy.json").read_text())
    status["leases"][0] |= {"weight": 3, "min_weight": 2, "max_weight": 4}
    status["queue"][0] |= {"weight": 2, "min_weight": 2, "max_weight": 4}
    text = top.plain(top.render(status, [], 100, 40, now=status["time"] + 1))
    assert " 3/2-4 " in text and "needs 2-4 slots" in text


def test_the_guard_counts_a_memory_per_slot_of_0_as_nothing(tmp_path):
    agent, _clock, load = make_agent(tmp_path, memory={}, **FOUR)
    load.available = 14 * G
    agent.tick()
    before = agent.memory_free()
    item = LedgerItem("lib", "z", "work", "ann", "work", 2, memory_per_slot=0)
    assert agent.ledger.try_admit(item, agent.policy()) == Admitted()
    assert agent.memory_free() == before  # 2 slots at 0 bytes, not at the assumed size


def test_a_queued_range_is_given_as_cut_on_every_surface(daemon):
    # Pool work holds at most 2, so a waiting 1-50 is a 1-2 wherever it is shown.
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, lease_id="h")
    queued = client.acquire(kind="work", weight=1, max_weight=50, lease_id="q")
    assert (queued["min_weight"], queued["max_weight"]) == (1, 2)
    assert queued["blocked"].endswith("this needs at least 1 (asks up to 2)")
    [row] = client.leases()["queue"]
    [status_row] = client.status()["queue"]
    [event] = [e for e in client.events()["events"] if e["type"] == "queued"]
    assert row["max_weight"] == status_row["max_weight"] == event["max_weight"] == 2
    assert _needs(status_row) == _needs(event) == "1-2 slots"
