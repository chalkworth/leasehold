"""The Claude Code hook's contract with the agent (`leasehold.claude_code`): what its JSON
output tells the agent and the person when a leased Bash command ends, against a real daemon."""

from __future__ import annotations

import threading

import pytest

from leasehold import busy, claude_code
from leasehold.client import Client


def no_renewer(_argv):
    raise OSError("no renewer in tests")


#: case: (the `error` PostToolUseFailure reports, OOM kills counted while the command ran,
#: Claude Code's own environment, what the agent is told after it; None: nothing).
ENDINGS = {
    "killed while the OOM killer ran": (
        "Exit code 137\n",
        1,
        {},
        "leasehold: pytest ended by SIGKILL while the kernel's OOM killer was ending processes; "
        "most likely it was killed for memory, not a test failure. Run it again only after the "
        "other heavy work has finished, with fewer workers.",
    ),
    "under Claude Code's memory limit": (
        "Exit code 137\n",
        2,
        {"CLAUDE_CODE_TOOL_MEMORY_LIMIT": "4G"},
        "leasehold: pytest was most likely killed by Claude Code's tool memory limit "
        "(CLAUDE_CODE_TOOL_MEMORY_LIMIT=4G, shared by every command of this session). This is "
        "not a test failure. Run it alone, or raise the limit.",
    ),
    "killed, no OOM kill": ("Exit code 137\n", 0, {}, None),
    "a failure while the OOM killer ran": ("Exit code 1\nFAILED test_x", 1, {}, None),
}


@pytest.mark.parametrize(("error", "kills", "env", "told"), ENDINGS.values(), ids=ENDINGS.keys())
def test_what_the_agent_is_told_when_a_command_ends(
    daemon, tmp_path, oom_kills, error, kills, env, told
):
    client = Client(daemon.path)
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "HOME": str(tmp_path), **env}
    event = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_1",
        "session_id": "s1",
        "tool_input": {"command": "pytest -q"},
    }
    claude_code.pre(event, client, env=env, watch_pid=None, spawn=no_renewer)
    assert len(client.leases()["leases"]) == 1
    oom_kills(kills)
    ended = event | {"hook_event_name": "PostToolUseFailure", "error": error}
    reply = claude_code.post(ended, client, env=env)
    assert client.leases()["leases"] == []
    if told is None:
        assert reply.output is None
        return
    assert reply.output["hookSpecificOutput"] == {
        "hookEventName": "PostToolUseFailure",
        "additionalContext": told,
    }
    assert "not a test failure" in reply.output["systemMessage"]


@pytest.mark.parametrize("waits", [False, True], ids=["granted at once", "granted after a wait"])
def test_what_the_agent_is_told_before_a_command_starts(daemon, tmp_path, monkeypatch, waits):
    monkeypatch.setattr(busy, "WAIT_NOTED_S", 0.2)
    client = Client(daemon.path)
    env = {"XDG_RUNTIME_DIR": str(tmp_path), "HOME": str(tmp_path)}
    event = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_2",
        "session_id": "s1",
        "tool_input": {"command": "pytest -q"},
    }
    if waits:
        held = client.acquire(
            kind="work", weight=2, ttl_s=30, wait_s=0, labels={"cmd": "cargo test"},
            session="other",
        )["lease"]["lease_id"]  # fmt: skip
        threading.Timer(0.6, client.release, [held]).start()
    reply = claude_code.pre(event, client, env=env, watch_pid=None, spawn=no_renewer)
    context = reply.output["hookSpecificOutput"]["additionalContext"]
    assert "granted 2 slots" in context and "2 jobs" in context
    if waits:
        assert "behind cargo test (session other)" in context
        assert "behind cargo test (session other)" in reply.output["systemMessage"]
    else:
        assert "systemMessage" not in reply.output
