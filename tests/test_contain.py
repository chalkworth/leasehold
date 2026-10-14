"""Containment (`leasehold.contain`): when a command may be put in a systemd user scope, read
from fixture cgroup trees; the `systemd-run` prefix a lease gets; and, where this machine's user
manager can take scopes, that `hold` really starts its command in one weighted to the lease
and that a caller outside the user's session is left where it is."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from support import CHILD_ENV, until

from leasehold import contain

UID = 1234
SESSION = f"0::/user.slice/user-{UID}.slice/session-7.scope\n"


def tree(tmp_path: Path, *, own=SESSION, delegated="cpu memory pids", v2=True, bus=True):
    root = tmp_path / "cg"
    manager = root / f"user.slice/user-{UID}.slice/user@{UID}.service"
    manager.mkdir(parents=True)
    if v2:
        (root / "cgroup.controllers").write_text("cpu io memory pids\n")
    if delegated is not None:
        (manager / "cgroup.subtree_control").write_text(delegated + "\n")
    mine = tmp_path / "self_cgroup"
    mine.write_text(own)
    runtime = tmp_path / "run"
    if bus:
        (runtime / "systemd").mkdir(parents=True)
        (runtime / "systemd" / "private").touch()
    return {
        "uid": UID,
        "env": {"XDG_RUNTIME_DIR": str(runtime)},
        "cgroup_root": root,
        "self_cgroup": mine,
        "which": lambda name: f"/usr/bin/{name}",
        "answers": lambda: True,
    }


def test_a_caller_in_its_users_session_with_a_delegated_manager_is_contained(tmp_path):
    assert contain.probe(**tree(tmp_path)) == contain.Containment(True, "")
    under_manager = f"0::/user.slice/user-{UID}.slice/user@{UID}.service/app.slice/x.scope\n"
    assert contain.probe(**tree(tmp_path / "m", own=under_manager)).enforced


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"own": "0::/system.slice/actions.runner.service\n"}, "caller in /system.slice/"),
        ({"own": "0::/user.slice/user-99.slice/session-1.scope\n"}, "not a user session"),
        ({"delegated": "pids"}, "not delegated cpu and memory"),
        ({"delegated": "cpu pids"}, "not delegated memory"),
        ({"delegated": None}, "no systemd user manager"),
        ({"v2": False}, "no cgroup v2"),
        ({"bus": False}, "cannot be reached"),
    ],
)
def test_anywhere_else_it_is_advisory_and_says_why(tmp_path, change, reason):
    found = contain.probe(**tree(tmp_path, **change))
    assert not found.enforced
    assert reason in found.reason
    assert found.describe().startswith("advisory: ")


def test_the_environment_a_missing_systemd_run_and_a_silent_manager_turn_it_off(tmp_path):
    args = tree(tmp_path)
    off = contain.probe(**args | {"env": args["env"] | {contain.ENV: "off"}})
    assert (off.enforced, off.reason) == (False, "LEASEHOLD_CONTAIN=off")
    assert "systemd-run" in contain.probe(**args | {"which": lambda name: None}).reason
    assert "does not answer" in contain.probe(**args | {"answers": lambda: False}).reason


def test_a_lease_scope_is_weighted_to_its_slots_and_held_to_its_memory():
    argv = contain.lease_argv("cc.toolu-01/x", {}, weight=4, memory="2g")
    assert argv[:5] == ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
    assert "--unit=lease-cc.toolu_01_x.scope" in argv
    assert "--slice=leasehold.slice" in argv
    assert argv[argv.index("CPUWeight=400") - 1] == "-p"
    assert "MemoryHigh=2147483648" in argv
    assert not any(a.startswith(("CPUQuota", "MemoryMax")) for a in argv)
    assert argv[-1] == "--"


def test_a_hard_cpu_cap_is_only_set_when_asked_for():
    argv = contain.lease_argv("a", {}, weight=3, hard_cpu=True)
    assert "CPUQuota=300%" in argv


def test_the_slice_is_the_one_run_names_and_never_one_outside_leasehold():
    assert "--slice=leasehold-s1_ab.slice" in contain.lease_argv(
        "a", {contain.ENV_SLICE: "leasehold-s1_ab.slice"}, weight=1
    )
    for other in ("system.slice", "leasehold-a-b.slice", "leasehold-x.slice/../y.slice", ""):
        argv = contain.lease_argv("a", {contain.ENV_SLICE: other}, weight=1)
        assert "--slice=leasehold.slice" in argv


def test_a_session_slice_name_cannot_nest_escape_or_be_shared():
    assert contain.session_slice("cc-abc/def", "1234") == "leasehold-cc_abc_def_1234.slice"
    assert contain.session_slice("a-b", "t1") != contain.session_slice("a-b", "t2")
    for name in ("user.slice", contain.ROOT_SLICE, "leasehold-a-b.slice"):
        with pytest.raises(ValueError, match="not a session slice"):
            contain.stop_slice(name)


_ECHO = [sys.executable, "-c", "import os; print(os.getpid())"]


def test_spawn_runs_the_command_behind_a_prefix_that_hands_over():
    child, why = contain.spawn(_ECHO, ["env", "--"], stdout=subprocess.PIPE, text=True)
    out, _ = child.communicate(timeout=10)
    assert why is None and int(out) == child.pid  # the trampoline became the command


def test_a_scope_that_cannot_be_made_runs_the_command_uncontained_once(tmp_path):
    marker = tmp_path / "ran"
    argv = ["sh", "-c", f"echo x >> {marker}"]
    child, why = contain.spawn(argv, ["sh", "-c", "exit 3", "--"])
    assert child.wait(timeout=10) == 0
    assert why == "no systemd scope (systemd-run exited 3); running uncontained"
    assert marker.read_text() == "x\n"


def test_a_scope_that_never_answers_is_given_up_on_before_the_command_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(contain, "SCOPE_TIMEOUT_S", 0.3)
    marker = tmp_path / "ran"
    argv = ["sh", "-c", f"echo x >> {marker}"]
    child, why = contain.spawn(argv, ["sh", "-c", "sleep 30", "--"])
    assert child.wait(timeout=10) == 0
    assert why is not None and "running uncontained" in why
    assert marker.read_text() == "x\n"


def test_a_cancelled_wait_for_a_scope_kills_what_it_started_and_never_runs_the_command(tmp_path):
    marker, pidfile = tmp_path / "ran", tmp_path / "prefix.pid"
    argv = ["sh", "-c", f"echo x >> {marker}"]
    cancel, cancelled = os.pipe()

    def cancel_once_started():
        until(lambda: pidfile.exists() and pidfile.read_text().strip())
        os.write(cancelled, b"x")

    canceller = threading.Thread(target=cancel_once_started)
    canceller.start()
    prefix = [
        "sh",
        "-c",
        f"echo $$ > {pidfile}.new; mv {pidfile}.new {pidfile}; exec sleep 30",
        "--",
    ]
    started = time.monotonic()
    try:
        with pytest.raises(contain.Cancelled):
            contain.spawn(argv, prefix, cancel_fd=cancel)
    finally:
        canceller.join()
        os.close(cancel)
        os.close(cancelled)
    assert time.monotonic() - started < 10  # not SCOPE_TIMEOUT_S
    with pytest.raises(ProcessLookupError):  # killed and reaped
        os.kill(int(pidfile.read_text()), 0)
    assert not marker.exists()


live = pytest.mark.skipif(
    not contain.probe().enforced, reason=f"no user scopes here ({contain.probe().reason})"
)
_REPORT = (
    "import pathlib, sys\n"
    "group = open('/proc/self/cgroup').read().split('::')[1].strip()\n"
    "base = pathlib.Path('/sys/fs/cgroup' + group)\n"
    "print(group, (base / 'cpu.weight').read_text().strip(),"
    " (base / 'memory.high').read_text().strip())\n"
)


def _hold(sock: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "leasehold", "--socket", str(sock), "hold", *args]
    return subprocess.run(
        argv, env=env or CHILD_ENV, capture_output=True, text=True, timeout=30, check=False
    )


@live
def test_hold_starts_its_command_in_a_scope_sized_to_the_lease(daemon):
    done = _hold(
        daemon.path, "--weight", "2", "--memory", "512m", "--", sys.executable, "-c", _REPORT
    )
    assert done.returncode == 0, done.stderr
    group, weight, high = done.stdout.split()
    assert "/leasehold.slice/lease-" in group and group.endswith(".scope")
    assert (weight, high) == ("200", str(512 * 1024 * 1024))


@live
def test_hold_leaves_a_command_where_it_is_when_containment_is_off(daemon):
    off = CHILD_ENV | {contain.ENV: "off"}
    done = _hold(daemon.path, "--", sys.executable, "-c", _REPORT, env=off)
    assert done.returncode == 0, done.stderr
    assert "leasehold.slice" not in done.stdout


@live
def test_a_missing_program_is_reported_by_hold_not_by_systemd_run(daemon):
    done = _hold(daemon.path, "--", "no-such-program-here")
    assert done.returncode == 127
    assert "cannot run no-such-program-here" in done.stderr


@live
def test_stop_slice_ends_whats_left_and_removes_the_slice():
    name = contain.session_slice(f"t{os.getpid()}", "x")
    proc = subprocess.Popen(
        [*contain.scope_argv(f"agent-t{os.getpid()}.scope", name, weight=1), "sleep", "300"]
    )
    try:
        until(lambda: contain._populated(name, Path("/sys/fs/cgroup")))
        assert contain.stop_slice(name, grace_s=5) is True
        assert proc.wait(timeout=10) != 0
        state = subprocess.run(
            ["systemctl", "--user", "is-active", name], capture_output=True, text=True, check=False
        )
        assert state.stdout.strip() != "active"
    finally:
        proc.kill()
