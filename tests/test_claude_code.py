"""The Claude Code hook's contract with the agent (`leasehold.claude_code`): what its JSON
output tells the agent and the person when a leased Bash command ends, against a real daemon,
where the command runs and what it is measured to use, and what the built-in rules weigh the
commands it sees."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from support import NO_SCOPE

from leasehold import busy, claude_code, commands, contain
from leasehold.client import Client
from leasehold.commands import CommandSettings


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


#: What the built-in rules weigh a command line at the default weights; None: no lease. Help,
#: version, listings and cleaning take none; named tests weigh 1; any other arguments weigh what
#: the program's rule weighs.
WEIGHTS = {
    "pytest --version": None,
    "pytest --collect-only -q": None,
    "uv run pytest tests/test_x.py::test_y -x": 1,
    "pytest -k test_y": 1,
    "pytest -k 'not slow'": 4,
    "pytest --deselect tests/test_x.py::test_y": 4,
    "pytest -q": 4,
    "pytest -n 4 tests/test_x.py::test_y": 4,
    "cargo build --help": None,
    "cargo run -- --help": 4,
    "make clean": None,
    "make clean all": 4,
    "make -j8": 4,
    "ninja -t clean": None,
    "bazel info release": None,
    "bazelisk build //...": 4,
    "go test -run TestX ./pkg": 1,
    "go test -run TestX ./...": 4,
    "npm test -- --help": None,
    "npm run build -- --help": 4,
    "npm test": 4,
    "pnpm vitest": 4,
    "tsc --version": None,
    "tsc --noEmit": 4,
    "just test": 4,
    "npm install": 2,
    "git status": None,
}


def test_what_a_command_line_weighs():
    settings = CommandSettings()
    found = {line: commands.match(settings, line) for line in WEIGHTS}
    assert {line: m and m.rule.weight for line, m in found.items()} == WEIGHTS


#: A worker that the OOM killer ends: pytest goes on, and exits 1.
_WORKER = (
    f"{sys.executable} -c 'import subprocess, sys; "
    'subprocess.run([sys.executable, "-c", "b = bytearray(512 << 20)"])\'; exit 1'
)
#: case: (what the `pytest` the agent runs does, the memory limit on the scope Claude Code
#: runs the command's shell in, or on the slice the lease's scope goes in (None: none), whether
#: the shell is moved into the lease's scope, and the end of what the agent is told after it
#: ends; None: nothing).
ATTACH = {
    "in a login session": ("exec sleep 1.2", None, True, None),
    "under Claude Code's memory limit": ("exec sleep 0.2", ("scope", "1G"), False, None),
    "a worker killed under a limit above it": (
        _WORKER,
        ("slice", "64M"),
        True,
        "when the memory limit above it (64 MiB) ran out, and pytest exited 1. This is not a "
        "test failure. Run it with fewer workers, or raise that limit.",
    ),
}


@pytest.mark.skipif(NO_SCOPE, reason="needs a systemd user manager")
@pytest.mark.parametrize(("body", "limit", "moved", "told"), ATTACH.values(), ids=ATTACH.keys())
def test_a_leased_command_runs_in_its_lease_scope(
    daemon, tmp_path, monkeypatch, body, limit, moved, told
):
    # Claude Code's part is played here: the session's env file is run in the command's shell,
    # before `eval` of the command.
    real = Path(os.environ["XDG_RUNTIME_DIR"])
    (tmp_path / "systemd").symlink_to(real / "systemd")  # the user manager, for the probe
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))  # the notes, for the renewer too
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "pytest").write_text(f"#!/bin/sh\ncat /proc/self/cgroup\n{body}\n")
    (bin_ / "pytest").chmod(0o755)
    slice_ = contain.session_slice("attach", os.urandom(3).hex())
    if limit is not None and limit[0] == "slice":
        props = [f"MemoryMax={limit[1]}", "MemorySwapMax=0"]
        subprocess.run(["systemctl", "--user", "set-property", "--runtime", slice_, *props])
        monkeypatch.setenv(contain.ENV_SLICE, slice_)
    env = dict(os.environ, HOME=str(tmp_path), CLAUDE_ENV_FILE=str(tmp_path / "env.sh"))
    claude_code.start({"session_id": "s1"}, env)
    client = Client(daemon.path)
    event = {
        "tool_name": "Bash",
        "tool_use_id": "toolu_3",
        "session_id": "s1",
        "tool_input": {"command": "pytest -q"},
    }
    claude_code.pre(event, client, env=env, watch_pid=os.getpid())
    script = (tmp_path / "env.sh").read_text() + ": && eval 'pytest -q' < /dev/null"
    shell = ["bash", "-c", script]
    if limit is not None and limit[0] == "scope":
        outer = f"lh-cc-{os.urandom(3).hex()}.scope"
        shell = ["systemd-run", "--user", "--scope", "--quiet", f"--unit={outer}"]
        shell += ["-p", f"MemoryMax={limit[1]}", "bash", "-c", script]
    try:
        ran = subprocess.run(
            shell, env=dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}"),
            capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
        hook = "PostToolUse" if ran.returncode == 0 else "PostToolUseFailure"
        ended = event | {"hook_event_name": hook, "error": f"Exit code {ran.returncode}\n"}
        reply = claude_code.post(ended, client, env=env)
    finally:
        subprocess.run(["systemctl", "--user", "stop", slice_], check=False)
    assert ran.stdout.splitlines()[0].endswith("/lease-cc.toolu_3.scope") == moved
    outcomes = client.outcomes()["outcomes"]
    assert bool(outcomes) == moved
    if moved:
        assert outcomes[0]["peak_bytes"] > 0
        assert outcomes[0]["exit_status"] == ran.returncode
    context = (reply.output or {}).get("hookSpecificOutput", {}).get("additionalContext")
    if told is None:
        assert context is None
    else:
        assert context.startswith("leasehold: the kernel's OOM killer ended a process of pytest")
        assert told in context
