"""`leasehold hold -- cmd` with real child processes against a daemon on a real socket: the
command's exit code comes through, the lease is held only while it runs and is described in the
command's environment, a revoked lend lease stops the whole process group (SIGKILL after the
grace for one that ignores SIGTERM), a wait past `--timeout` exits 75 with its place in line
given back, an interrupt before the command runs gives back what was asked for, Ctrl-C on a
terminal ends `hold` by SIGINT, and renewals ride out a daemon (`Relay`) that does not answer."""

from __future__ import annotations

import fcntl
import http.server
import json
import os
import signal
import socketserver
import subprocess
import sys
import termios
import textwrap
import threading
import time
from pathlib import Path

import pytest
from support import CHILD_ENV, until

from leasehold import hold as hold_module
from leasehold import jobs
from leasehold.client import ApiError, Client
from leasehold.exits import EXIT_BUSY, EXIT_REFUSED, EXIT_REVOKED, EXIT_UNAVAILABLE
from leasehold.hold import waiting_line


def hold(sock: Path, *args: str, wait: bool = True, **kw) -> subprocess.Popen:
    argv = [sys.executable, "-m", "leasehold", "--socket", str(sock), "hold", *args]
    kw.setdefault("env", CHILD_ENV)
    proc = subprocess.Popen(argv, stderr=subprocess.PIPE, text=True, **kw)
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


def test_an_unknown_pool_exits_77_without_running_the_command(daemon, tmp_path):
    ran = tmp_path / "ran"
    proc = hold(daemon.path, "--pool", "nope", "--", "touch", str(ran))
    assert proc.returncode == EXIT_REFUSED
    assert "refused: there is no pool 'nope'" in proc.stderr.read()
    assert not ran.exists()


def test_a_daemon_that_fails_on_the_request_exits_77_and_runs_nothing(
    daemon, tmp_path, monkeypatch
):
    """A 500 is an answer from a daemon, not its absence: the command never runs unleased."""

    def broken(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr(daemon.agent, "acquire_wait", broken)
    ran = tmp_path / "ran"
    proc = hold(daemon.path, "--", "touch", str(ran))
    assert proc.returncode == EXIT_REFUSED
    assert "500: internal: RuntimeError: bug" in proc.stderr.read()
    assert not ran.exists()


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


def test_thread_pools_get_the_rules_threads_or_the_flags_and_worker_counts_the_jobs(
    daemon, tmp_path
):
    """The built-in pytest rule gives each worker one thread; `--threads` overrides it; a
    variable already set is left alone."""
    probe = '#!/bin/sh\necho "$PYTEST_XDIST_AUTO_NUM_WORKERS $OMP_NUM_THREADS $LEASEHOLD_THREADS"\n'
    for name in ("pytest", "other"):
        (tmp_path / name).write_text(probe)
        (tmp_path / name).chmod(0o755)
    env = {k: v for k, v in CHILD_ENV.items() if k not in jobs.JOB_VARIABLES}

    def sizes(*args: str, **extra: str) -> str:
        proc = hold(daemon.path, *args, stdout=subprocess.PIPE, env=env | extra)
        assert proc.returncode == 0, proc.stderr.read()
        return proc.stdout.read().strip()

    pytest_, other = str(tmp_path / "pytest"), str(tmp_path / "other")
    assert sizes("--", pytest_, "-n", "auto") == "2 1 1"  # weight 4, cut to 2 jobs
    assert sizes("--weight", "2", "--", pytest_) == "2 1 1"  # the rule's threads still
    assert sizes("--threads", "jobs", "--", pytest_) == "2 2 2"
    assert sizes("--weight", "2", "--", other) == "2 2 2"  # no rule: the job count
    assert sizes("--weight", "2", "--threads", "1", "--", other) == "2 1 1"
    assert sizes("--", pytest_, OMP_NUM_THREADS="6") == "2 6 1"  # the user's own wins
    bad = hold(daemon.path, "--threads", "0", "--", other)
    assert bad.returncode == 2 and "count of at least 1" in bad.stderr.read()


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


def no_traceback(proc: subprocess.Popen) -> str:
    err = proc.stderr.read()
    assert "Traceback" not in err, err
    return err


@pytest.mark.parametrize(("sig", "ends"), [(signal.SIGINT, -signal.SIGINT), (signal.SIGTERM, 143)])
def test_an_interrupt_while_waiting_gives_the_place_in_line_back(daemon, sig, ends):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, ttl_s=30)
    proc = hold(daemon.path, "--", "true", wait=False)
    until(lambda: client.leases()["queue"])
    proc.send_signal(sig)
    assert proc.wait(timeout=15) == ends  # SIGINT ends it by SIGINT, so a shell loop stops
    assert "interrupted" in no_traceback(proc)
    assert client.leases()["queue"] == []


def test_ctrl_c_while_acquire_waits_gives_the_place_back_without_a_traceback(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, ttl_s=30)
    argv = [sys.executable, "-m", "leasehold", "--socket", str(daemon.path), "acquire"]
    proc = subprocess.Popen(
        [*argv, "--lease-id", "a"], env=CHILD_ENV, stderr=subprocess.PIPE, text=True
    )
    until(lambda: client.leases()["queue"])
    proc.send_signal(signal.SIGINT)
    assert proc.wait(timeout=15) == -signal.SIGINT
    no_traceback(proc)
    assert client.leases()["queue"] == []


#: Runs `hold` with a scope prefix that never answers (its pid in `argv[2]`), so an interrupt
#: arrives while the command is being started; the command would make `argv[3]`.
STARTING = """
import sys
from leasehold import hold
from leasehold.client import Client
sock, pidfile, ran = sys.argv[1:4]
started = f"echo $$ > {pidfile}.new; mv {pidfile}.new {pidfile}; exec sleep 30"
hold.scope_prefix = lambda *_: ["sh", "-c", started, "--"]
sys.exit(hold.hold(Client(sock), ["sh", "-c", f"touch {ran}"], weight=1))
"""


@pytest.mark.parametrize(("sig", "ends"), [(signal.SIGINT, -signal.SIGINT), (signal.SIGTERM, 143)])
def test_an_interrupt_while_the_command_starts_stops_it_and_gives_the_lease_back(
    daemon, tmp_path, sig, ends
):
    pidfile, ran = tmp_path / "prefix.pid", tmp_path / "ran"
    proc = subprocess.Popen(
        [sys.executable, "-c", STARTING, str(daemon.path), str(pidfile), str(ran)],
        env=CHILD_ENV, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    until(pidfile.exists)
    started = time.monotonic()
    proc.send_signal(sig)
    assert proc.wait(timeout=15) == ends
    assert time.monotonic() - started < 10  # not the 30 s a scope is waited for
    assert "interrupted" in no_traceback(proc)
    assert not alive(int(pidfile.read_text()))
    assert not ran.exists()
    assert Client(daemon.path).leases() == {"leases": [], "queue": []}


DIES_OF_SIGINT = "import os, signal; signal.signal(signal.SIGINT, signal.SIG_DFL); os.kill(0, 2)"


def test_without_a_terminal_a_command_killed_by_sigint_exits_130(daemon):
    proc = hold(daemon.path, "--", sys.executable, "-c", DIES_OF_SIGINT)
    assert proc.returncode == 130, proc.stderr.read()


def test_ctrl_c_on_a_terminal_ends_hold_by_sigint_once_the_lease_is_given_back(daemon, tmp_path):
    ready = tmp_path / "ready"
    sleeper = f"import pathlib, time; pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    code, out = on_a_terminal(f"""
        import os, pathlib, signal, subprocess, sys, time
        hold = [sys.executable, "-m", "leasehold", "--socket", {str(daemon.path)!r}, "hold"]
        job = subprocess.Popen([*hold, "--", sys.executable, "-c", {sleeper!r}])
        deadline = time.monotonic() + 20
        while not pathlib.Path({str(ready)!r}).exists() or os.tcgetpgrp(0) == os.getpgrp():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.killpg(os.tcgetpgrp(0), signal.SIGINT)  # Ctrl-C: to the terminal's foreground group
        print("exit", job.wait(timeout=20), "back", os.tcgetpgrp(0) == os.getpgrp())
    """)
    assert code == 0, out
    assert f"exit {-signal.SIGINT} back True" in out, out
    assert Client(daemon.path).leases() == {"leases": [], "queue": []}


class Relay:
    """A daemon's stand-in on `path`: each request goes on to the daemon at `target`, unless
    `answer(path, body)` gives an answer of its own (a dict), or `STALL` to give none. The paths
    asked for, in order, are in `asked`."""

    STALL = "stall"

    def __init__(self, path: Path, target: Path, answer) -> None:
        relay = self
        self.path = path
        self.answer = answer
        self.asked: list[str] = []
        self.released = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                relay.asked.append(self.path)
                answer = relay.answer(self.path, body)
                if answer == Relay.STALL:
                    relay.released.wait(30)
                    return
                status = 200
                if answer is None:
                    try:
                        answer = Client(target).request(self.command, self.path, body)
                    except ApiError as exc:
                        status, answer = exc.status, exc.payload
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

        self.server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, path: str) -> int:
        return self.asked.count(path)

    def close(self) -> None:
        self.released.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay(daemon, sock_dir):
    """`relay(answer)`: a `Relay` to `daemon` on a socket of its own, closed afterwards."""
    made: list[Relay] = []

    def make(answer) -> Relay:
        made.append(Relay(sock_dir / f"relay{len(made)}.sock", daemon.path, answer))
        return made[-1]

    yield make
    for r in made:
        r.close()


def test_a_renewal_left_unanswered_does_not_cost_a_lend_lease(relay):
    def stall_the_first_renewal(path, _body):
        return Relay.STALL if path == "/v1/renew" and r.count(path) == 1 else None

    r = relay(stall_the_first_renewal)
    proc = hold(r.path, "--kind", "lend", "--ttl", "3", "--", "sleep", "4")
    assert proc.returncode == 0, proc.stderr.read()
    assert r.count("/v1/renew") >= 2, r.asked


def test_a_stopping_group_renews_its_lease_every_ttl_third_not_faster(relay):
    r = relay(lambda _path, _body: None)
    leftover = "trap '' TERM; sleep 30 & exit 0"  # what is left ignores SIGTERM: the full grace
    proc = hold(r.path, "--ttl", "3", "--grace", "2", "--", "sh", "-c", leftover)
    assert proc.returncode == 0, proc.stderr.read()
    assert r.count("/v1/renew") <= 4, r.asked


@pytest.mark.parametrize(
    ("again", "said"),
    [
        ({"status": "refused", "reason": "too heavy now"}, "now refuses it (too heavy now)"),
        ({"status": "queued", "position": 1}, "waiting in line (place 1)"),
    ],
)
def test_a_lost_work_lease_asked_for_again_says_the_answer_once(relay, again, said):
    def lost(path, _body):
        if path == "/v1/renew":
            return {"status": "unknown"}
        if path == "/v1/acquire" and r.count(path) > 1:
            return again
        return None

    r = relay(lost)
    proc = hold(r.path, "--ttl", "0.6", "--", "sleep", "2")
    err = proc.stderr.read()
    assert proc.returncode == 0, err
    assert err.count(said) == 1, err
    asked_again = r.count("/v1/acquire") - 1
    if again["status"] == "refused":
        assert asked_again == 1 and r.count("/v1/renew") == 1, r.asked  # not asked again
    else:
        assert asked_again >= 3, r.asked  # each renewal asks again, keeping the place
