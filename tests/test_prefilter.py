"""The Claude Code hook's pre-filter (integrations/claude-code/hooks/prefilter.sh) and its word
list (leasehold.programs, `leasehold commands --programs`): no Python for a command that cannot
need a lease, the Python hook for every one that might."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from support import CHILD_ENV, cli
from test_commands import HEAVY

from leasehold import programs, shell
from leasehold.commands import CommandSettings

ROOT = Path(__file__).resolve().parents[1]
PREFILTER = ROOT / "integrations" / "claude-code" / "hooks" / "prefilter.sh"
#: A stand-in for `leasehold` that records how it was called and what it read.
FAKE = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$MARKER"\ncat >> "$MARKER"\necho \'{"ok": 1}\'\n'


def event(command: str, tool_use_id: str = "toolu_01ABC") -> str:
    """A PreToolUse event as Claude Code writes it: one line of JSON."""
    return json.dumps(
        {
            "session_id": "4b1c-9e2f",
            "transcript_path": "/home/u/.claude/projects/p/4b1c-9e2f.jsonl",
            "cwd": "/home/u/go/src/make",  # program names elsewhere in the event do not count
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "run pytest"},
            "tool_use_id": tool_use_id,
        }
    )


@pytest.fixture
def box(tmp_path):
    """A PATH with the fake `leasehold`, the default rules' word list, and a run directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "leasehold").write_text(FAKE)
    (bin_dir / "leasehold").chmod(0o755)
    cache = tmp_path / "cache"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert programs.write(
        cache / "leasehold" / "programs", programs.names(CommandSettings().shell_rules())
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CACHE_HOME": str(cache),
        "XDG_RUNTIME_DIR": str(run_dir),
        "MARKER": str(tmp_path / "marker"),
    }
    return env, tmp_path / "marker"


def prefilter(env: dict, stage: str, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(PREFILTER), stage], input=stdin, env=env, capture_output=True, text=True,
        timeout=10,
    )  # fmt: skip


def called(marker: Path) -> str | None:
    return marker.read_text() if marker.exists() else None


def test_a_light_command_never_starts_leasehold(box):
    env, marker = box
    for command in ("ls -la", "git status", "echo cargotest", 'printf "a\\npytest"'):
        done = prefilter(env, "pre", event(command))
        assert done.returncode == 0 and done.stdout == "" and called(marker) is None, command


def test_a_heavy_command_goes_to_the_hook_with_the_same_event(box):
    env, marker = box
    stdin = event("cargo test --workspace")
    done = prefilter(env, "pre", stdin)
    assert done.returncode == 0 and json.loads(done.stdout) == {"ok": 1}
    assert called(marker) == f"hook claude-code pre\n{stdin}\n"


@pytest.mark.parametrize(
    "command",
    [c for c, _ in HEAVY] + ['"py"test -x', "py\\test", ".venv/bin/pytest"],
)
def test_every_command_a_rule_matches_goes_to_the_hook(box, command):
    env, marker = box
    if not command.startswith(('"py"', "py\\")):  # quotes the shell drops
        assert shell.match(CommandSettings().shell_rules(), command) is not None
    assert prefilter(env, "pre", event(command)).returncode == 0
    assert called(marker) is not None


def test_with_no_word_list_or_no_command_found_the_hook_decides(box, tmp_path):
    env, marker = box
    prefilter(env, "pre", '{"tool_input": {"cmd": "ls"}}')
    assert called(marker) is not None
    marker.unlink()
    (tmp_path / "cache" / "leasehold" / "programs").unlink()
    prefilter(env, "pre", event("ls"))
    assert called(marker) is not None


def test_inside_a_lease_or_without_leasehold_it_does_nothing(box):
    env, marker = box
    assert prefilter(env | {"LEASEHOLD_LEASE_ID": "x"}, "pre", event("make")).returncode == 0
    done = prefilter(env | {"PATH": "/usr/bin:/bin"}, "pre", event("make"))
    assert done.returncode == 0 and done.stdout == ""
    assert called(marker) is None


def test_post_goes_to_the_hook_only_for_a_noted_lease(box, tmp_path):
    env, marker = box
    assert prefilter(env, "post", event("make")).returncode == 0
    assert called(marker) is None
    notes = tmp_path / "run" / f"leasehold-hooks-{os.getuid()}"
    notes.mkdir(mode=0o700)
    (notes / "cc.toolu_01ABC").write_text("0 -\n")
    prefilter(env, "post", event("make"))
    assert called(marker).startswith("hook claude-code post\n")


def test_post_without_a_runtime_dir_or_with_an_odd_id_goes_to_the_hook(box):
    env, marker = box
    no_run = {k: v for k, v in env.items() if k != "XDG_RUNTIME_DIR"}
    prefilter(no_run, "post", event("make"))
    assert called(marker) is not None
    marker.unlink()
    prefilter(env, "post", event("make", tool_use_id="toolu/" + "z" * 80))
    assert called(marker) is not None


def test_a_program_the_prefilter_cannot_read_as_a_word_removes_the_list(tmp_path):
    target = tmp_path / "programs"
    assert programs.write(target, ["make", "pytest"])
    assert target.read_text() == "make\npytest\n"
    assert not programs.write(target, ["make", "my tool"])
    assert not target.exists()


def test_commands_programs_prints_and_writes_the_served_list(daemon, tmp_path):
    done = cli(daemon.path, "commands", "--programs")
    assert done.returncode == 0, done.stderr
    names = done.stdout.split()
    assert "pytest" in names and "cargo" in names and names == sorted(names)
    env = CHILD_ENV | {"XDG_CACHE_HOME": str(tmp_path)}
    done = cli(daemon.path, "commands", "--programs", "--write", env=env)
    target = tmp_path / "leasehold" / "programs"
    assert done.returncode == 0 and done.stdout.strip() == str(target)
    assert target.read_text().split() == names
    assert cli(daemon.path, "commands", "--write").returncode == 2
