"""Claude Code's hooks (leasehold.claude_code, `leasehold hook claude-code`): a lease for each
heavy Bash command, released after it; never in an agent's way without reason."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from support import FACTS, FakeLoad, machine_config

from leasehold import claude_code, hooknotes
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.server import Daemon

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def own_tmp(tmp_path, monkeypatch):
    """Notes a hook writes with no `XDG_RUNTIME_DIR` go under this test's own temp directory."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def event(command: str, *, tool_use_id: str = "toolu_01ABC", tool: str = "Bash", **extra) -> dict:
    """A hook's stdin as Claude Code 2.1 sends it (fields as observed in a real session)."""
    return {
        "session_id": "4b1c-9e2f",
        "transcript_path": "/home/u/.claude/projects/p/4b1c-9e2f.jsonl",
        "cwd": "/home/u/p",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"command": command, "description": "run it"},
        "tool_use_id": tool_use_id,
        **extra,
    }


class Spawned(list):
    def __call__(self, argv):
        self.append(argv)
        return 4242


def leases(path) -> list[dict]:
    return Client(path).leases()["leases"]


def test_a_heavy_command_takes_a_lease_and_gives_it_back(daemon):
    client, spawned = Client(daemon.path), Spawned()
    reply = claude_code.pre(event("uv run pytest -x"), client, env={}, watch_pid=1, spawn=spawned)
    assert reply.output is None  # nothing to say: the command runs, under its lease
    [lease] = leases(daemon.path)
    assert lease["lease_id"] == "cc.toolu_01ABC" and lease["weight"] == 2
    assert lease["session"] == "cc-4b1c-9e2f"
    assert lease["labels"] == {"agent": "claude-code", "cmd": "uv run pytest -x"}
    [argv] = spawned
    assert argv[argv.index("keep") + 1] == "cc.toolu_01ABC" and "--watch-pid" in argv
    claude_code.post(event("uv run pytest -x"), client, env={})
    assert leases(daemon.path) == []


def test_a_command_that_waited_for_its_lease_says_how_long(daemon, monkeypatch):
    monkeypatch.setattr(claude_code, "WAIT_NOTED_S", 0.0)
    client = Client(daemon.path)
    reply = claude_code.pre(event("pytest"), client, env={}, watch_pid=None, spawn=Spawned())
    assert reply.output == {"systemMessage": "leasehold: pytest waited 0s in line for its lease"}
    assert [lease["lease_id"] for lease in leases(daemon.path)] == ["cc.toolu_01ABC"]


def test_a_subagents_lease_is_in_its_sessions_turn_and_names_it(daemon):
    client = Client(daemon.path)
    e = event("pytest", agent_id="ac9be8ee61ea46b24", agent_type="general-purpose")
    claude_code.pre(e, client, env={}, watch_pid=None, spawn=Spawned())
    [lease] = leases(daemon.path)
    assert lease["session"] == "cc-4b1c-9e2f" and lease["labels"]["subagent"] == "ac9be8ee61ea46b24"


def test_light_commands_other_tools_and_leased_agents_pass_untouched(daemon):
    client, spawned = Client(daemon.path), Spawned()
    for e, env in [
        (event("git status"), {}),
        (event("pytest", tool="Read"), {}),
        (event("pytest"), {"LEASEHOLD_LEASE_ID": "outer"}),
        ({"tool_name": "Bash", "tool_input": {"command": "pytest"}}, {}),  # no tool_use_id
        (event("pytest", tool_input={"command": "pytest", "run_in_background": True}), {}),
    ]:
        assert claude_code.pre(e, client, env=env, watch_pid=None, spawn=spawned).output is None
    assert spawned == [] and leases(daemon.path) == []


def test_no_daemon_lets_the_command_run(sock_dir):
    reply = claude_code.pre(
        event("pytest"), Client(sock_dir / "none.sock"), env={}, watch_pid=None, spawn=Spawned()
    )
    assert reply.output is None and "no lease taken" in reply.log[0]


@pytest.fixture
def busy(tmp_path, sock_dir, request):
    """A machine whose work pool someone else fills, with `[commands]` set by the test."""
    config = machine_config(
        tmp_path, socket=str(sock_dir / "s.sock"), commands=request.param, lease_ttl_s=30
    )
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        Client(d.path).acquire(kind="work", weight=2, lease_id="someone-else")
        yield d
    finally:
        d.stop()


@pytest.mark.parametrize("busy", [{"wait_s": 0.5, "when_busy": "run"}], indirect=True)
def test_a_busy_machine_runs_the_command_unleased_and_says_so(busy):
    reply = claude_code.pre(
        event("pytest"), Client(busy.path), env={}, watch_pid=None, spawn=Spawned()
    )
    message = reply.output["systemMessage"]
    assert message.startswith("leasehold: machine busy for 0.5s: work lease someone-else holds")
    assert message.endswith("queued #1; running pytest without a lease")
    assert [lease["lease_id"] for lease in leases(busy.path)] == ["someone-else"]
    assert Client(busy.path).leases()["queue"] == []  # its place in line given back


@pytest.mark.parametrize("busy", [{"wait_s": 0.5, "when_busy": "deny"}], indirect=True)
def test_a_busy_machine_can_refuse_the_command_with_a_reason_for_the_agent(busy):
    reply = claude_code.pre(
        event("pytest"), Client(busy.path), env={}, watch_pid=None, spawn=Spawned()
    )
    out = reply.output["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "leasehold: machine busy for 0.5s: work lease someone-else holds 2 slots; pool work "
        "holds 2 of 2; this needs 2; queued #1. The command did not run. Do lighter work first, "
        "then run it again."
    )


def test_ids_are_made_safe_and_short():
    e = event("x", tool_use_id="toolu/" + "z" * 100)
    lid = claude_code.lease_id(e)
    assert lid.startswith("cc.") and len(lid) == 64 and "/" not in lid
    assert claude_code.session({"session_id": 7}) is None


def test_the_agent_is_the_nearest_ancestor_named_claude(tmp_path):
    def fake(pid, ppid, comm):
        d = tmp_path / str(pid)
        d.mkdir()
        rest = " ".join(["S", str(ppid)] + ["0"] * 17 + ["5"] + ["0"] * 5)
        (d / "stat").write_text(f"{pid} ({comm}) {rest}\n")
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t1\t1\t1\t1\n")

    fake(1, 0, "systemd")
    fake(10, 1, "claude")
    fake(11, 10, "bash")
    fake(12, 11, "leasehold")
    assert claude_code.agent_pid(12, root=tmp_path) == 10
    assert claude_code.agent_pid(1, root=tmp_path) is None


def test_the_hook_command_always_exits_zero(daemon):
    run = [sys.executable, "-m", "leasehold", "--socket", str(daemon.path), "hook", "claude-code"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for stage, stdin in [("pre", "not json"), ("pre", json.dumps(event("git log"))),
                         ("post", json.dumps(event("pytest")))]:  # fmt: skip
        done = subprocess.run(
            [*run, stage], input=stdin, capture_output=True, text=True, env=env, timeout=30
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout == ""


def test_the_renewer_lives_at_most_the_calls_timeout_and_a_minute():
    assert (
        claude_code.max_hold(event("pytest", tool_input={"command": "x", "timeout": 30000})) == 90
    )
    assert claude_code.max_hold(event("pytest")) == 660
    assert claude_code.max_hold(event("x", tool_input={"command": "x", "timeout": 10**9})) == 660


def test_post_stops_the_renewer_even_when_the_daemon_is_gone(tmp_path, sock_dir):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    keeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        hooknotes.write(hooknotes.state_dir(env), "cc.toolu_01ABC", keeper.pid, None)
        claude_code.post(event("pytest"), Client(sock_dir / "none.sock"), env=env)
        assert keeper.wait(timeout=10) != 0  # stopped by a signal
    finally:
        keeper.kill()
    assert list((tmp_path / f"leasehold-hooks-{os.getuid()}").iterdir()) == []


def test_a_state_directory_others_can_write_is_not_used(tmp_path):
    shared = tmp_path / f"leasehold-hooks-{os.getuid()}"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    assert hooknotes.state_dir({"XDG_RUNTIME_DIR": str(tmp_path)}) is None


def test_a_lease_is_noted_for_post_or_not_taken(daemon, tmp_path):
    client = Client(daemon.path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    env = {"XDG_RUNTIME_DIR": str(run_dir)}
    claude_code.pre(event("pytest"), client, env=env, watch_pid=777, spawn=Spawned())
    notes = run_dir / f"leasehold-hooks-{os.getuid()}"
    renewer, _, agent, program = (notes / "cc.toolu_01ABC").read_text().split()
    assert (renewer, agent, program) == ("4242", "777", "pytest")  # what a shim looks for
    claude_code.post(event("pytest"), client, env=env)
    assert list(notes.iterdir()) == [] and leases(daemon.path) == []

    notes.chmod(0o777)  # not ours alone: no note can be trusted there, so no lease
    reply = claude_code.pre(event("pytest"), client, env=env, watch_pid=None, spawn=Spawned())
    assert leases(daemon.path) == [] and "no lease taken" in reply.log[-1]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
def test_a_lease_whose_note_cannot_be_written_is_given_back_and_its_renewer_stopped(
    daemon, tmp_path
):
    env = {"XDG_RUNTIME_DIR": str(tmp_path)}
    notes = tmp_path / f"leasehold-hooks-{os.getuid()}"
    notes.mkdir(mode=0o500)
    keeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        reply = claude_code.pre(
            event("pytest"), Client(daemon.path), env=env, watch_pid=None,
            spawn=lambda argv: keeper.pid,
        )  # fmt: skip
        assert keeper.wait(timeout=10) != 0  # stopped by a signal
    finally:
        keeper.kill()
        notes.chmod(0o700)
    assert leases(daemon.path) == [] and "no lease kept" in reply.log[-1]


def test_pre_keeps_the_prefilters_word_list_in_step(daemon, tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    claude_code.pre(event("git status"), Client(daemon.path), env=env, watch_pid=None)
    words = (tmp_path / "cache" / "leasehold" / "programs").read_text().split()
    assert "pytest" in words and "cargo" in words and words == sorted(words)


def test_a_refused_command_is_denied_not_run_without_a_lease(tmp_path, sock_dir):
    """A refusal means a working daemon said no (here: the user's submit rate in the pool), so
    the command is denied with the reason, even with `when_busy = "run"`."""
    config = machine_config(
        tmp_path,
        socket=str(sock_dir / "r.sock"),
        pools={"work": {"reserved": 2, "max": 2, "create_rate_per_min": 1}},
        kinds={"work": "work"},
        lend={"pools": []},
        commands={"when_busy": "run"},
    )
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        client = Client(d.path)
        client.acquire(kind="work", weight=1, lease_id="first")
        reply = claude_code.pre(event("pytest"), client, env={}, watch_pid=None, spawn=Spawned())
        decision = reply.output["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "pytest refused" in decision["permissionDecisionReason"]
        assert "per-user limit" in decision["permissionDecisionReason"]
        assert [lease["lease_id"] for lease in leases(d.path)] == ["first"]
    finally:
        d.stop()
