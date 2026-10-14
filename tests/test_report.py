"""`leasehold report`: one POST of the daemon's own status, with the headers it is given and its
own User-Agent; a bearer token only from a token file that is not world-readable; https unless
the URL is loopback; and a failed POST never failing the run."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from leasehold.cli import main
from leasehold.exits import EXIT_UNAVAILABLE
from leasehold.report import ReportError, observe, parse_headers


class _Capture(BaseHTTPRequestHandler):
    """Records each POST's headers and body, and answers `server.answer`."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.seen.append((dict(self.headers), self.rfile.read(length)))
        self.send_response(self.server.answer)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


@pytest.fixture
def receiver():
    """A loopback HTTP server that records what is POSTed to it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    server.seen = []
    server.answer = 204
    server.url = f"http://127.0.0.1:{server.server_address[1]}/machines/report"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()


def _token(path: Path, mode: int, text: str = "test-token") -> Path:
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, mode)
    return path


def report(sock: Path, url: str, *args: str) -> int:
    return main(["--socket", str(sock), "report", "--url", url, "--once", *args])


def test_once_posts_the_daemon_status_with_a_bearer_token(daemon, receiver, tmp_path):
    token = _token(tmp_path / "report.token", 0o640)
    assert report(daemon.path, receiver.url, "--token-file", str(token)) == 0
    [(headers, body)] = receiver.seen
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"].startswith("leasehold/")
    posted = json.loads(body)
    assert posted["schema"] == 2
    assert posted["machine"]["name"] == "test-machine"


def test_without_a_token_file_no_authorization_is_sent(daemon, receiver):
    assert report(daemon.path, receiver.url) == 0
    [(headers, _body)] = receiver.seen
    assert "Authorization" not in headers
    assert headers["User-Agent"].startswith("leasehold/")


def test_header_values_are_sent_as_given(daemon, receiver):
    code = report(
        daemon.path, receiver.url, "--header", "X-Fleet: east", "--header", "User-Agent: probe/1"
    )
    assert code == 0
    [(headers, _body)] = receiver.seen
    assert headers["X-Fleet"] == "east"
    assert headers["User-Agent"] == "probe/1"


@pytest.mark.parametrize("header", ["Content-Type: text/plain", "content-length: 1"])
def test_the_body_headers_cannot_be_overridden(daemon, receiver, capsys, header):
    assert report(daemon.path, receiver.url, "--header", header) == 1
    assert "is set by the report itself" in capsys.readouterr().err
    assert receiver.seen == []


def test_a_malformed_header_is_refused():
    assert parse_headers(["A: b", "C:d"]) == {"A": "b", "C": "d"}
    for bad in ["no colon", ": no name", "two words: x"]:
        with pytest.raises(ReportError, match="is not 'Name: value'"):
            parse_headers([bad])


def test_a_world_readable_token_is_refused_before_any_request(receiver, tmp_path, sock_dir, capsys):
    token = _token(tmp_path / "report.token", 0o644)
    assert report(sock_dir / "missing.sock", receiver.url, "--token-file", str(token)) == 1
    assert "world-readable" in capsys.readouterr().err
    assert receiver.seen == []


def test_a_non_loopback_http_url_is_refused(sock_dir, capsys):
    assert report(sock_dir / "missing.sock", "http://example.com/machines/report") == 1
    assert "https" in capsys.readouterr().err


def test_a_failed_post_does_not_fail_once(daemon, receiver, caplog):
    receiver.answer = 500
    assert report(daemon.path, receiver.url) == 0
    assert any("500" in record.message for record in caplog.records)


def test_a_missing_daemon_on_once_is_unavailable(sock_dir):
    assert report(sock_dir / "missing.sock", "http://127.0.0.1/report") == EXIT_UNAVAILABLE


def test_a_repeated_failure_is_logged_once_until_it_changes():
    outcome, do_log = observe(None, "500")
    assert do_log is True
    outcome, do_log = observe(outcome, "500")
    assert do_log is False
    outcome, do_log = observe(outcome, "200")
    assert do_log is False
    _outcome, do_log = observe(outcome, "500")
    assert do_log is True


class _Redirect(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.hits += 1
        self.send_response(307)
        self.send_header("Location", self.server.target)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def test_a_redirect_is_never_followed(daemon, receiver, tmp_path):
    """Following it could carry the token to a URL the report never checked."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    server.hits, server.target = 0, receiver.url
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        token = _token(tmp_path / "t", 0o600)
        url = f"http://127.0.0.1:{server.server_address[1]}/r"
        assert report(daemon.path, url, "--token-file", str(token)) == 0
    finally:
        server.shutdown()
    assert server.hits == 1 and receiver.seen == []


@pytest.mark.parametrize("header", ["X(Y: 1", "X-Tag: café", "X-Tag: a\x07b"])
def test_headers_http_cannot_send_are_refused_up_front(header):
    with pytest.raises(ReportError):
        parse_headers([header])


def test_authorization_from_both_a_header_and_a_token_file_is_refused(daemon, tmp_path, capsys):
    token = _token(tmp_path / "t", 0o600)
    code = report(
        daemon.path, "http://127.0.0.1:9/x", "--token-file", str(token),
        "--header", "Authorization: Basic eA==",
    )  # fmt: skip
    assert code != 0 and "use one" in capsys.readouterr().err
