"""`leasehold mcp`: the tool handlers against a real daemon, the lease keeper, and the server over
stdio."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
from support import CHILD_ENV, Clock, until

from leasehold import mcp_server as m
from leasehold.client import Client, Unavailable


def _leases(daemon, **kw) -> m.Leases:
    return m.Leases(Client(daemon.path), **kw)


def _held(daemon) -> dict[str, dict]:
    return {x["lease_id"]: x for x in Client(daemon.path).leases()["leases"]}


def _queued(daemon) -> dict[str, dict]:
    return {x["lease_id"]: x for x in Client(daemon.path).leases()["queue"]}


class _Spy(Client):
    """A real client that records the `wait_s` of each acquire."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.waits: list[float] = []

    def acquire(self, **body) -> dict:
        self.waits.append(body.get("wait_s"))
        return super().acquire(**body)


def test_a_lease_is_granted_with_its_jobs_and_labelled_as_the_agents(daemon):
    leases = _leases(daemon)
    answer = m.acquire_lease(leases, weight=2, reason="unit tests")
    lease_id = answer.data["lease_id"]
    assert answer.data["status"] == "granted"
    assert answer.data["jobs"] == 2
    assert lease_id.startswith("mcp.") and lease_id in answer.text
    assert "2 parallel jobs" in answer.text
    lease = _held(daemon)[lease_id]
    assert lease["labels"] == {"agent": "mcp", "reason": "unit tests"}
    assert lease["session"] == leases.session
    assert [x["lease_id"] for x in m.list_my_leases(leases).data["leases"]] == [lease_id]


def test_a_queued_request_is_granted_after_a_release(daemon):
    leases = _leases(daemon)
    first = m.acquire_lease(leases, weight=2).data["lease_id"]
    answer = m.acquire_lease(leases, weight=1, wait_s=0)
    second = answer.data["lease_id"]
    assert answer.data["status"] == "queued"
    assert answer.data["position"] == 1
    assert "wait_for_capacity" in answer.text
    assert second in _queued(daemon)

    assert m.wait_for_capacity(leases, second, wait_s=0).data["status"] == "queued"
    m.release_lease(leases, first)
    granted = m.wait_for_capacity(leases, second, wait_s=5)
    assert granted.data["status"] == "granted"
    assert set(_held(daemon)) == {second}
    with pytest.raises(m.ToolFailure, match="released"):
        m.wait_for_capacity(leases, first)


def test_a_lease_id_names_an_earlier_request_to_wait_on(daemon):
    leases = _leases(daemon)
    first = m.acquire_lease(leases, weight=2).data["lease_id"]
    second = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    Client(daemon.path).release(first)
    answer = m.acquire_lease(leases, weight=9, lease_id=second, wait_s=5)
    assert answer.data["status"] == "granted"
    assert _held(daemon)[second]["weight"] == 1  # asked again as first made


def test_waits_are_capped_and_made_of_short_asks(daemon, monkeypatch):
    monkeypatch.setattr(m, "MAX_WAIT_S", 2.5)
    spy = _Spy(daemon.path)
    leases = m.Leases(spy)
    m.acquire_lease(leases, weight=2)
    started = time.monotonic()
    queued = m.acquire_lease(leases, wait_s=1000).data
    assert queued["status"] == "queued"
    assert 2.5 <= time.monotonic() - started < 4.5
    assert max(spy.waits) <= m.ASK_S
    spy.waits.clear()
    m.wait_for_capacity(leases, queued["lease_id"], wait_s=-5)
    assert spy.waits == [0.0]


def test_a_daemon_that_never_answers_ends_the_call_within_its_wait(sock_dir):
    stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stalled.bind(str(sock_dir / "stalled.sock"))
    stalled.listen(8)  # connections are accepted by the kernel, and never answered
    try:
        leases = m.Leases(Client(sock_dir / "stalled.sock"))
        started = time.monotonic()
        with pytest.raises(m.ToolFailure, match="leasehold serve"):
            m.acquire_lease(leases, wait_s=20)
        assert time.monotonic() - started < 20 + m.ASK_MARGIN_S
    finally:
        stalled.close()


class _LostReply(Client):
    """A real client whose acquire reaches the daemon but whose answer never comes back."""

    def acquire(self, **body) -> dict:
        super().acquire(**body)
        raise Unavailable("the reply was lost")


def test_a_request_whose_answer_was_lost_is_given_back_by_the_renewer(daemon):
    Client(daemon.path).acquire(kind="work", weight=2, lease_id="blocker", ttl_s=60)
    leases = m.Leases(_LostReply(daemon.path))
    leases.start()
    try:
        with pytest.raises(m.ToolFailure, match="the reply was lost"):
            m.acquire_lease(leases, wait_s=0)
        assert leases.items() == []
        until(lambda: _queued(daemon) == {}, timeout=5)
    finally:
        leases.close()


class _Slow(Client):
    """A daemon that answers `queued` to every ask, just inside its socket timeout."""

    def __init__(self) -> None:
        super().__init__("/nonexistent")
        self.timeouts: list[float] = []

    def acquire(self, **body) -> dict:
        self.timeouts.append(self.timeout + body["wait_s"])
        time.sleep(self.timeouts[-1] - 0.05)
        return {"status": "queued", "lease_id": body["lease_id"], "position": 1}

    def release(self, lease_id: str) -> dict:
        return {"status": "released", "lease_id": lease_id}


def test_slow_answers_never_carry_a_call_past_its_wait_and_margin():
    slow = _Slow()
    leases = m.Leases(slow)
    started = time.monotonic()
    assert m.acquire_lease(leases, wait_s=2.5).data["status"] == "queued"
    assert time.monotonic() - started <= 2.5 + m.ASK_MARGIN_S + 0.2
    assert max(slow.timeouts) <= m.ASK_S + m.ASK_MARGIN_S


class _RevokedMeanwhile(Client):
    """A real client during whose acquire the renewer learns the lease is revoked."""

    leases: m.Leases

    def acquire(self, **body) -> dict:
        answer = super().acquire(**body)
        for held in self.leases.items():
            if held.lease_id == body["lease_id"]:
                held.revoked = "lending switched off"
        return answer


def test_a_revocation_seen_during_the_call_wins_over_its_grant(daemon):
    client = _RevokedMeanwhile(daemon.path)
    client.leases = leases = m.Leases(client)
    answer = m.acquire_lease(leases)
    assert answer.data["status"] == "revoked"
    assert "lending switched off" in answer.text and "Granted" not in answer.text


def test_ids_this_server_did_not_make_are_refused(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", lease_id="someone.else", ttl_s=30)
    leases = _leases(daemon)
    for call in (m.release_lease, m.wait_for_capacity):
        with pytest.raises(m.ToolFailure, match="not a lease this MCP server made"):
            call(leases, "someone.else")
    with pytest.raises(m.ToolFailure, match="not a lease"):
        m.acquire_lease(leases, lease_id="someone.else")
    assert "someone.else" in _held(daemon)


def test_a_refused_request_holds_nothing(daemon):
    leases = _leases(daemon)
    answer = m.acquire_lease(leases, kind="lend", pool="work")
    assert answer.data["status"] == "refused"
    assert "lend" in answer.data["reason"]
    assert leases.items() == []


def test_status_of_an_idle_machine_says_what_would_start(daemon):
    answer = m.machine_status(Client(daemon.path))
    assert answer.text.startswith("test-machine: 2 of 2 slots free; pools held/max: work 0/2")
    assert "Pressure (PSI avg10): cpu 0%, memory 0%, io 0%." in answer.text
    assert answer.text.endswith("2 slots free: a work lease of weight up to 2 would start now.")
    assert answer.data["free"] == 2
    assert answer.data["work_fits_now"] == 2
    assert answer.data["waiting"] == []
    assert answer.data["pressure"] == {"cpu": 0.0, "memory": 0.0, "io": 0.0}


def test_status_of_a_full_machine_names_who_waits_and_why(daemon):
    leases = _leases(daemon)
    m.acquire_lease(leases, weight=2, reason="full suite")
    waiting = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    daemon.agent.loader.set(cpu=40.0)
    daemon.agent.last_load = None
    answer = m.machine_status(Client(daemon.path))
    print(answer.text)
    assert "0 of 2 slots free" in answer.text
    assert "work 2/2 (1 queued)" in answer.text
    assert f"Waiting: {waiting} (weight 1 in work: " in answer.text
    assert "Lending is closed" in answer.text
    assert answer.text.endswith(
        "The machine is full and 1 is waiting; prefer light work, or queue with acquire_lease "
        "and do other work meanwhile. Pressure is high (cpu 40%): expect slow runs."
    )
    assert answer.data["waiting"][0]["lease_id"] == waiting
    assert answer.data["waiting"][0]["blocked"]
    assert answer.data["lend_open"] is False


def test_an_unreachable_daemon_is_a_tool_error_that_says_what_to_do(sock_dir):
    leases = m.Leases(Client(sock_dir / "nobody.sock"))
    for call in (lambda: m.machine_status(leases.client), lambda: m.acquire_lease(leases)):
        with pytest.raises(m.ToolFailure) as caught:
            call()
        assert "`leasehold serve`" in str(caught.value)
        assert "`leasehold doctor`" in str(caught.value)
    assert leases.items() == []


def test_the_keeper_renews_a_lease_past_its_ttl(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", lease_id="unkept", ttl_s=0.6)
    leases = _leases(daemon, ttl_s=0.6)
    leases.start()
    try:
        kept = m.acquire_lease(leases).data["lease_id"]
        time.sleep(2.0)
        held = _held(daemon)
        assert kept in held
        assert "unkept" not in held  # the TTL ends a lease nobody renews
    finally:
        leases.close()
    assert kept not in _held(daemon)


def test_close_during_a_wait_leaves_nothing_on_the_daemon(daemon):
    client = Client(daemon.path)
    client.acquire(kind="work", weight=2, lease_id="blocker", ttl_s=60)
    leases = _leases(daemon)
    waiting = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    outcome = []

    def wait() -> None:
        try:
            outcome.append(m.wait_for_capacity(leases, waiting, wait_s=15))
        except m.ToolFailure as exc:
            outcome.append(exc)

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.3)  # the waiter is in an ask, which a release wakes to ask again
    leases.close()
    waiter.join(timeout=5)
    time.sleep(0.3)
    assert waiting not in _queued(daemon)
    client.release("blocker")
    time.sleep(0.3)
    assert _held(daemon) == {} and _queued(daemon) == {}
    assert isinstance(outcome[0], m.ToolFailure)  # the wait ended in the shutdown


def test_a_request_abandoned_after_close_releases_itself(daemon):
    leases = _leases(daemon)
    queued = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    held = leases.touch(queued)
    leases.close()  # its sweep has run: no renewer is left to release what comes later
    Client(daemon.path).acquire(kind="work", weight=1, lease_id=queued, ttl_s=60)
    leases._items[queued] = held  # as an ask in flight during close would have it
    leases.abandon(held)
    assert queued not in _held(daemon) and queued not in _queued(daemon)


def test_close_releases_held_leases_and_places_in_line(daemon):
    leases = _leases(daemon)
    held = m.acquire_lease(leases, weight=2).data["lease_id"]
    queued = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    leases.close()
    assert _held(daemon) == {} and _queued(daemon) == {}
    with pytest.raises(m.ToolFailure, match="exited"):
        m.release_lease(leases, held)
    with pytest.raises(m.ToolFailure, match="shutting down"):
        m.acquire_lease(leases)
    assert queued not in _queued(daemon)


def test_a_place_is_kept_between_calls_and_a_grant_handed_over_on_the_next(daemon):
    clock = Clock()
    leases = _leases(daemon, clock=clock)
    first = m.acquire_lease(leases, weight=2).data["lease_id"]
    second = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    clock.now += leases.grace_s - 1
    leases.renew_all()
    assert second in _queued(daemon)
    Client(daemon.path).release(first)  # the daemon grants `second` with nobody asking
    until(lambda: second in _held(daemon))
    leases.renew_all()
    [mine] = [h for h in leases.items() if h.lease_id == second]
    assert (mine.state, mine.claimed) == ("granted", False)
    assert "granted while you were away" in m.list_my_leases(leases).text
    answer = m.wait_for_capacity(leases, second, wait_s=0)
    assert answer.data["status"] == "granted" and answer.data["jobs"] == 1
    clock.now += leases.grace_s * 2
    leases.renew_all()
    assert second in _held(daemon)  # a claimed lease is kept however long the agent works


def test_a_place_nobody_asks_about_is_given_back_when_the_grace_ends(daemon):
    clock = Clock()
    leases = _leases(daemon, clock=clock)
    m.acquire_lease(leases, weight=2)
    second = m.acquire_lease(leases, wait_s=0).data["lease_id"]
    clock.now += leases.grace_s - 0.5
    leases.renew_all()
    assert second in _queued(daemon)
    clock.now += 0.5
    leases.renew_all()
    assert second not in _queued(daemon)
    with pytest.raises(m.ToolFailure, match="given back"):
        m.wait_for_capacity(leases, second)


def test_the_renewer_gives_a_place_back_at_its_grace_not_its_next_renewal(daemon):
    leases = _leases(daemon, ttl_s=30, grace_s=0.5)  # renewals 10 s apart
    leases.start()
    try:
        m.acquire_lease(leases, weight=2)
        waiting = m.acquire_lease(leases, wait_s=0).data["lease_id"]
        started = time.monotonic()
        until(lambda: waiting not in _queued(daemon), timeout=5)
        assert time.monotonic() - started < 3
    finally:
        leases.close()


def _lend_lease(daemon, leases) -> str:
    """A lend lease of `leases`', revoked by work that then waits for its slot."""
    until(lambda: Client(daemon.path).status()["lend"]["eligible"])
    answer = m.acquire_lease(leases, kind="lend", wait_s=5)
    assert answer.data["status"] == "granted"
    queued = Client(daemon.path).acquire(kind="work", weight=2, lease_id="w", ttl_s=60)
    assert queued["status"] == "queued"
    return answer.data["lease_id"]


def test_a_revoked_lease_is_never_handed_back_as_granted(daemon):
    leases = _leases(daemon)
    lend = _lend_lease(daemon, leases)
    answer = m.wait_for_capacity(leases, lend, wait_s=0)
    assert answer.data["status"] == "revoked"
    assert "stop the work it was for now" in answer.text
    assert "Granted" not in answer.text
    assert "revoked" in m.list_my_leases(leases).text
    assert m.list_my_leases(leases).data["leases"][0]["state"] == "revoked"


def test_a_revocation_the_renewer_saw_is_answered_without_asking(daemon):
    spy = _Spy(daemon.path)
    leases = m.Leases(spy)
    lend = _lend_lease(daemon, leases)
    leases.renew_all()
    spy.waits.clear()
    answer = m.acquire_lease(leases, lease_id=lend)
    assert answer.data["status"] == "revoked" and spy.waits == []
    assert "stop the work" in m.list_my_leases(leases).text
    m.release_lease(leases, lend)
    assert lend not in _held(daemon)


def test_the_command_without_the_extra_says_how_to_install_it(monkeypatch, capsys, sock_dir):
    from leasehold import cli

    monkeypatch.setitem(sys.modules, "mcp", None)  # import mcp now raises ImportError
    code = cli.main(["--socket", str(sock_dir / "s.sock"), "mcp"])
    assert code == 2
    err = capsys.readouterr()
    assert "uv tool install 'leasehold[mcp]'" in err.err
    assert "uvx --from 'leasehold[mcp]' leasehold mcp" in err.err
    assert err.out == ""


def test_importing_the_server_module_does_not_import_the_sdk():
    probe = "import sys, leasehold.cli, leasehold.mcp_server; print('mcp' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", probe], env=CHILD_ENV, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


# --- over stdio ----------------------------------------------------------------------------

needs_mcp = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None, reason="the mcp extra is not installed"
)
TOOLS = {"machine_status", "acquire_lease", "wait_for_capacity", "release_lease", "list_my_leases"}


def _argv(daemon) -> list[str]:
    return [sys.executable, "-m", "leasehold", "--socket", str(daemon.path), "mcp"]


@needs_mcp
def test_the_server_answers_over_stdio_and_releases_at_eof(daemon):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    argv = _argv(daemon)
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=CHILD_ENV)

    async def session() -> tuple[set[str], str, dict]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            status = await client.call_tool("machine_status", {})
            granted = await client.call_tool("acquire_lease", {"weight": 1, "reason": "smoke"})
            assert not granted.is_error
            assert granted.structured_content["lease_id"] in _held(daemon)
            foreign = await client.call_tool("release_lease", {"lease_id": "someone.else"})
            assert foreign.is_error
            assert "not a lease this MCP server made" in foreign.content[0].text
            assert not (await client.call_tool("list_my_leases", {})).is_error
            return names, status.content[0].text, granted.structured_content

    names, status, granted = asyncio.run(session())
    assert names == TOOLS
    assert "2 of 2 slots free" in status
    until(lambda: granted["lease_id"] not in _held(daemon))  # stdin closed: released


@needs_mcp
def test_sigterm_releases_the_servers_leases(daemon):
    from mcp.types import LATEST_PROTOCOL_VERSION

    proc = subprocess.Popen(
        _argv(daemon),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=CHILD_ENV,
        text=True,
    )

    def send(message: dict) -> None:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0"} | message) + "\n")
        proc.stdin.flush()

    try:
        init = {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {}}
        init["clientInfo"] = {"name": "test", "version": "0"}
        send({"id": 1, "method": "initialize", "params": init})
        assert json.loads(proc.stdout.readline())["id"] == 1
        send({"method": "notifications/initialized"})
        call = {"name": "acquire_lease", "arguments": {"weight": 2}}
        send({"id": 2, "method": "tools/call", "params": call})
        answer = json.loads(proc.stdout.readline())
        lease_id = answer["result"]["structuredContent"]["lease_id"]
        assert lease_id in _held(daemon)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert lease_id not in _held(daemon)
    finally:
        proc.kill()
        proc.wait()
