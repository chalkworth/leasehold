"""Which commands take a lease (leasehold.commands, leasehold.shell): reading a shell command
line, the rules, their config, and the rules a daemon serves."""

from __future__ import annotations

import pytest
from support import machine_config

from leasehold import shell
from leasehold.client import Client
from leasehold.commands import CommandSettings, match
from leasehold.shell import programs

DEFAULTS = CommandSettings()


#: Heavy commands in the many forms an agent writes them, and the program each runs.
HEAVY = [
    ("pytest -x", "pytest"),
    ("cd api && uv run pytest -x 2>&1 | tail -20", "pytest"),
    ("python3 -m pytest tests", "pytest"),
    ("CI=1 cargo test --release", "cargo"),
    ("nice -n 10 make -j8 all", "make"),
    ("timeout 600 go test ./...", "go"),
    ("env FOO=1 time ninja -C build", "ninja"),
    ("bash -c 'npm test'", "npm"),
    ("npx vitest run", "vitest"),
    ("pnpm exec tsc -b", "tsc"),
    ("docker compose up --build -d", "docker"),
    ("for f in a b; do pytest $f; done", "pytest"),
    ("if true; then make; fi", "make"),
    ("(cd web && npm ci)", "npm"),
    ("./node_modules/.bin/jest --ci", "jest"),
    ("/usr/bin/make", "make"),
    ("bash -lc 'pytest -q'", "pytest"),
    ("sh -o pipefail -c 'cargo test | tee log'", "cargo"),
    ("env -u FOO pytest", "pytest"),
    ("timeout -k 5 600 pytest", "pytest"),
    ("npx -p @playwright/test playwright test", "playwright"),
    ("sudo -u ci make -j4", "make"),
    ("find tests -name '*.py' | xargs -P 4 pytest", "pytest"),
    ("true && pytest", "pytest"),
    ("false || pytest", "pytest"),
    ("true || (false)&& pytest", "pytest"),
    ("(cd web)&&npm ci", "npm"),
    ("make>log 2>&1", "make"),
    ("cd api\nuv run pytest -x", "pytest"),
    ("cd a\n\tmake", "make"),
]


@pytest.mark.parametrize(("command", "program"), HEAVY)
def test_a_heavy_command_matches_wherever_it_sits_in_the_line(command, program):
    found = match(DEFAULTS, command)
    assert found is not None and found.program == program


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "run pytest"',
        "cargo --version",
        "go version",
        "npm view react",
        "ls > pytest.log",
        "echo hi # pytest",
        "echo 'unclosed pytest",
        "grep -r make src",
        "",
        "false && pytest",
        "true || cargo build",
        "false && cd x && make",
        "bash script.sh",
        "! true && pytest",
        "false && (true; pytest)",
        "true || (false; pytest)",
        "false && { make; }",
    ],
)
def test_a_light_command_matches_nothing(command):
    assert match(DEFAULTS, command) is None


def test_the_heaviest_match_wins_and_the_config_comes_first():
    assert match(DEFAULTS, "npm ci && pytest").program == "pytest"  # 4 beats 2
    mine = CommandSettings(rule=[{"programs": ["pytest"], "weight": 1}])
    found = match(mine, "npm ci && pytest")
    assert (found.program, found.rule.weight, found.rule.source) == ("pytest", 1, "config")
    alone = CommandSettings(defaults=False, rule=[{"programs": ["e2e.sh"], "weight": 3}])
    assert match(alone, "pytest") is None and match(alone, "./e2e.sh --all").rule.weight == 3


def test_programs_skips_redirect_targets_and_descriptors():
    assert list(programs("make 2>&1 >build.log")) == [("make", [])]


def test_rules_are_checked_in_the_config(tmp_path):
    with pytest.raises(ValueError, match="not a regular expression"):
        CommandSettings(rule=[{"programs": ["x"], "args": "(", "weight": 1}])
    with pytest.raises(ValueError, match="not under pools"):
        machine_config(
            tmp_path, commands={"rule": [{"programs": ["x"], "weight": 1, "pool": "no"}]}
        )
    with pytest.raises(ValueError, match="only observed programs"):
        machine_config(
            tmp_path, commands={"rule": [{"programs": ["x"], "weight": 1, "pool": "observed"}]}
        )


def test_the_daemon_serves_rules_its_pools_can_grant(daemon):
    """The test machine's work pool may hold 2, so every rule asks for at most 2."""
    answer = Client(daemon.path).commands()
    assert answer["wait_s"] == 300 and answer["when_busy"] == "deny"
    assert {r["source"] for r in answer["rules"]} == {"default"}
    assert all(r["pool"] == "work" and 1 <= r["weight"] <= 2 for r in answer["rules"])
    served = shell.served(answer)
    assert shell.match(served.rules, "uv run pytest").rule.weight == 2


def _top_level_help() -> str:
    import os

    from leasehold.cli import _parser

    os.environ["COLUMNS"] = "200"
    return _parser().format_help()


def test_top_level_help_lists_only_the_visible_commands_in_order():
    from leasehold.cli import VISIBLE

    listed = _top_level_help().split("\n\n")[2].split("\n")[1:]
    names = [line.split()[0] for line in listed if line.startswith("    ")]
    assert names == list(VISIBLE)


def test_top_level_help_names_every_hidden_command_last():
    text = " ".join(_top_level_help().split())
    advanced = text.split("advanced:")[1]
    hidden = ["acquire", "keep", "release", "hook", "shims", "facts", "ui"]
    hidden += ["metrics", "mcp", "report", "observe", "lend", "pool"]
    for name in hidden:
        assert name in advanced


def test_a_hidden_command_still_parses_and_has_help(capsys):
    import pytest

    from leasehold.cli import _parser

    assert _parser().parse_args(["release", "abc"]).command == "release"
    with pytest.raises(SystemExit) as exit_:
        _parser().parse_args(["facts", "--help"])
    assert exit_.value.code == 0
    assert "facts" in capsys.readouterr().out


def test_the_experimental_commands_say_so(capsys):
    import pytest

    from leasehold.cli import _parser

    for name in ("mcp", "ui", "metrics", "report", "observe", "lend"):
        with pytest.raises(SystemExit):
            _parser().parse_args([name, "--help"])
        assert "Experimental: may change before 1.0." in " ".join(capsys.readouterr().out.split())
