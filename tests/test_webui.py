"""`leasehold ui` (leasehold.webui) and `leasehold metrics` (leasehold.metrics): the page, the
token the API routes need, the Host check, and the metrics text."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
from pathlib import Path

import pytest
from support import cli

from leasehold.client import Client
from leasehold.metrics import render
from leasehold.webui import UiServer


@pytest.fixture
def ui(daemon):
    server = UiServer(lambda: Client(daemon.path))
    server.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def get(server, path: str, host: str | None = None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host} if host else {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_the_page_is_served_with_a_strict_policy(ui):
    code, headers, body = get(ui, "/")
    assert code == 200 and b"<html" in body.lower()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "no-referrer"
    # Everything is inline: the page asks nothing of any other origin.
    assert not re.search(rb"""(src|href)=["']https?://""", body)


def test_the_api_routes_need_the_token(ui):
    for path in ("/api/status", "/api/events", "/metrics"):
        code, _, body = get(ui, path)
        assert code == 403 and json.loads(body)["error"] == "forbidden"
        code, _, _ = get(ui, f"{path}?token=wrong")
        assert code == 403


def test_with_the_token_the_daemon_is_read_through(ui, daemon):
    Client(daemon.path).acquire(kind="work", lease_id="seen", weight=1)
    code, _, body = get(ui, f"/api/status?token={ui.token}")
    status = json.loads(body)
    assert code == 200 and status["schema"] == 2
    assert [lease["lease_id"] for lease in status["leases"]] == ["seen"]
    code, _, body = get(ui, f"/api/events?since=0&token={ui.token}")
    assert code == 200 and any(e.get("lease_id") == "seen" for e in json.loads(body)["events"])
    code, headers, body = get(ui, f"/metrics?token={ui.token}")
    assert code == 200 and headers["Content-Type"].startswith("text/plain; version=0.0.4")
    assert b'leasehold_held_slots{machine="test-machine"} 1' in body


def test_a_request_for_another_host_is_refused(ui):
    """A page elsewhere that rebinds its own name to 127.0.0.1 still sends its own Host."""
    code, _, _ = get(ui, f"/api/status?token={ui.token}", host="evil.example:80")
    assert code == 421


def test_no_daemon_is_a_502_the_page_can_explain(sock_dir):
    server = UiServer(lambda: Client(sock_dir / "none.sock"))
    server.start()
    try:
        code, _, body = get(server, f"/api/status?token={server.token}")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 502 and json.loads(body)["error"] == "unavailable"


def test_the_printed_link_carries_the_token(ui):
    assert ui.url == f"http://127.0.0.1:{ui.server_address[1]}/?token={ui.token}"
    assert len(ui.token) >= 32


def test_metrics_are_valid_exposition_text(daemon):
    text = render(Client(daemon.path).status())
    names = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            names.add(line.split()[2])
            continue
        if line.startswith("#"):
            continue
        assert re.fullmatch(r"leasehold_[a-z_0-9]+\{[^}]*\} -?[0-9.e+-]+", line), line
    assert {"leasehold_slots", "leasehold_pool_max_slots", "leasehold_lend_open"} <= names
    assert 'leasehold_pool_max_slots{machine="test-machine",pool="work"} 2' in text


def test_metrics_textfile_is_written_whole(daemon, tmp_path):
    out = tmp_path / "textfiles"
    out.mkdir()
    result = cli(daemon.path, "metrics", "--textfile", str(out))
    assert result.returncode == 0, result.stderr
    assert (out / "leasehold.prom").read_text().startswith("# HELP leasehold_slots")
    assert [p.name for p in out.iterdir()] == ["leasehold.prom"]  # no partial file left


def test_the_browser_is_handed_a_private_file_not_the_token(monkeypatch):
    from leasehold import webui

    opened = []
    monkeypatch.setattr(webui.webbrowser, "open", opened.append)
    url = "http://127.0.0.1:1234/?token=secret-token"
    webui.open_browser(url, keep_s=0.5)
    [arg] = opened
    assert "secret-token" not in arg and arg.startswith("file://")
    page = Path(arg.removeprefix("file://"))
    assert oct(page.stat().st_mode & 0o777) == "0o600"
    assert oct(page.parent.stat().st_mode & 0o777) == "0o700"
    assert "secret-token" in page.read_text()
    deadline = time.monotonic() + 10
    while page.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not page.parent.exists()


def test_the_page_takes_the_token_out_of_its_address():
    from leasehold.webui import page

    text = page().decode()
    assert "sessionStorage.setItem('leasehold-token'" in text and "history.replaceState" in text


def test_metrics_textfile_is_readable_by_others_whatever_the_umask(daemon, tmp_path):
    out = tmp_path / "textfiles"
    out.mkdir()
    old = os.umask(0o077)
    try:
        result = cli(daemon.path, "metrics", "--textfile", str(out))
    finally:
        os.umask(old)
    assert result.returncode == 0, result.stderr
    assert oct((out / "leasehold.prom").stat().st_mode & 0o777) == "0o644"


def test_metrics_textfile_never_writes_through_a_planted_link(daemon, tmp_path):
    out = tmp_path / "textfiles"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    (out / "leasehold.prom").symlink_to(victim)  # replaced, not followed
    result = cli(daemon.path, "metrics", "--textfile", str(out))
    assert result.returncode == 0, result.stderr
    assert victim.read_text() == "keep me"
    assert not (out / "leasehold.prom").is_symlink()
