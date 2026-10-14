"""`leasehold hold -- cmd` with real child processes against a daemon on a real socket: the
command's exit code comes through, the lease is held only while it runs and is described in the
command's environment, a revoked lend lease stops the whole process group (SIGKILL after the
grace for one that ignores SIGTERM), and a wait past `--timeout` exits 75 with its place in line
given back."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import termios
import textwrap
import time
from pathlib import Path

from support import CHILD_ENV, until

from leasehold import hold as hold_module
from leasehold.client import Client
from leasehold.exits import EXIT_BUSY, EXIT_REFUSED, EXIT_REVOKED, EXIT_UNAVAILABLE
from leasehold.hold import waiting_line


def hold(sock: Path, *args: str, wait: bool = True, **kw) -> subprocess.Popen:
    argv = [sys.executable, "-m", "leasehold", "--socket", str(sock), "hold", *args]
    proc = subprocess.Popen(argv, env=CHILD_ENV, stderr=subprocess.PIPE, text=True, **kw)
    if wait:
        proc.wait(timeout=30)
    return proc


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_commands_exit_code_comes_through_and_the_lease_is_held_only_while_it_runs(daemon):
    client = Client(daemon.path)
    script = (
        "import os, sys\n"
        "from leasehold.client import Client\n"
        f"held = Client({str(daemon.path)!r}).leases()['leases']\n"
        "assert [x['lease_id'] for x in held] == [os.environ['LEASEHOLD_LEASE_ID']], held\n"
        "assert held[0]['labels'] == {'job': 'unit'}\n"
        "assert held[0]['kind'] == 'work', held\n"
        "sys.exit(7)\n"
    )
    proc = hold(daemon.path, "--label", "job=unit", "--", sys.executable, "-c", script)
    assert proc.returncode == 7, proc.stderr.read()
    assert client.leases() == {"leases": [], "queue": []}
    assert hold(daemon.path, "--", "true").returncode == 0


def test_the_command_sees_its_lease_in_its_environment(daemon):
    script = (
        "import os, sys\n"
        "env = {k: v for k, v in os.environ.items() if k.startswith('LEASEHOLD_')}\n"
        "assert env['LEASEHOLD_WEIGHT'] == '2', env\n"
        "assert env['LEASEHOLD_POOL'] == 'work', env\n"
        "assert int(env['LEASEHOLD_GRANT']) >= 1, env\n"
        "assert env['LEASEHOLD_LEASE_ID'], env\n"
    )
    proc = hold(daemon.path, "--weight", "2", "--", sys.executable, "-c", script)
    assert proc.returncode == 0, proc.stderr.read()


def test_a_revoked_lend_lease_stops_the_whole_group(daemon, tmp_path):
    pidfile = tmp_path / "bg.pid"
    proc = hold(
        daemon.path,
        "--kind", "lend", "--ttl", "0.6", "--grace", "5",
        "--", "sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait",
        wait=False,
    )  # fmt: skip
    client = Client(daemon.path)
    until(lambda: pidfile.exists() and pidfile.read_text().strip())
    background = int(pidfile.read_text())
    assert [x["kind"] for x in client.leases()["leases"]] == ["lend"]
    # Work that does not fit beside the lent slot queues: lending is revoked at once.
    assert client.acquire(kind="work", weight=2, lease_id="w", ttl_s=30)["status"] == "queued"
    assert proc.wait(timeout=15) == EXIT_REVOKED, proc.stderr.read()
    until(lambda: not alive(background))
    assert client.acquire(kind="work", weight=2, lease_id="w")["status"] == "granted"


def test_a_command_ignoring_sigterm_is_killed_after_the_grace(daemon, tmp_path):
    marker = tmp_path / "started"
    script = (
        "import signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n"
        "time.sleep(60)\n"
    )
    proc = hold(
        daemon.path, "--kind", "lend", "--ttl", "0.6", "--grace", "0.5",
        "--", sys.executable, "-c", script, wait=False,
    )  # fmt: skip
    until(marker.exists)
    started = time.monotonic()
    Client(daemon.path).acquire(kind="work", weight=2, ttl_s=30)
    assert proc.wait(timeout=15) == EXIT_REVOKED
    assert time.monotonic() - started < 10


def test_waiting_past_the_timeout_exits_75_and_leaves_no_place_in_line(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, ttl_s=30)
    proc = hold(daemon.path, "--timeout", "0.5", "--", "true")
    assert proc.returncode == EXIT_BUSY
    assert "waiting for 1 slot (work): place 1; pool work holds 2 of 2" in proc.stderr.read()
    assert client.leases()["queue"] == []


def test_a_long_wait_says_it_is_still_waiting_and_for_how_long(daemon, monkeypatch):
    monkeypatch.setattr(hold_module, "_POLL_S", 0.1)
    monkeypatch.setattr(hold_module, "PROGRESS_S", 0.2)
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, ttl_s=30)
    said: list[str] = []
    request = {"kind": "work", "pool": None, "weight": 1, "lease_id": "waiter"}
    code, _ = hold_module.wait_for_grant(client, request, 1.0, said.append)
    assert code == EXIT_BUSY
    assert said[0].startswith("waiting for 1 slot (work): place 1; pool work holds 2 of 2")
    progress = [line for line in said if line.endswith("s so far")]
    assert progress and all(line.startswith(said[0] + "; ") for line in progress)
    assert len(progress) <= 5  # every PROGRESS_S, not every ask


def test_a_request_that_can_never_be_granted_and_a_missing_daemon(daemon, sock_dir):
    assert hold(daemon.path, "--weight", "9", "--", "true").returncode == EXIT_REFUSED
    gone = sock_dir / "absent.sock"
    assert hold(gone, "--if-unavailable", "fail", "--", "true").returncode == EXIT_UNAVAILABLE


SHOW = "echo lease=${LEASEHOLD_LEASE_ID:-none} jobs=${CARGO_BUILD_JOBS:-none}; exit 3"


def test_with_no_daemon_the_command_runs_without_a_lease_and_says_so(sock_dir):
    proc = hold(sock_dir / "absent.sock", "--", "sh", "-c", SHOW, stdout=subprocess.PIPE)
    assert proc.returncode == 3
    assert proc.stdout.read() == "lease=none jobs=none\n"
    assert proc.stderr.read() == ("leasehold hold: no daemon answers; running without a lease\n")


def test_if_unavailable_fail_from_the_environment_keeps_exit_69(sock_dir):
    env = CHILD_ENV | {"LEASEHOLD_IF_UNAVAILABLE": "fail"}
    argv = [sys.executable, "-m", "leasehold", "--socket", str(sock_dir / "absent.sock")]
    done = subprocess.run(
        [*argv, "hold", "--", "true"], env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == EXIT_UNAVAILABLE and "is a daemon running?" in done.stderr
    bad = subprocess.run(
        [*argv, "hold", "--if-unavailable", "maybe", "--", "true"], env=CHILD_ENV,
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert bad.returncode == 1 and "is not run or fail" in bad.stderr


def test_with_no_daemon_to_be_found_it_runs_too_unless_told_to_fail(tmp_path):
    argv = [sys.executable, "-m", "leasehold", "--config", str(tmp_path / "none.toml"), "hold"]
    run = subprocess.run(
        [*argv, "--", "sh", "-c", "exit 4"], env=CHILD_ENV, capture_output=True, text=True,
        timeout=30,
    )  # fmt: skip
    assert run.returncode == 4 and "running without a lease" in run.stderr
    fail = subprocess.run(
        [*argv, "--if-unavailable", "fail", "--", "true"], env=CHILD_ENV, capture_output=True,
        text=True, timeout=30,
    )  # fmt: skip
    assert fail.returncode == 1 and "cannot read the config" in fail.stderr


def test_the_waiting_line_says_what_is_waited_for_its_place_and_why():
    request = {"kind": "external", "pool": "ci", "weight": 2}
    assert waiting_line(request, {"position": 3, "blocked": "pool ci is closed"}) == (
        "waiting for 2 slots in pool ci (external): place 3; pool ci is closed"
    )
    assert waiting_line({"kind": "work", "pool": None}, {"position": 1, "blocked": None}) == (
        "waiting for 1 slot (work): place 1"
    )


def test_with_no_weight_hold_weighs_the_command_as_the_commands_rules_do(daemon, tmp_path):
    make = tmp_path / "make"
    make.write_text('#!/bin/sh\necho "$LEASEHOLD_WEIGHT"\n')
    make.chmod(0o755)
    other = tmp_path / "other"
    other.write_text(make.read_text())
    other.chmod(0o755)

    def weight(*args: str) -> str:
        proc = hold(daemon.path, *args, stdout=subprocess.PIPE)
        assert proc.returncode == 0, proc.stderr.read()
        return proc.stdout.read().strip()

    # make weighs 4 by the built-in rules, cut to the 2 the work pool may hold.
    assert weight("--", str(make), "-j") == "2"
    assert weight("--weight", "1", "--", str(make)) == "1"
    assert weight("--", str(other)) == "1"


def on_a_terminal(script: str) -> tuple[int, str]:
    """Run the Python `script` as a session leader on a new terminal (a pty), as a login shell
    runs; its exit code and everything written to the terminal."""
    master, slave = os.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        stdin=slave, stdout=slave, stderr=slave, env=CHILD_ENV, start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
    )  # fmt: skip
    os.close(slave)
    out = b""
    while True:
        try:
            chunk = os.read(master, 4096)
        except OSError:  # EIO: every writer has closed the terminal
            break
        if not chunk:
            break
        out += chunk
    os.close(master)
    return proc.wait(timeout=30), out.decode(errors="replace")


#: What the command under `hold` reports: whether it can open the terminal and has it.
PROBE = (
    "import os; fd = os.open('/dev/tty', os.O_RDWR); "
    "print('fg', os.tcgetpgrp(fd) == os.getpgrp(), os.getpgrp() == os.getpid())"
)


def test_from_a_terminal_the_command_gets_it_and_gives_it_back(daemon):
    code, out = on_a_terminal(f"""
        import os, subprocess, sys
        hold = [sys.executable, "-m", "leasehold", "--socket", {str(daemon.path)!r}, "hold"]
        done = subprocess.run([*hold, "--", sys.executable, "-c", {PROBE!r}])
        print("back", os.tcgetpgrp(0) == os.getpgrp(), done.returncode)
    """)
    assert code == 0, out
    assert "fg True True" in out and "back True 0" in out, out


def test_ctrl_z_stops_hold_with_the_command_and_fg_continues_both(daemon):
    stop_then_probe = "import os, signal; os.kill(0, signal.SIGTSTP); " + PROBE
    code, out = on_a_terminal(f"""
        import os, signal, subprocess, sys
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)  # a shell hands the terminal on
        hold = [sys.executable, "-m", "leasehold", "--socket", {str(daemon.path)!r}, "hold"]
        job = subprocess.Popen(
            [*hold, "--", sys.executable, "-c", {stop_then_probe!r}], process_group=0
        )
        os.tcsetpgrp(0, job.pid)
        _, status = os.waitpid(job.pid, os.WUNTRACED)
        print("stopped", os.WIFSTOPPED(status), os.tcgetpgrp(0) == job.pid)
        os.killpg(job.pid, signal.SIGCONT)  # fg
        print("exit", job.wait(timeout=20))
        os.tcsetpgrp(0, os.getpgrp())
    """)
    assert code == 0, out
    assert "stopped True True" in out and "fg True True" in out and "exit 0" in out, out
