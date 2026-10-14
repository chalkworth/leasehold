"""Known programs started outside leasehold (leasehold.observe, leasehold.proctable): which
processes a rule counts, and the observed leases the daemon imposes and ends with them."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid

import pytest
from support import FACTS, Clock, FakeLoad, cli, machine_config

from leasehold import proctable
from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.observe import Match, ObserveRule, is_leasehold, item_id, roots, under_leasehold
from leasehold.proctable import Proc

UID = os.getuid()


def proc(pid, ppid=1, comm="python3", cmdline="", start=None, uid=UID) -> Proc:
    p = Proc(pid=pid, ppid=ppid, uid=uid, start=start or pid, comm=comm)
    p._read.add("cmdline")
    p._cmdline = cmdline
    return p


def rule(**kw) -> ObserveRule:
    base = {"name": "prep", "match": {"cmdline": r"prep_dataset\.py"}, "weight": 3}
    return ObserveRule.model_validate(base | kw)


def table(*procs: Proc) -> dict[int, Proc]:
    return {p.pid: p for p in procs}


# --- matching and roots -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("comm", "cmdline", "expected"),
    [
        ("leasehold", "/home/u/.local/bin/leasehold serve", True),
        ("python3", "/opt/leasehold/bin/python3 /opt/leasehold/bin/leasehold hold -- make", True),
        ("python3", "python3 -m leasehold.shims /s pytest -x", True),
        ("python3.12", "/usr/bin/python3.12 -X dev -m leasehold status", True),
        ("python3", "/opt/leasehold/bin/python3 -I -s /opt/leasehold/bin/leasehold hold", True),
        ("python3", "python3 -W ignore /x/leasehold status", True),
        ("python3", "python3 -c import leasehold", False),
        ("python3", "python3 script.py -m leasehold", False),
        ("vim", "vim leasehold", False),
        ("python3", "python3 prep_dataset.py --out leasehold", False),
        ("python3", "python3 -m pytest", False),
    ],
)
def test_leaseholds_own_commands_are_known(comm, cmdline, expected):
    assert is_leasehold(proc(10, comm=comm, cmdline=cmdline)) is expected


def test_what_runs_under_leasehold_is_not_observed():
    procs = table(
        proc(10, comm="leasehold", cmdline="leasehold hold -- python3 prep_dataset.py"),
        proc(11, ppid=10, cmdline="python3 prep_dataset.py"),  # holds hold's lease already
        proc(12, comm="leasehold", cmdline="leasehold hold -- python3 prep_dataset.py --b"),
        proc(20, cmdline="python3 prep_dataset.py --by-hand"),  # waiting hold above: itself
    )
    counted = roots(rule(), procs, {}, under_leasehold(procs))
    assert [p.pid for p in counted] == [20]
    assert [p.pid for p in roots(rule(), procs, {})] == [10, 12, 20]  # without the check


def test_a_rule_counts_only_the_root_of_a_matching_tree():
    procs = table(
        proc(10, cmdline="python3 prep_dataset.py --all"),
        proc(11, ppid=10, cmdline="python3 prep_dataset.py --worker 1"),
        proc(12, ppid=11, cmdline="python3 prep_dataset.py --worker 2"),
        proc(20, ppid=1, cmdline="python3 prep_dataset.py --other"),
        proc(30, ppid=10, cmdline="sort"),
    )
    assert [p.pid for p in roots(rule(), procs, {})] == [10, 20]


def test_a_match_under_a_non_matching_parent_is_its_own_root():
    procs = table(
        proc(5, comm="bash", cmdline="bash"),
        proc(6, ppid=5, cmdline="python3 prep_dataset.py"),
    )
    assert [p.pid for p in roots(rule(), procs, {})] == [6]


def test_a_child_whose_parent_vanished_mid_scan_is_a_root():
    procs = table(proc(7, ppid=999, cmdline="python3 prep_dataset.py"))
    assert [p.pid for p in roots(rule(), procs, {})] == [7]


def test_every_given_field_must_match():
    procs = table(
        proc(1, comm="python3", cmdline="python3 prep_dataset.py", uid=UID),
        proc(2, comm="pypy", cmdline="pypy prep_dataset.py", uid=UID),
        proc(3, comm="python3", cmdline="python3 prep_dataset.py", uid=UID + 1),
    )
    picky = rule(match={"comm": "python3", "cmdline": "prep_dataset", "user": "ann"})
    assert [p.pid for p in roots(picky, procs, {"ann": UID})] == [1]
    assert roots(picky, procs, {"ann": None}) == []  # a user that does not exist


def test_max_count_keeps_the_oldest():
    procs = table(*(proc(p, ppid=0, start=100 - p, cmdline="prep_dataset.py") for p in (1, 2, 3)))
    assert [p.pid for p in roots(rule(max_count=2), procs, {})] == [3, 2]


def test_a_rule_must_match_on_something_and_its_patterns_must_compile():
    with pytest.raises(ValueError, match="at least one"):
        Match()
    with pytest.raises(ValueError):
        Match(cmdline="(")


def test_the_lease_id_is_unique_to_the_process_lifetime():
    assert item_id(rule(), proc(42, start=777)) == "observed.prep.42.777"


# --- the process table ------------------------------------------------------------------------


def fake_proc(root, pid, *, ppid=1, comm="x", state="S", start=5, uid=1000, cmdline=b""):
    d = root / str(pid)
    d.mkdir()
    rest = " ".join([state, str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5)
    (d / "stat").write_text(f"{pid} ({comm}) {rest}\n")
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "cmdline").write_bytes(cmdline)


def test_scan_reads_pid_parent_owner_start_comm_and_command_line(tmp_path):
    fake_proc(tmp_path, 1, ppid=0, comm="init")
    fake_proc(tmp_path, 40, ppid=1, comm="a) b", start=99, uid=7, cmdline=b"python3\0x.py\0")
    fake_proc(tmp_path, 41, state="Z")  # a zombie runs nothing
    (tmp_path / "self").mkdir()
    procs = proctable.scan(tmp_path)
    assert sorted(procs) == [1, 40]
    p = procs[40]
    assert (p.ppid, p.uid, p.start, p.comm, p.cmdline) == (1, 7, 99, "a) b", "python3 x.py")
    assert p.exe is None  # no exe link in the fake tree


def test_scan_of_the_real_proc_finds_this_process():
    me = proctable.scan()[os.getpid()]
    assert me.uid == UID and me.ppid == os.getppid() and me.cmdline


# --- the daemon -------------------------------------------------------------------------------


@pytest.fixture
def prep():
    """A real process that looks like a data-prep run, under a name nothing else on the machine
    has (not even the shell that started these tests), so it is a root."""
    marker = f"prep_{uuid.uuid4().hex[:12]}.py"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", marker])
    child.marker = marker
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def observing_agent(tmp_path, prep, **extra):
    config = machine_config(
        tmp_path,
        observe=[{"name": "prep", "match": {"cmdline": re.escape(prep.marker)}, "weight": 2}],
        **extra,
    )
    return MachineAgent(config, facts=FACTS, load=FakeLoad(), clock=Clock())


def observed(agent) -> list[dict]:
    return [lease for lease in agent.leases()["leases"] if lease["kind"] == "observed"]


def test_a_running_program_is_charged_and_its_lease_ends_with_it(tmp_path, prep):
    agent = observing_agent(tmp_path, prep)
    agent.tick()
    [lease] = observed(agent)
    assert lease["pool"] == "observed" and lease["weight"] == 2
    assert lease["labels"]["rule"] == "prep" and lease["labels"]["pid"] == str(prep.pid)
    agent.tick()
    assert len(observed(agent)) == 1  # seen again: still one lease
    st = agent.status()
    assert st["observe"] == [
        {"name": "prep", "weight": 2, "pool": "observed", "max_count": None, "running": 1}
    ]
    events = [e["type"] for e in agent.events.since(0)["events"] if e.get("kind") == "observed"]
    assert events == ["granted"]
    prep.kill()
    prep.wait()
    agent.tick()
    assert observed(agent) == []
    ended = [e for e in agent.events.since(0)["events"] if e["type"] == "ended"]
    assert ended and ended[0]["reason"] == "holder gone"


def test_an_observed_program_holds_back_new_work_and_says_why(tmp_path, prep):
    agent = observing_agent(tmp_path, prep)  # 2 slots: the program takes both
    agent.tick()
    answer = agent.acquire(AcquireRequest(kind="work", lease_id="W"), user="ann")
    assert answer["status"] == "queued" and answer["blocked"]
    prep.kill()
    prep.wait()
    agent.tick()
    assert agent.acquire(AcquireRequest(kind="work", lease_id="W"), user="ann")["status"] == (
        "granted"
    )


def test_observing_needs_an_observed_pool_and_charges_only_there(tmp_path):
    with pytest.raises(ValueError, match="no observed pool"):
        machine_config(
            tmp_path,
            pools={"work": {"max": 2}, "external": {"max": 1}, "lend": {"max": 1}},
            observe=[{"name": "p", "match": {"comm": "x"}, "weight": 1}],
        )
    with pytest.raises(ValueError, match="not the observed pool"):
        machine_config(tmp_path, observe=[{"name": "p", "match": {"comm": "x"}, "weight": 1,
                                           "pool": "work"}])  # fmt: skip
    with pytest.raises(ValueError, match="named twice"):
        machine_config(
            tmp_path,
            observe=[{"name": "p", "match": {"comm": "x"}, "weight": 1}] * 2,
        )


def test_the_observe_command_shows_what_matches_now(tmp_path, prep, sock_dir):
    config = tmp_path / "m.toml"
    config.write_text(
        '[ceiling]\nslots = 2\n[[observe]]\nname = "prep"\nweight = 2\n'
        f'[observe.match]\ncmdline = "{prep.marker}"\n'
    )
    shown = cli(sock_dir / "unused.sock", "--config", str(config), "observe")
    assert shown.returncode == 0, shown.stderr
    assert "prep: weight 2 each, 1 running now" in shown.stdout
    assert f"pid {prep.pid}" in shown.stdout


def test_a_process_that_stops_matching_gives_its_lease_back(tmp_path):
    """It exec'd into something else: still running, no longer the program the rule names."""
    me = proctable.scan()[os.getpid()]
    looks = proc(me.pid, ppid=0, start=me.start, cmdline="python3 prep_dataset.py")
    table_now = {me.pid: looks}
    config = machine_config(
        tmp_path, observe=[{"name": "prep", "match": {"cmdline": "prep_dataset"}, "weight": 1}]
    )
    agent = MachineAgent(
        config, facts=FACTS, load=FakeLoad(), clock=Clock(), processes=lambda: table_now
    )
    agent.tick()
    assert len(observed(agent)) == 1
    table_now = {me.pid: proc(me.pid, ppid=0, start=me.start, cmdline="python3 serve.py")}
    agent.tick()
    assert observed(agent) == []


def test_a_process_the_scan_cannot_see_keeps_its_lease_while_it_runs(tmp_path):
    me = proctable.scan()[os.getpid()]
    table_now = {me.pid: proc(me.pid, ppid=0, start=me.start, cmdline="prep_dataset")}
    config = machine_config(
        tmp_path, observe=[{"name": "prep", "match": {"cmdline": "prep_dataset"}, "weight": 1}]
    )
    agent = MachineAgent(
        config, facts=FACTS, load=FakeLoad(), clock=Clock(), processes=lambda: table_now
    )
    agent.tick()
    table_now = {}  # hidden from the scan (hidepid), but alive: the ledger still sees it
    agent.tick()
    assert len(observed(agent)) == 1


def test_a_client_lease_named_like_an_observed_one_is_its_own_lease(tmp_path, prep):
    agent = observing_agent(tmp_path, prep, ceiling={"slots": 4})
    agent.tick()
    [mine] = observed(agent)
    answer = agent.acquire(AcquireRequest(kind="work", lease_id=mine["lease_id"]), user="ann")
    assert answer["status"] == "granted"
    events = agent.events.since(0)["events"]
    granted = [(e["kind"], e["lease_id"]) for e in events if e["type"] == "granted"]
    assert granted == [("observed", mine["lease_id"]), ("work", mine["lease_id"])]


def test_a_lease_left_by_a_removed_rule_is_given_back(tmp_path):
    """The daemon restarts on the same ledger with the rule gone: the process no longer counts."""
    me = proctable.scan()[os.getpid()]
    table_now = {me.pid: proc(me.pid, ppid=0, start=me.start, cmdline="prep_dataset")}
    rules = [{"name": "prep", "match": {"cmdline": "prep_dataset"}, "weight": 1}]
    first = MachineAgent(
        machine_config(tmp_path, observe=rules), facts=FACTS, load=FakeLoad(), clock=Clock(),
        processes=lambda: table_now,
    )  # fmt: skip
    first.tick()
    assert len(observed(first)) == 1
    again = MachineAgent(
        machine_config(tmp_path), facts=FACTS, load=FakeLoad(), clock=Clock(),
        processes=lambda: table_now,
    )  # fmt: skip
    again.tick()
    assert observed(again) == []
    [ended] = [e for e in again.events.since(0)["events"] if e["type"] == "ended"]
    assert ended["reason"] == "no longer matched"


def test_another_users_process_is_alive_while_it_exists():
    from leasehold.process import _hidden

    assert _hidden(1)  # init: another user's, and running
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not _hidden(child.pid)


def test_a_removed_rules_lease_ends_even_when_its_process_is_hidden(tmp_path):
    me = proctable.scan()[os.getpid()]
    table_now = {me.pid: proc(me.pid, ppid=0, start=me.start, cmdline="prep_dataset")}
    rules = [{"name": "prep", "match": {"cmdline": "prep_dataset"}, "weight": 1}]
    first = MachineAgent(
        machine_config(tmp_path, observe=rules), facts=FACTS, load=FakeLoad(), clock=Clock(),
        processes=lambda: table_now,
    )  # fmt: skip
    first.tick()
    again = MachineAgent(
        machine_config(tmp_path), facts=FACTS, load=FakeLoad(), clock=Clock(),
        processes=dict,  # the scan sees nothing (hidepid), though the process runs
    )  # fmt: skip
    again.tick()
    assert observed(again) == []
