"""When the daemon says no, and when it cannot answer: config load, `doctor`, `GET
/v1/commands`, the Claude Code hook and the PATH shims agree. A rule no lease could ever be
granted for still matches its commands, which are refused (never run under a lighter rule's
lease); an error answer under 500 is a refusal; one of 500 or more, like no daemon, lets the
command run without a lease, saying why."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from support import FACTS, FakeLoad, machine_config
from test_claude_code import Spawned, event

from leasehold import claude_code, doctor, shims
from leasehold.access import Forbidden
from leasehold.agent import MachineAgent
from leasehold.client import Client
from leasehold.ledger import LedgerUnavailable
from leasehold.server import Daemon

ROOT = Path(__file__).resolve().parents[1]
G = 2**30
FAKE = '#!/bin/sh\necho "lease=${LEASEHOLD_LEASE_ID:-none} args=$*"\n'
FOUR = {
    "ceiling": {"slots": 4},
    "pools": {
        "work": {"reserved": 2, "max": 4, "priority": 0},
        "background": {"reserved": 0, "max": 2, "priority": 1},
    },
    "kinds": {"work": "work", "external": "background"},
    "lend": {"pools": [], "quiet_s": 0},
}
STANDING_7G = [{"name": "db", "pool": "background", "weight": 1, "memory": "7g"}]

#: name -> (config overrides, what the reason says)
NEVER = {
    # A config pytest rule declaring 6g beside 7g of standing memory in a 12 GiB budget: the
    # built-in pytest rule (weight 4, no memory) must not be used instead.
    "memory": (
        dict(
            FOUR,
            memory={},
            standing=STANDING_7G,
            commands={"rule": [{"programs": ["pytest"], "weight": 2, "memory": "6g"}]},
        ),
        "7.0 GiB is held by standing work",
    ),
    # More than one lease may be given (no [ceiling] memory: the machine's 16 GiB).
    "ceiling": (
        dict(
            FOUR,
            memory={},
            commands={"rule": [{"programs": ["pytest"], "weight": 2, "memory": "32g"}]},
        ),
        "memory 32g is more than the 16.0 GiB one lease may be given",
    ),
    # No `work` pool for the built-in rules, which name none.
    "no work pool": (
        {
            "ceiling": {"slots": 4},
            "pools": {"build": {"reserved": 2, "max": 4}},
            "kinds": {"external": "build"},
            "lend": {"pools": [], "quiet_s": 0},
        },
        "there is no pool 'work'",
    ),
}


@pytest.fixture(autouse=True)
def own_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def _start(tmp_path, sock_dir, **overrides):
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"), **overrides)
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    return config, daemon


def _shim(tmp_path, daemon, program="pytest") -> dict:
    """A shim for `program` before a fake real one; the environment that runs it."""
    real = tmp_path / "bin"
    real.mkdir(exist_ok=True)
    (real / program).write_text(FAKE)
    (real / program).chmod(0o755)
    shim_dir = tmp_path / "shims"
    shims.install(shim_dir, [program], python=sys.executable)
    return {
        "PATH": f"{shim_dir}:{real}:/usr/bin:/bin",
        "LEASEHOLD_SOCKET": str(daemon.path),
        "PYTHONPATH": str(ROOT / "src"),
        "HOME": str(tmp_path),
    }


def _run(env, program="pytest") -> subprocess.CompletedProcess:
    return subprocess.run(
        [program, "-x"], env=env, capture_output=True, text=True, timeout=30, check=False
    )


@pytest.mark.parametrize("case", list(NEVER))
def test_a_rule_never_granted_is_refused_everywhere_alike(tmp_path, sock_dir, case):
    overrides, said = NEVER[case]
    config, daemon = _start(tmp_path, sock_dir, **overrides)
    try:
        check = doctor.check_rules(config, 16 * G)
        assert check.level == "fail" and said in check.detail
        client = Client(daemon.path)
        [served] = [
            r
            for r in client.commands()["rules"]
            if "pytest" in r["programs"]
            and r["args"] is None  # the plain pytest rule, not the xdist one
            and r["source"] == ("default" if case == "no work pool" else "config")
        ]
        assert said in served["never"]
        reply = claude_code.pre(event("pytest -x"), client, env={}, watch_pid=None, spawn=Spawned())
        decision = reply.output["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert said in decision["permissionDecisionReason"]
        assert client.leases()["leases"] == []  # no lighter rule's lease either
        done = _run(_shim(tmp_path, daemon))
        assert done.returncode == 77 and done.stdout == ""
        assert said in done.stderr
        assert client.leases()["leases"] == []
    finally:
        daemon.stop()


def test_where_the_config_alone_shows_it_the_rule_fails_to_load(tmp_path):
    """A fixed [ceiling] memory, and a config rule naming no pool where there is no `work`
    pool, need no machine to check: the config does not load."""
    over_ceiling = dict(
        FOUR,
        ceiling={"slots": 4, "memory": "8g"},
        memory={},
        commands={"rule": [{"programs": ["pytest"], "weight": 2, "memory": "10g"}]},
    )
    with pytest.raises(
        ValueError, match=r"commands.rule\[0\]: memory 10g is more than \[ceiling\]"
    ):
        machine_config(tmp_path, **over_ceiling)
    no_pool = dict(
        NEVER["no work pool"][0], commands={"rule": [{"programs": ["make"], "weight": 1}]}
    )
    with pytest.raises(ValueError, match=r"commands.rule\[0\]: it names no pool"):
        machine_config(tmp_path, **no_pool)


def _failing(*_args, **_kwargs):
    raise LedgerUnavailable("disk full", problem="failing", path=Path("/x"))


def _forbidden(*_args, **_kwargs):
    raise Forbidden("not yours to ask for")


@pytest.mark.parametrize("where", ["acquire", "commands"])
@pytest.mark.parametrize(("fault", "denied"), [(_failing, False), (_forbidden, True)])
def test_the_hook_runs_on_a_daemon_fault_and_denies_on_an_answered_no(
    tmp_path, sock_dir, monkeypatch, where, fault, denied
):
    """503 `ledger_unavailable` (a full disk) is the daemon unable to answer: the command runs
    without a lease, and says why. 403 is the daemon saying no: denied, with its text. The
    same rule holds for listing the rules and for asking for the lease."""
    _, daemon = _start(tmp_path, sock_dir, **FOUR)
    try:
        monkeypatch.setattr(daemon.agent, where, fault)
        client = Client(daemon.path)
        reply = claude_code.pre(event("pytest"), client, env={}, watch_pid=None, spawn=Spawned())
        text = "forbidden" if denied else "ledger_unavailable"
        if denied:
            decision = reply.output["hookSpecificOutput"]
            assert decision["permissionDecision"] == "deny"
            assert text in decision["permissionDecisionReason"]
        else:
            message = reply.output["systemMessage"]
            assert text in message and "without a lease" in message
    finally:
        daemon.stop()


@pytest.mark.parametrize("where", ["acquire", "commands"])
@pytest.mark.parametrize(("fault", "denied"), [(_failing, False), (_forbidden, True)])
def test_a_shim_runs_on_a_daemon_fault_and_exits_77_on_an_answered_no(
    tmp_path, sock_dir, monkeypatch, where, fault, denied
):
    _, daemon = _start(tmp_path, sock_dir, **FOUR)
    try:
        monkeypatch.setattr(daemon.agent, where, fault)
        done = _run(_shim(tmp_path, daemon))
        if denied:
            assert done.returncode == 77 and done.stdout == ""
            assert "forbidden" in done.stderr
        else:
            assert done.returncode == 0 and "lease=none" in done.stdout
            assert "ledger_unavailable" in done.stderr and "without a lease" in done.stderr
    finally:
        daemon.stop()
