"""Which commands take a lease (leasehold.commands, leasehold.shell): reading a shell command
line, the rules, their config, and the rules a daemon serves."""

from __future__ import annotations

import json

import pytest
from support import CHILD_ENV, cli, machine_config

from leasehold import jobs, shell
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


def test_a_rule_gives_its_processes_jobs_threads_or_a_count():
    def threads(value):
        return CommandSettings(rule=[{"programs": ["x"], "weight": 1, "threads": value}])

    assert CommandSettings(rule=[{"programs": ["x"], "weight": 1}]).rule[0].threads == "jobs"
    assert threads(3).rule[0].threads == 3 and threads("jobs").rule[0].threads == "jobs"
    for bad in (0, -1, "two", "3", True, 1.5):
        with pytest.raises(ValueError, match="threads"):
            threads(bad)


@pytest.mark.parametrize(
    "command",
    ["pytest -n 4", "pytest -n4", "pytest -n=4", "pytest -nauto", "pytest --numprocesses=auto",
     "pytest -x --numprocesses 3 tests", "py.test -n logical", "uv run pytest -n auto",
     "python -m pytest -n 2"],
)  # fmt: skip
def test_an_xdist_run_gets_one_thread_per_worker(command):
    found = match(DEFAULTS, command)
    assert (found.program, found.rule.threads, found.rule.weight) in {
        ("pytest", 1, 4), ("py.test", 1, 4),
    }  # fmt: skip


@pytest.mark.parametrize(
    "command", ["pytest", "pytest -n 0", "pytest -k n", "pytest --no-header", "tox", "nox"]
)
def test_a_serial_run_and_tox_and_nox_get_the_job_count(command):
    assert match(DEFAULTS, command).rule.threads == "jobs"


@pytest.mark.parametrize(
    "command",
    ["pytest -k '-n 4'", "pytest -- -n 4", "pytest -n 0 tests"],
)
def test_xdist_options_are_read_as_arguments_not_as_text(command):
    assert match(DEFAULTS, command).rule.threads == "jobs"


@pytest.mark.parametrize("command", ["pytest -vn 4", "pytest -xn4", "uv run --with x pytest -n2"])
def test_combined_short_flags_and_runner_options_still_find_xdist(command):
    found = match(DEFAULTS, command)
    assert (found.program, found.rule.threads) == ("pytest", 1)


@pytest.mark.parametrize(
    "command",
    ["uvx --with pytest-xdist pytest -n 2", "uvx --from pytest pytest -n2",
     "uv tool run --with x pytest -n 2", "python -X dev -m pytest -n 2",
     "python3 -W error -m pytest -n 2", "python -O -u -I -m pytest -n 2",
     "python -uXdev -m pytest -n 2", "python -mpytest -n 2"],
)  # fmt: skip
def test_uvx_and_python_options_before_m_still_find_pytest(command):
    found = match(DEFAULTS, command)
    assert (found.program, found.rule.threads) == ("pytest", 1)


@pytest.mark.parametrize(
    "command", ["python -c 'import pytest' -m pytest -n 2", "python script.py -m pytest -n 2"]
)
def test_python_running_a_script_or_a_command_is_not_pytest(command):
    found = match(DEFAULTS, command)
    assert found is None or found.program != "pytest"


@pytest.mark.parametrize(
    "command",
    ["pytest --cov -n auto", "pytest --cov -n 4 tests", "pytest --pyargs -n 4 pkg",
     "pytest -d --tx 4*popen", "pytest --maxfail 3 -n 2"],
)  # fmt: skip
def test_an_option_never_takes_a_following_option_as_its_value(command):
    assert match(DEFAULTS, command).rule.threads == 1


@pytest.mark.parametrize("command", ["pytest --maxfail -n 2", "pytest -k '-n 4' -n 2"])
def test_an_option_missing_its_value_is_a_run_pytest_refuses(command):
    assert match(DEFAULTS, command).rule.threads == "jobs"


def test_uv_run_options_that_take_a_value_are_not_the_program():
    words = list(shell.programs("uv run --with numpy -p 3.12 --group dev pytest -x"))
    assert words == [("pytest", ["-x"])]
    assert list(shell.programs("uv run --frozen -- pytest")) == [("pytest", [])]


@pytest.mark.parametrize(
    ("line", "threads"),
    [
        ("pytest -n 4; pytest -n 2", "jobs/4"),
        ("pytest -n 4; pytest -n2 x", "jobs/4"),
        ("pytest -n auto; pytest -n 2", 1),
        ("cargo test && pytest -n auto", 1),
        ("cargo test && pytest -n 4", "jobs/4"),
        ("pytest -n 4 && pytest", "jobs/4"),
        ("cargo test && make", "jobs"),
        ("ls -l", None),
    ],
)
def test_a_line_of_several_programs_takes_the_fewest_threads_any_of_them_allows(line, threads):
    """No program on the line runs more threads per process than its own rule gives it."""
    assert shell.threads(DEFAULTS.shell_rules(), list(shell.programs(line))) == threads


def test_a_rule_with_a_count_lowers_a_share_of_the_jobs():
    rules = [shell.Rule.of({"programs": ["tool"], "weight": 1, "threads": 3}),
             *DEFAULTS.shell_rules()]  # fmt: skip
    found = shell.threads(rules, list(shell.programs("tool; pytest -n 2")))
    assert found == "jobs/2:3"
    assert jobs.thread_count(found, 16) == 3 and jobs.thread_count(found, 4) == 2


_OWN_PYTEST = shell.Rule.of({"programs": ["pytest"], "weight": 2, "source": "config"})


@pytest.mark.parametrize(
    ("own", "line", "threads"),
    [
        (_OWN_PYTEST, "pytest -n auto", 1),
        (_OWN_PYTEST, "pytest -n 4", "jobs/4"),
        (_OWN_PYTEST, "pytest -n auto --maxprocesses 3", "jobs/3"),
        (_OWN_PYTEST, "pytest", "jobs"),
        (_OWN_PYTEST, "cargo test && pytest -n 4", "jobs/4"),
        (_OWN_PYTEST, "cargo test && pytest -n auto", 1),
        (shell.Rule.of({"programs": ["pytest"], "weight": 2, "threads": 3}), "pytest -n 2",
         "jobs/2:3"),
        (shell.Rule.of({"programs": ["pytest"], "weight": 2, "threads": 8}), "pytest -n auto", 1),
    ],
)  # fmt: skip
def test_a_config_rule_for_pytest_still_shares_the_jobs_among_its_workers(own, line, threads):
    """Whatever rule a pytest run matches, its workers share the job count; the rule's own
    `threads` can only lower that."""
    rules = [own, *DEFAULTS.shell_rules()]
    assert shell.match_programs(rules, list(shell.programs(line))).rule.source == "config"
    assert shell.threads(rules, list(shell.programs(line))) == threads


@pytest.mark.parametrize(
    ("line", "threads"),
    [("tool", "jobs/4:2"), ("tool; pytest -n 8", "jobs/8:2"), ("tool; other", 1),
     ("tool; cargo test", "jobs/4:2")],
)  # fmt: skip
def test_a_served_share_with_a_limit_is_read_whole(line, threads):
    rules = [shell.Rule.of({"programs": ["tool"], "weight": 1, "threads": "jobs/4:2"}),
             shell.Rule.of({"programs": ["other"], "weight": 1, "threads": 1}),
             *DEFAULTS.shell_rules()]  # fmt: skip
    assert shell.threads(rules, list(shell.programs(line))) == threads


def test_builds_get_the_job_count():
    rules = [r for _src, r in CommandSettings().rules()]
    assert all(r.threads == "jobs" for r in rules if r.programs[0] != "pytest" or not r.args)


def test_a_rule_served_without_threads_gives_the_job_count():
    """A daemon older than the setting serves rules without it."""
    assert shell.Rule.of({"programs": ["pytest"], "weight": 4}).threads == "jobs"
    assert shell.Rule.of({"programs": ["x"], "weight": 1, "threads": 2}).threads == 2
    assert shell.Rule.of({"programs": ["x"], "weight": 1, "threads": "jobs"}).threads == "jobs"


@pytest.mark.parametrize(
    ("served", "threads"),
    [("jobs/4", "jobs/4"), ("jobs/4:2", "jobs/4:2"), ("jobs/0", 1), ("jobs/x", 1),
     ("jobs/4:", 1), ("half", 1), (0, 1), (True, 1), (2.5, 1)],
)  # fmt: skip
def test_a_served_share_is_kept_and_anything_unknown_gives_one_thread(served, threads):
    assert shell.Rule.of({"programs": ["x"], "weight": 1, "threads": served}).threads == threads


def test_the_daemon_serves_rules_its_pools_can_grant(daemon):
    """The test machine's work pool may hold 2, so every rule asks for at most 2."""
    answer = Client(daemon.path).commands()
    assert answer["wait_s"] == 300 and answer["when_busy"] == "deny"
    assert {r["source"] for r in answer["rules"]} == {"default"}
    assert all(r["pool"] == "work" and 1 <= r["weight"] <= 2 for r in answer["rules"])
    served = shell.served(answer)
    assert shell.match(served.rules, "uv run pytest").rule.weight == 2
    assert shell.match(served.rules, "uv run pytest -n 2").rule.threads == 1
    assert shell.match(served.rules, "uv run pytest").rule.threads == "jobs"
    assert shell.match(served.rules, "cargo build").rule.threads == "jobs"
    assert all("threads" in r for r in answer["rules"])


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


def test_the_built_in_weights_come_from_the_config():
    from leasehold import template

    weights = {r.programs[0]: r.weight for _src, r in CommandSettings().rules()}
    assert weights["pytest"] == 4 and weights["npm"] == 2  # without the keys
    set_ = CommandSettings(heavy_weight=3, light_weight=1)
    weights = {r.programs[0]: r.weight for _src, r in set_.rules()}
    assert weights["pytest"] == weights["cargo"] == weights["make"] == 3
    assert weights["npm"] == weights["jest"] == 1
    assert [template.weights(n) for n in (1, 3, 4, 6, 14)] == [
        (1, 1), (3, 2), (2, 1), (3, 2), (7, 4),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "command",
    ["uv --directory p run pytest -n 2", "uv --project p run pytest -n 2", "uv -q run pytest -n 2",
     "uv --color never --offline run pytest -n 2", "uv --directory=p run --with x pytest -n 2",
     "uv --cache-dir /c tool run pytest -n 2", "python3.13t -m pytest -n 2",
     "python3t -m pytest -n 2", "pypy3 -m pytest -n 2", "pypy -m pytest -n 2",
     "python3.13.1 -m pytest -n 2", "/usr/bin/python3.12.7t -m pytest -n 2"],
)  # fmt: skip
def test_uvs_global_options_and_more_pythons_still_find_pytest(command):
    found = match(DEFAULTS, command)
    assert (found.program, found.rule.weight) == ("pytest", 4)


def test_uvs_directory_is_where_the_config_is_read(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n")
    rules = DEFAULTS.shell_rules()
    for command in ("uv --directory sub run pytest", "uv run --directory sub pytest",
                    "uv --directory=sub run pytest"):  # fmt: skip
        found = list(shell.programs(command))
        assert shell.threads(rules, found, env={}, cwd=tmp_path) == "jobs/2", command
    assert shell.threads(rules, list(shell.programs("uv run pytest")), env={}, cwd=tmp_path) == (
        "jobs"
    )


def test_testing_a_line_reports_the_threads_hold_would_give_it(daemon, tmp_path):
    env = {**CHILD_ENV, "PYTEST_ADDOPTS": ""}
    text = cli(daemon.path, "commands", "--test", "cargo test && pytest -n 2", cwd=tmp_path,
               env=env)  # fmt: skip
    assert text.returncode == 0, text.stderr
    assert "threads per process: the job count divided by 2" in text.stdout
    done = cli(daemon.path, "commands", "--test", "cargo test && pytest -n 2", "--json",
               cwd=tmp_path, env=env)  # fmt: skip
    assert json.loads(done.stdout)["threads"] == "jobs/2"
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n auto\n")
    auto = cli(daemon.path, "commands", "--test", "pytest", "--json", cwd=tmp_path, env=env)
    assert json.loads(auto.stdout)["threads"] == 1
