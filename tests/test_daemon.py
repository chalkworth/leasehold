"""The machine agent end to end over a real unix socket (leasehold.server, .api, .client, .cli):
the endpoints, a blocking acquire that is granted when capacity frees, the administrator
commands, the socket's mode, and a clean stop on SIGTERM."""

from __future__ import annotations

import json
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest
from support import CHILD_ENV, FACTS, FakeLoad, machine_config, until

import leasehold
from leasehold import ledger as ledger_module
from leasehold import server
from leasehold.agent import MachineAgent
from leasehold.cli import main
from leasehold.client import ApiError, Client, Unavailable
from leasehold.server import Daemon


def test_status_reports_the_machine_and_is_versioned(daemon):
    st = Client(daemon.path).status()
    assert st["schema"] == 2
    assert st["machine"] == {"name": "test-machine", "labels": {}}
    assert st["standing"] == []
    assert st["ceiling"]["slots"] == 2 and st["slots"] == 2
    assert set(st["pools"]) == {"work", "external", "lend", "observed"}
    assert st["pools"]["lend"]["preemptible"] is True
    assert st["lend"]["eligible"] is True  # idle, quiet_s = 0
    assert st["load"]["pressure"]["cpu"]["some"]["avg10"] == 0.0
    assert st["leases"] == [] and st["queue"] == []
    assert stat.S_IMODE(daemon.path.stat().st_mode) == 0o600  # a user daemon: yours alone


def test_status_text_prints_standing_work_and_json_is_the_object(tmp_path, sock_dir, capsys):
    sock = sock_dir / "r.sock"
    config = machine_config(
        tmp_path,
        name="guest",
        socket=str(sock),
        ceiling={"slots": 4},
        pools={
            "work": {"reserved": 1, "max": 4, "priority": 0},
            "external": {"reserved": 0, "max": 2, "priority": 1},
        },
        kinds={"work": "work", "external": "external"},
        lend={"pools": [], "quiet_s": 0},
        standing=[
            {
                "name": "gate",
                "kind": "vm",
                "pool": "work",
                "weight": 2,
                "cpus": 2.0,
                "memory": "3g",
                "note": "neighbour",
            },
            {"name": "db", "pool": "external", "weight": 1},
        ],
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        client = Client(sock)
        assert client.acquire(kind="work", weight=1, lease_id="w1")["status"] == "granted"
        st = client.status()
        assert [item["name"] for item in st["standing"]] == ["gate", "db"]
        work = st["pools"]["work"]
        assert work["held_weight"] == 3 and work["running"] == 1
        external = st["pools"]["external"]
        assert external["held_weight"] == 1 and external["running"] == 0
        assert st["held"] == 4

        assert main(["--socket", str(sock), "status"]) == 0
        text = capsys.readouterr().out
        assert f"machine guest  x86_64  4 cpus  leasehold {leasehold.__version__}\n" in text
        assert "slots 4/4 held;" in text
        assert "\nstanding:\n" in text
        assert "  gate         vm         work       weight 2  2 cpus  3.0 GiB  neighbour\n" in text
        assert "  db           workload   external   weight 1\n" in text

        assert main(["--socket", str(sock), "status", "--json"]) == 0
        body = json.loads(capsys.readouterr().out)
        assert body["standing"] == st["standing"]
        assert body["machine"] == st["machine"]
        assert body["pools"]["work"]["held_weight"] == 3
    finally:
        daemon.stop()


def test_what_other_pools_reservations_put_out_of_reach_is_refused(tmp_path, sock_dir, capsys):
    """4 slots, `work` reserving 2: `external` can never hold 3, whatever its configured max,
    so a weight-3 request is refused (not left at the head of the line for good), the next
    one is granted, and no administrator can raise the pool's max past 2."""
    sock = sock_dir / "r.sock"
    config = machine_config(
        tmp_path,
        socket=str(sock),
        ceiling={"slots": 4},
        pools={
            "work": {"reserved": 2, "max": 4, "priority": 0},
            "external": {"reserved": 0, "max": 4, "priority": 1},
        },
        kinds={"work": "work", "external": "external"},
        lend={"pools": [], "quiet_s": 0},
    )
    daemon = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    daemon.start()
    try:
        client = Client(sock)
        assert client.status()["pools"]["external"]["max"] == 2
        big = client.acquire(kind="external", weight=3, lease_id="big")
        assert big["status"] == "refused" and "max 2" in big["reason"]
        assert client.acquire(kind="external", weight=1, lease_id="e1")["status"] == "granted"
        assert client.status()["queue"] == []
        assert main(["--socket", str(sock), "pool", "set", "external", "--max", "3"]) == 1
        assert "could never be reached" in capsys.readouterr().err
    finally:
        daemon.stop()


def test_status_text_leaves_out_an_empty_standing_section(daemon, capsys):
    assert main(["--socket", str(daemon.path), "status"]) == 0
    assert "standing:" not in capsys.readouterr().out


def test_acquire_renew_list_and_release(daemon):
    client = Client(daemon.path)
    granted = client.acquire(kind="external", labels={"job": "lint"}, lease_id="g1")
    assert granted["status"] == "granted"
    lease = granted["lease"]
    assert (lease["lease_id"], lease["pool"], lease["labels"]) == (
        "g1",
        "external",
        {"job": "lint"},
    )
    assert lease["user"] is not None and lease["grant"] >= 1
    assert client.renew("g1")["status"] == "ok"
    assert [x["lease_id"] for x in client.leases()["leases"]] == ["g1"]
    queued = client.acquire(kind="external", lease_id="g2", session="t1")
    assert (queued["status"], queued["position"]) == ("queued", 1)
    assert queued["blocked"] == "pool external holds 1 of 1; this needs 1"
    [line] = client.leases()["queue"]
    assert (line["lease_id"], line["session"], line["blocked"]) == ("g2", "t1", queued["blocked"])
    assert client.renew("g2")["status"] == "queued"
    assert client.release("g1")["status"] == "released"
    assert client.renew("g1")["status"] == "unknown"
    assert client.acquire(kind="external", lease_id="g2")["status"] == "granted"


def test_a_blocking_acquire_is_granted_as_soon_as_capacity_frees(daemon):
    client = Client(daemon.path)
    assert client.acquire(kind="work", weight=2, lease_id="big")["status"] == "granted"
    answers = []
    waiter = threading.Thread(
        target=lambda: answers.append(client.acquire(kind="work", lease_id="next", wait_s=10))
    )
    started = time.monotonic()
    waiter.start()
    time.sleep(0.3)
    client.release("big")
    waiter.join(timeout=10)
    assert answers[0]["status"] == "granted"
    assert time.monotonic() - started < 5


def test_a_place_given_back_during_a_blocking_acquire_ends_it_and_stays_given_back(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, lease_id="big")
    answers = []
    waiter = threading.Thread(
        target=lambda: answers.append(client.acquire(kind="work", lease_id="next", wait_s=10))
    )
    started = time.monotonic()
    waiter.start()
    until(lambda: client.leases()["queue"])
    client.release("next")  # as a waiter interrupted while its request is still being served
    waiter.join(timeout=10)
    assert answers[0]["status"] == "refused" and "given back" in answers[0]["reason"]
    assert time.monotonic() - started < 5
    assert client.leases()["queue"] == []


def test_a_blocking_acquire_answers_queued_when_its_wait_is_up(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2)
    answer = client.acquire(kind="work", wait_s=0.3)
    assert answer["status"] == "queued"


def test_bad_requests_are_400_and_unknown_paths_404(daemon):
    client = Client(daemon.path)
    with pytest.raises(ApiError) as bad:
        client.acquire(kind="nonsense")
    assert bad.value.status == 400
    with pytest.raises(ApiError) as extra:
        client.request("POST", "/v1/renew", {"lease_id": "x", "surprise": 1})
    assert extra.value.status == 400
    with pytest.raises(ApiError) as missing:
        client.request("GET", "/v1/nope")
    assert missing.value.status == 404


def test_the_daemons_own_user_administers_it_from_the_cli(daemon, capsys):
    """The test runs as the daemon's user, whom `SO_PEERCRED` names: an administrator."""
    sock = str(daemon.path)
    assert main(["--socket", sock, "pool", "set", "work", "--max", "1"]) == 0
    assert capsys.readouterr().out == "pool work: max 1 (overridden)\n"
    assert Client(daemon.path).status()["pools"]["work"]["max"] == 1
    assert main(["--socket", sock, "pool", "set", "work", "--reset"]) == 0
    assert capsys.readouterr().out == "pool work: max 2 (configured)\n"
    assert main(["--socket", sock, "pool", "set", "work", "--max", "9"]) == 1
    assert "between 1 and the machine's 2 slots" in capsys.readouterr().err
    assert main(["--socket", sock, "pool", "set", "work", "--max", "0"]) == 1
    capsys.readouterr()
    assert main(["--socket", sock, "pool", "set", "work", "--max", "1"]) == 0
    capsys.readouterr()
    assert main(["--socket", sock, "lend", "off"]) == 0
    assert capsys.readouterr().out == "lending off: lending switched off\n"
    assert Client(daemon.path).status()["lend"]["switch"] is False
    assert main(["--socket", sock, "lend", "on"]) == 0
    assert capsys.readouterr().out.startswith("lending on: ")
    assert Client(daemon.path).set_pool_max("external", None)["overridden"] is False


def test_the_cli_says_its_version(capsys):
    with pytest.raises(SystemExit) as done:
        main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out == f"leasehold {leasehold.__version__}\n"


def test_no_daemon_is_unavailable(sock_dir):
    with pytest.raises(Unavailable):
        Client(sock_dir / "absent.sock").status()


def test_a_second_daemon_on_a_live_socket_is_refused_and_a_stale_one_is_cleared(daemon):
    with pytest.raises(RuntimeError, match="already listening"):
        Daemon(daemon.config, daemon.agent).start()
    daemon.stop()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(daemon.path))  # a socket file nobody answers on
    stale.close()
    again = Daemon(daemon.config, daemon.agent)
    again.start()
    try:
        assert Client(again.path).status()["schema"] == 2
    finally:
        again.stop()


def test_serve_stops_cleanly_on_sigterm(tmp_path, sock_dir):
    sock = sock_dir / "d.sock"
    config = tmp_path / "machine.toml"
    config.write_text(
        f'name = "buildbox"\nledger_dir = "{tmp_path}"\nsocket = "{sock}"\ntick_s = 0.1\n'
        "[ceiling]\nslots = 2\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "leasehold", "--config", str(config), "serve"],
        env=CHILD_ENV,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                status = Client(sock).status()
                break
            except Unavailable:
                time.sleep(0.05)
        else:
            pytest.fail("the daemon never answered")
        assert status["machine"]["name"] == "buildbox"
        assert Client(sock).acquire(kind="work", lease_id="kept")["status"] == "granted"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert "stopped" in proc.stderr.read()
        assert not sock.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_a_bad_session_in_the_environment_is_refused_by_the_cli(daemon, monkeypatch, capsys):
    monkeypatch.setenv("LEASEHOLD_SESSION", "no spaces allowed")
    with pytest.raises(SystemExit) as done:
        main(["--socket", str(daemon.path), "hold", "--", "true"])
    assert "LEASEHOLD_SESSION" in str(done.value)


def test_an_empty_session_in_the_environment_means_no_session(daemon, monkeypatch):
    monkeypatch.setenv("LEASEHOLD_SESSION", "")
    assert main(["--socket", str(daemon.path), "hold", "--", "true"]) == 0


def test_a_pool_name_is_sent_escaped(daemon):
    """`pool set 'work?x'` names no pool: the name is not cut at `?` or `/` on its way."""
    client = Client(daemon.path)
    for name in ("work?x", "work/", "work/../work"):
        with pytest.raises(ApiError) as caught:
            client.set_pool_max(name, 1)
        assert caught.value.status in (400, 404)
    assert client.status()["pools"]["work"]["max"] == 2


def test_an_unknown_pool_is_refused_not_a_dropped_connection(daemon):
    client = Client(daemon.path)
    answer = client.acquire(kind="work", pool="nope", lease_id="u1")
    assert answer == {"status": "refused", "reason": "there is no pool 'nope'"}
    assert client.leases() == {"leases": [], "queue": []}


def test_a_fault_in_the_daemon_is_a_500_and_it_keeps_serving(daemon, monkeypatch, capsys):
    def broken(*_a, **_k):
        raise KeyError("lost")

    monkeypatch.setattr(daemon.agent, "acquire_wait", broken)
    client = Client(daemon.path)
    with pytest.raises(ApiError) as failed:
        client.acquire(kind="work")
    assert (failed.value.status, failed.value.payload["error"]) == (500, "internal")
    assert failed.value.payload["detail"] == "KeyError: 'lost'"
    assert "Traceback" in capsys.readouterr().err
    assert client.status()["schema"] == 2


def test_a_ledger_that_fails_after_startup_is_503(daemon, monkeypatch):
    monkeypatch.setattr(ledger_module, "_BUSY_TIMEOUT_S", 0.1)
    other = sqlite3.connect(daemon.agent.ledger.path, isolation_level=None)
    try:
        other.execute("BEGIN EXCLUSIVE")  # holds the write lock past the busy timeout
        with pytest.raises(ApiError) as failed:
            Client(daemon.path).acquire(kind="work")
        other.execute("ROLLBACK")
    finally:
        other.close()
    assert (failed.value.status, failed.value.payload["error"]) == (503, "ledger_unavailable")
    assert "locked" in failed.value.payload["detail"]


def raw_request(path, request: bytes) -> tuple[int, dict]:
    """Send `request` as is over the unix socket at `path`, never closing the sending side; the
    answer's status and JSON body."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(path))
        conn.sendall(request)
        answer = b""
        while chunk := conn.recv(65536):
            answer += chunk
    head, _, body = answer.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


@pytest.mark.parametrize("length", ["-1", "abc", "1_0", "+5", ""])
def test_a_malformed_content_length_is_400_and_no_body_is_read(daemon, length):
    """Were `-1` read as a length, the read would wait for the end of a stream the client never
    ends, and this would time out."""
    request = f"POST /v1/acquire HTTP/1.0\r\nContent-Length: {length}\r\n\r\n".encode()
    status, answer = raw_request(daemon.path, request + b'{"kind": "work"}')
    assert (status, answer["error"]) == (400, "bad_request")
    assert Client(daemon.path).leases()["leases"] == []


def test_a_body_over_the_limit_is_413_unread(daemon):
    request = b"POST /v1/acquire HTTP/1.0\r\nContent-Length: 999999999\r\n\r\n{}"
    status, answer = raw_request(daemon.path, request)
    assert (status, answer["error"]) == (413, "too_large")


def test_a_stalled_client_is_cut_off_but_a_long_poll_is_not(daemon, monkeypatch):
    assert 0 < server._Handler.timeout <= 60
    monkeypatch.setattr(server._Handler, "timeout", 0.3)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stalled:
        stalled.settimeout(10)
        stalled.connect(str(daemon.path))
        stalled.sendall(b"POST /v1/acquire HTTP/1.0\r\nContent-Length: 50\r\n\r\n{")
        started = time.monotonic()
        assert stalled.recv(65536) == b""  # closed, unanswered
        assert time.monotonic() - started < 5
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2)
    started = time.monotonic()
    assert client.acquire(kind="work", wait_s=1.0)["status"] == "queued"
    assert time.monotonic() - started >= 1.0  # waited the whole second, past the read timeout
