"""`leasehold run -- agent` (`leasehold.session`): the agent's environment names its session,
its slice and the shims; where this machine's user manager takes scopes, the agent runs in a
slice of its own whose leftovers are stopped when it exits, unless `--leave-running`."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from support import CHILD_ENV

from leasehold import contain, session


def test_the_agent_sees_its_session_its_slice_and_the_shims_first_on_path(tmp_path):
    shims = tmp_path / "shims"
    env = {"PATH": f"/usr/bin:{shims}:/bin", contain.ENV_SLICE: "leasehold-old.slice"}
    slice_ = "leasehold-claude_ab12.slice"
    out = session.session_env(env, "claude_ab12", slice_=slice_, shim_dir=shims)
    assert out[session.ENV_SESSION] == "claude_ab12"
    assert out[contain.ENV_SLICE] == "leasehold-claude_ab12.slice"
    assert out["PATH"] == f"{shims}:/usr/bin:/bin"


def test_without_containment_or_shims_nothing_names_a_slice_or_changes_path():
    env = {"PATH": "/usr/bin", contain.ENV_SLICE: "leasehold-old.slice"}
    out = session.session_env(env, "s", slice_=None, shim_dir=None)
    assert contain.ENV_SLICE not in out
    assert out["PATH"] == "/usr/bin"


def test_a_session_id_names_the_agent_and_is_safe_in_a_unit_name():
    sid = session.new_session(["/usr/local/bin/claude-code"])
    assert sid.startswith("claude_code_") and "-" not in sid


def _run(*args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "leasehold", "run", *args],
        env=env or CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_agents_exit_code_comes_through():
    done = _run("--quiet", "--", sys.executable, "-c", "import sys; sys.exit(5)")
    assert done.returncode == 5, done.stderr


live = pytest.mark.skipif(
    not contain.probe().enforced, reason=f"no user scopes here ({contain.probe().reason})"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


@live
def test_the_agent_runs_in_its_own_slice_and_what_it_leaves_is_stopped(tmp_path):
    pidfile = tmp_path / "pid"
    script = (
        "import os, subprocess\n"
        "print(open('/proc/self/cgroup').read().strip())\n"
        "q = subprocess.DEVNULL\n"
        "p = subprocess.Popen(['sleep', '300'], start_new_session=True, stdout=q, stderr=q)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    done = _run("--session", f"t{os.getpid()}", "--", sys.executable, "-c", script)
    assert done.returncode == 0, done.stderr
    assert re.search(
        rf"/leasehold-t{os.getpid()}_(\w+)\.slice/agent-t{os.getpid()}_\1\.scope", done.stdout
    )
    assert "stopped what the session left running" in done.stderr
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


@live
def test_leave_running_keeps_what_the_agent_started(tmp_path):
    pidfile = tmp_path / "pid"
    sid = f"k{os.getpid()}"
    script = (
        "import subprocess\n"
        "q = subprocess.DEVNULL\n"
        "p = subprocess.Popen(['sleep', '300'], start_new_session=True, stdout=q, stderr=q)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    done = _run("--leave-running", "--session", sid, "--", sys.executable, "-c", script)
    try:
        assert done.returncode == 0, done.stderr
        time.sleep(0.5)
        assert _alive(int(pidfile.read_text()))
    finally:
        group = Path(f"/proc/{pidfile.read_text()}/cgroup").read_text().split("/")
        contain.stop_slice(next(p for p in group if p.startswith(f"leasehold-{sid}_")), grace_s=2)


def test_ctrl_c_and_ctrl_backslash_reach_the_agent_not_run():
    script = (
        "import signal\n"
        "print(signal.getsignal(signal.SIGINT) is signal.default_int_handler,"
        " signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL)\n"
    )
    done = _run("--quiet", "--no-contain", "--", sys.executable, "-c", script)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["True", "True"]


def test_contain_mode_off_in_the_config_runs_the_agent_uncontained(tmp_path):
    config = tmp_path / "m.toml"
    config.write_text('[ceiling]\nslots = 1\n[contain]\nmode = "off"\n')
    env = CHILD_ENV | {"LEASEHOLD_CONFIG": str(config)}
    done = _run("--", sys.executable, "-c", "print(open('/proc/self/cgroup').read())", env=env)
    assert done.returncode == 0, done.stderr
    assert "advisory: [contain] mode is off" in done.stderr
    assert "leasehold-" not in done.stdout


def test_a_missing_agent_is_not_started():
    done = _run("--quiet", "--", "no-such-agent-here")
    assert done.returncode == 127
