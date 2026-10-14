"""`leasehold top`: the pure render against fixed status objects (golden files under
`tests/golden/`; `LEASEHOLD_UPDATE_GOLDEN=1` rewrites them), its fit to any size, and
`top --once` against a real daemon."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
from pathlib import Path

import pytest
from support import cli

from leasehold import top
from leasehold.cli import Context, watch
from leasehold.client import Client, Unavailable
from leasehold.exits import EXIT_UNAVAILABLE

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "top"
GOLDEN = HERE / "golden"
UPDATE = os.environ.get("LEASEHOLD_UPDATE_GOLDEN") == "1"

#: Thirty polls of pressure, the same every run.
HISTORY = {
    "cpu": [10 + i for i in range(30)],
    "memory": [1.5 + (i % 3) * 0.3 for i in range(30)],
    "io": [4 + (i % 5) for i in range(30)],
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def busy() -> dict:
    return _load("status-busy.json")


@pytest.fixture
def idle() -> dict:
    return _load("status-idle.json")


@pytest.fixture
def events() -> list[dict]:
    return _load("events-busy.json")["events"]


def _styled(lines: list[top.Line]) -> str:
    """The lines with each run marked `[style]` (`[]` for plain), for golden files."""
    out = []
    for line in lines:
        out.append("".join(f"[{style}]{text}" for text, style in line).rstrip())
    return "\n".join(out) + "\n"


def _text(lines: list[top.Line]) -> str:
    return "\n".join("".join(t for t, _ in line).rstrip() for line in lines) + "\n"


def _check_golden(name: str, lines: list[top.Line]) -> None:
    for suffix, content in ((".txt", _text(lines)), (".styles", _styled(lines))):
        path = GOLDEN / f"{name}{suffix}"
        if UPDATE:
            path.parent.mkdir(exist_ok=True)
            path.write_text(content)
        assert path.exists(), f"no golden {path.name}: run with LEASEHOLD_UPDATE_GOLDEN=1"
        assert content == path.read_text(), f"{path.name} differs; LEASEHOLD_UPDATE_GOLDEN=1"


def _fits(lines: list[top.Line], width: int, height: int) -> None:
    assert len(lines) == height
    for line in lines:
        assert sum(top.cells(text) for text, _ in line) == width, line


def _sections(lines: list[top.Line]) -> list[str]:
    names = ("PRESSURE", "SLOTS", "POOLS", "LEASES", "QUEUE", "LENDING", "EVENTS")
    found = []
    for line in lines:
        text = "".join(t for t, _ in line)
        found += [n for n in names if text.startswith(n)]
    return found


# --- golden renders ------------------------------------------------------------------------------


@pytest.mark.parametrize("width,height", [(100, 40), (80, 24)])
def test_the_busy_machine_renders_as_its_golden_file(busy, events, width, height):
    lines = top.render(busy, events, width, height, history=HISTORY, now=busy["time"] + 1)
    _fits(lines, width, height)
    _check_golden(f"busy-{width}x{height}", lines)


def test_the_idle_machine_renders_as_its_golden_file(idle):
    lines = top.render(idle, [], 100, 40, history=HISTORY, now=idle["time"])
    _fits(lines, 100, 40)
    _check_golden("idle-100x40", lines)


def test_the_ascii_render_is_its_golden_file_and_only_ascii(busy, events):
    lines = top.render(busy, events, 100, 40, history=HISTORY, now=busy["time"], unicode=False)
    _fits(lines, 100, 40)
    assert top.plain(lines).isascii()
    _check_golden("busy-100x40-ascii", lines)


# --- fitting the terminal ------------------------------------------------------------------------


@pytest.mark.parametrize("width", [1, 7, 20, 41, 79, 80, 100, 137, 220])
@pytest.mark.parametrize("height", [1, 2, 3, 5, 9, 14, 20, 24, 40, 70])
def test_every_line_is_exactly_the_width_and_there_are_exactly_height_lines(
    busy, idle, events, width, height
):
    for status, evs in ((busy, events), (idle, [])):
        _fits(top.render(status, evs, width, height, history=HISTORY), width, height)


def test_short_heights_drop_whole_sections_least_important_first(busy, events):
    full = _sections(top.render(busy, events, 100, 40))
    assert full == ["PRESSURE", "SLOTS", "POOLS", "LEASES", "QUEUE", "LENDING", "EVENTS"]
    assert _sections(top.render(busy, events, 100, 1)) == []
    assert "buildbox" in top.plain(top.render(busy, events, 100, 1))
    assert _sections(top.render(busy, events, 100, 4)) == ["SLOTS"]
    assert _sections(top.render(busy, events, 100, 12)) == ["SLOTS", "LEASES", "QUEUE"]
    assert _sections(top.render(busy, events, 80, 20)) == [
        "PRESSURE", "SLOTS", "LEASES", "QUEUE", "LENDING", "EVENTS"
    ]  # fmt: skip
    for height in range(1, 41):
        shown = _sections(top.render(busy, events, 100, height))
        assert [n for n in full if n in shown] == shown  # always in the same order
        if "QUEUE" in shown:  # a waiting lease is never shown without why it waits
            assert "machine has 0 of 12 slots free" in top.plain(
                top.render(busy, events, 100, height)
            ) or "+1 more waiting" in top.plain(top.render(busy, events, 100, height))


def test_long_lists_are_cut_with_a_count_of_what_is_hidden(busy):
    many = copy.deepcopy(busy)
    lease = many["leases"][1]
    many["leases"] = [lease | {"lease_id": f"cc.{i:02d}", "weight": 1} for i in range(30)]
    text = top.plain(top.render(many, [], 100, 30))
    assert "more held" in text
    shown = sum(1 for line in text.splitlines() if "cc." in line and "pytest" in line)
    hidden = int(text.split(" more held")[0].rsplit("+", 1)[1])
    assert shown + hidden == 30


def test_narrow_tables_drop_columns_rather_than_overflow(busy):
    wide = top.plain(top.render(busy, [], 100, 40))
    narrow = top.plain(top.render(busy, [], 80, 40))
    assert "SESSION  POOL" in wide
    assert "POOL" not in narrow.split("LEASES", 1)[1].splitlines()[0]  # kind says it already
    assert "reserved 4" in wide


# --- what is shown -------------------------------------------------------------------------------


def test_each_kind_has_its_own_mark_with_and_without_unicode():
    for glyphs in (top.UNICODE, top.ASCII):
        marks = [glyphs.marks[k] for k in ("work", "external", "lend", "observed")]
        assert len({*marks, glyphs.other}) == 5


def test_why_a_lease_waits_is_shown_under_it_in_the_wait_style(busy):
    lines = top.render(busy, [], 100, 40)
    waits = [text for line in lines for text, style in line if style == "wait"]
    assert waits == ["▸ pool work is at 5 of 6; this needs 2", "▸ machine has 0 of 12 slots free"]


def test_pressure_above_the_lending_limit_is_a_warning(busy):
    lines = top.render(busy, [], 100, 40)
    cpu = next(line for line in lines if "".join(t for t, _ in line).startswith("PRESSURE"))
    assert any(t.strip().startswith("38.2%") and s == "warn" for t, s in cpu)
    assert "above the 10% lending limit" in "".join(t for t, _ in cpu)


def test_the_sort_orders_the_leases(busy):
    def order(sort):
        text = top.plain(top.render(busy, [], 100, 40, sort=sort)).split("LEASES", 1)[1]
        return [r.split()[2] for r in text.splitlines()[1:6]]  # the user column

    assert order("pool") == ["bob", "alice", "alice", "runner", "carol"]
    assert order("held") == ["carol", "bob", "runner", "alice", "alice"]
    assert order("weight")[0] == "runner"
    assert order("user") == ["alice", "alice", "bob", "carol", "runner"]


def test_client_text_cannot_drive_the_terminal(busy):
    evil = copy.deepcopy(busy)
    evil["leases"][0]["user"] = "mallory\x1b]0;owned\x07"
    evil["leases"][0]["labels"] = {"cmd": "rm\x1b[2J\r\n -rf"}
    evil["queue"][0]["blocked"] = "\x9b31m red"
    events = [{"seq": 1, "time": evil["time"], "type": "granted", "user": "\x1bc", "weight": 1}]
    for unicode in (True, False):
        text = top.plain(top.render(evil, events, 120, 40, unicode=unicode))
        assert not any((ord(ch) < 32 and ch != "\n") or 0x7F <= ord(ch) < 0xA0 for ch in text)
    assert "mallory?]0;owned?" in top.plain(top.render(evil, events, 120, 40))


def test_wide_characters_keep_every_line_exactly_the_width(busy):
    wide = copy.deepcopy(busy)
    wide["leases"][1]["user"] = "山田太郎"
    wide["leases"][1]["labels"] = {"cmd": "ビルド -j"}
    wide["queue"][0]["blocked"] = "プール work は満杯です"
    for width in (61, 80, 99, 100):
        _fits(top.render(wide, [], width, 40), width, 40)
    assert "?" * 8 in top.plain(top.render(wide, [], 100, 40, unicode=False))


def test_a_status_with_fields_missing_still_renders(busy):
    sparse = copy.deepcopy(busy)
    for key in ("held", "overcommitted", "mode", "observe", "facts", "standing"):
        sparse.pop(key)
    sparse["lend"] = {}
    sparse["load"] = None
    _fits(top.render(sparse, [{"type": "something new"}], 100, 40), 100, 40)
    text = top.plain(top.render(sparse, [], 100, 40))
    assert "12/12 held" in text
    assert "no PSI" in text


def test_overcommitted_weight_is_shown_past_the_last_slot(busy):
    over = copy.deepcopy(busy)
    over["held"], over["overcommitted"] = 14, 2
    over["leases"].append(over["leases"][3] | {"lease_id": "extra", "weight": 2})
    lines = top.render(over, [], 100, 40)
    text = top.plain(lines)
    assert "14/12 held  2 over" in text
    numbers = next(line for line in lines if "".join(t for t, _ in line).startswith("SLOTS"))
    assert ("14", "bad") in [(t.strip(), s) for t, s in numbers]


def test_the_footer_says_when_the_daemon_stopped_answering():
    line = top.footer(80, sort="pool", paused=False, error="cannot reach /run/x.sock", age=12)
    text = "".join(t for t, _ in line)
    assert len(text) == 80
    assert "no daemon answers, last answer 12s ago; retrying" in text
    paused = "".join(t for t, _ in top.footer(80, sort="held", paused=True))
    assert "PAUSED" in paused and "sort: held" in paused


def test_plain_drops_styles_and_trailing_blanks():
    lines = [[("a", "title"), ("  ", "")], [("   ", "")], [("", "")]]
    assert top.plain(lines) == "a"


# --- colour --------------------------------------------------------------------------------------


def test_no_color_turns_colour_off_when_set_and_not_empty():
    assert top.use_color({}, True)
    assert top.use_color({"NO_COLOR": ""}, True)
    assert not top.use_color({"NO_COLOR": "1"}, True)
    assert not top.use_color({"NO_COLOR": "yes"}, True)
    assert not top.use_color({}, False)


def test_without_colour_every_style_is_attributes_only_and_held_slots_stay_visible(busy, events):
    import curses

    styles = {s for line in top.render(busy, events, 100, 40) for _, s in line}
    styles |= {s for line in top.help_lines() for _, s in line}
    assert {"bay:work", "pool:external", "wait", "warn"} <= styles
    for style in styles:
        look = top.look(style, color=False)
        assert (look.fg, look.bg) == (-1, -1), style
    assert top.look("bay:work", color=False).attr & curses.A_REVERSE
    assert top.look("wait", color=False).attr & curses.A_BOLD
    coloured = top.look("bay:work", color=True, colors=256, pools=list(busy["pools"]))
    assert coloured.bg != -1


def test_other_pools_take_their_own_colours_in_turn():
    order = ["work", "gpu", "nightly"]
    gpu = top.pool_colour("gpu", order, 256)
    nightly = top.pool_colour("nightly", order, 256)
    assert len({gpu, nightly, top.pool_colour("work", order, 256)}) == 3


# --- polling -------------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, status: dict) -> None:
        self.state = status
        self.seq = 0
        self.down = False

    def status(self) -> dict:
        if self.down:
            raise Unavailable("cannot reach leasehold at /x.sock: refused")
        return self.state

    def events(self, since: int = 0) -> dict:
        self.seq += 150
        batch = [{"seq": n, "time": 0.0, "type": "left"} for n in range(since + 1, self.seq + 1)]
        return {"events": batch, "next": self.seq, "dropped": False}


def test_the_watch_keeps_recent_events_and_pressure_and_survives_a_lost_daemon(busy):
    client = FakeClient(busy)
    clock = iter(range(100))
    w = top.Watch(client, clock=lambda: float(next(clock)))
    assert w.poll() and w.poll()
    assert len(w.events) == top.KEEP_EVENTS
    assert w.events[-1]["seq"] == 300 and w.since == 300
    assert list(w.history["cpu"]) == [38.2, 38.2]
    client.down = True
    assert not w.poll()
    assert "refused" in w.error and w.status is busy  # the last status stays on screen
    client.down = False
    assert w.poll() and w.error is None
    assert w.now() == busy["time"] + 1  # a second after the last answer


# --- the command ---------------------------------------------------------------------------------


def _top(sock: str, *args: str) -> int:
    parser = argparse.ArgumentParser()
    watch.register(parser.add_subparsers())
    ns = parser.parse_args(["top", *args])
    return ns.run(ns, Context(socket=sock, config=None, env={}))


def test_top_once_prints_the_machine_from_a_real_daemon(daemon, capsys):
    client = Client(daemon.path)
    client.acquire(lease_id="build.1", kind="work", weight=2, labels={"cmd": "make -j"})
    client.acquire(lease_id="build.2", kind="work", weight=1, wait_s=0)
    assert _top(str(daemon.path), "--once") == 0
    out = capsys.readouterr().out
    assert out.isascii() or "■" in out
    lines = out.splitlines()
    assert len(lines) <= top.PLAIN_SIZE[1]
    assert all(top.cells(line) <= top.PLAIN_SIZE[0] for line in lines)
    assert "test-machine" in lines[0] and "2/2 held" in lines[0]
    assert "build.1" in out and "make -j" in out
    assert "QUEUE" in out and "build.2" in out and "granted" in out


def test_top_off_a_terminal_prints_once_even_without_the_flag(daemon):
    out = io.StringIO()
    assert top.run(Client(daemon.path), stdout=out) == 0
    assert "test-machine" in out.getvalue()


def test_top_once_without_a_daemon_exits_as_status_does(sock_dir, capsys):
    assert _top(str(sock_dir / "none.sock"), "--once") == EXIT_UNAVAILABLE
    assert "no daemon answers" in capsys.readouterr().err


def test_top_refuses_an_interval_out_of_range():
    with pytest.raises(SystemExit):
        _top("/nowhere", "--interval", "0")


def test_the_top_command_runs_as_a_program(daemon):
    shown = cli(daemon.path, "top", "--once")
    assert shown.returncode == 0, shown.stderr
    assert "test-machine" in shown.stdout


def test_the_header_says_when_the_guard_holds_pools_to_reserved_and_containment_is_off(busy):
    plain = _text(top.render(busy, [], width=160, height=40))
    assert "guard:" not in plain and "contain off" not in plain
    busy["guard"] = {"on": True, "reason": "memory pressure 31% > 20%"}
    busy["contain"] = {"mode": "off", "hard_cpu": False}
    header = _text(top.render(busy, [], width=160, height=40)).splitlines()[0]
    assert "guard: reserved slots only" in header and "contain off" in header
