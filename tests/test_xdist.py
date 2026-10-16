"""Whether a pytest run starts xdist workers (leasehold.xdist): from its arguments read one by
one, from `PYTEST_ADDOPTS`, and from the `addopts` of the config file pytest would read."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest

from leasehold import jobs, shell, xdist
from leasehold.commands import CommandSettings
from leasehold.xdist import config_addopts, in_args, run_starts_workers


@pytest.mark.parametrize(
    "args",
    [["-n", "4"], ["-n4"], ["-n=4"], ["-nauto"], ["-n", "logical"], ["--numprocesses=2"],
     ["--numprocesses", "3"], ["-vn", "4"], ["-xn4"], ["-x", "tests", "-n", "2"],
     ["-n", "0", "-n", "2"]],
)  # fmt: skip
def test_arguments_that_start_workers(args):
    assert in_args(args) is True


@pytest.mark.parametrize(
    "args",
    [[], ["-k", "-n 4"], ["-k-n4"], ["--", "-n", "4"], ["-n", "0"], ["-n", "2", "-n", "0"],
     ["-p", "no:xdist"], ["-m", "-n 4"], ["--no-header"], ["-kn", "4"],
     ["--maxfail", "-n", "2"], ["-n", "2", "-k"], ["-k", "-n 4", "-n", "2"]],
)  # fmt: skip
def test_arguments_that_do_not(args):
    assert in_args(args) is not True


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pytest.ini", "[pytest]\naddopts = -q -n 4\n"),
        (".pytest.ini", "[pytest]\naddopts = -n 4\n"),
        ("pytest.toml", '[pytest]\naddopts = ["-n", "4"]\n'),
        ("pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n 4"\n'),
        ("pyproject.toml", '[tool.pytest]\naddopts = ["-n", "4"]\n'),
        ("tox.ini", "[tox]\n[pytest]\naddopts = --numprocesses=auto\n"),
        ("setup.cfg", "[metadata]\nname = x\n[tool:pytest]\naddopts = -n auto\n"),
    ],
)
def test_each_kind_of_config_is_read_from_the_directory_or_above(tmp_path, name, text):
    _write(tmp_path / name, text)
    below = tmp_path / "pkg" / "tests"
    below.mkdir(parents=True)
    assert run_starts_workers([], {}, tmp_path)
    assert run_starts_workers([], {}, below)
    assert not run_starts_workers(["-n", "0"], {}, below)  # the command line comes last


def test_a_file_without_a_pytest_section_is_passed_over_for_one_above(tmp_path):
    _write(tmp_path / "tox.ini", "[pytest]\naddopts = -n 2\n")
    _write(tmp_path / "sub" / "pyproject.toml", "[project]\nname = 'x'\n")
    _write(tmp_path / "sub" / "setup.cfg", "[metadata]\nname = x\n")
    assert config_addopts([], tmp_path / "sub") == ["-n", "2"]


def test_the_first_config_in_pytests_order_wins(tmp_path):
    _write(tmp_path / "pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-n 4"\n')
    _write(tmp_path / "pytest.ini", "[pytest]\n")  # always pytest's, even without addopts
    assert not run_starts_workers([], {}, tmp_path)


def test_a_config_named_with_c_or_found_from_path_arguments(tmp_path):
    _write(tmp_path / "ci" / "xdist.ini", "[pytest]\naddopts = -n 3\n")
    _write(tmp_path / "proj" / "pytest.ini", "[pytest]\naddopts = -n 2\n")
    (tmp_path / "proj" / "tests").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert run_starts_workers(["-c", "../ci/xdist.ini"], {}, elsewhere)
    assert run_starts_workers(["--config-file=../ci/xdist.ini"], {}, elsewhere)
    assert run_starts_workers(["../proj/tests"], {}, elsewhere)
    assert not run_starts_workers([], {}, elsewhere)


def test_pytest_addopts_comes_after_the_config_and_before_the_arguments(tmp_path):
    assert run_starts_workers([], {"PYTEST_ADDOPTS": "-n 2"}, tmp_path)
    _write(tmp_path / "pytest.ini", "[pytest]\naddopts = -n 4\n")
    assert not run_starts_workers([], {"PYTEST_ADDOPTS": "-n 0"}, tmp_path)
    assert run_starts_workers(["-n2"], {"PYTEST_ADDOPTS": "-n 0"}, tmp_path)


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", "[tool.pytest.ini_options\naddopts = '-n 4'\n"),
        ("pytest.ini", "addopts = -n 4\n[pytest\n"),
        ("pytest.ini", '[pytest]\naddopts = -n "4\n'),
        ("pytest.toml", "[pytest]\naddopts = 4\n"),
    ],
)
def test_a_config_that_cannot_be_read_counts_as_none(tmp_path, name, text):
    _write(tmp_path / name, text)
    assert config_addopts([], tmp_path) == []
    assert not run_starts_workers([], {"PYTEST_ADDOPTS": '-n "2'}, tmp_path)
    assert run_starts_workers(["-n", "2"], {}, tmp_path)


@pytest.mark.parametrize(
    "args",
    [["--cov", "-n", "auto"], ["--cov", "-n", "4", "tests"], ["--pyargs", "-n", "4", "pkg"],
     ["--cache-show", "-n", "2"], ["--dist", "load", "--tx", "4*popen"], ["-d", "--tx=popen"],
     ["-k", "x", "-n", "2"]],
)  # fmt: skip
def test_options_take_no_word_that_looks_like_an_option(args):
    assert in_args(args) is True


@pytest.mark.parametrize(
    "args",
    [["-p", "no:xdist", "-n", "4"], ["-n", "4", "-p", "no:xdist"], ["-pno:xdist", "--tx", "popen"],
     ["-n", "9" * 5000], ["--numprocesses=" + "9" * 5000]],
)  # fmt: skip
def test_xdist_turned_off_or_a_count_too_long_to_read_starts_none(args):
    assert in_args(args) is not True


def test_a_count_too_long_to_read_leaves_the_earlier_setting():
    assert in_args(["-n", "2", "-n", "9" * 5000]) is True


#: Each case: the files to write (a value starting `->` makes a symbolic link to the rest),
#: the directory pytest runs in, its arguments, and `PYTEST_ADDOPTS`.
_SETUPS = {
    "ini here": ({"pytest.ini": "[pytest]\naddopts = -x\n"}, ".", [], ""),
    **{
        f"the value of {option} is not a path": (
            {"pytest.ini": "[pytest]\naddopts = -x\n", f"{value}/pytest.ini": "[pytest]\n"},
            ".", [option, value], "",
        )
        for option, value in [("-k", "tests"), ("--basetemp", "tmp"), ("--rootdir", "r"),
                              ("-m", "x"), ("-o", "k=v"), ("--override-ini", "a=b")]
    },
    "a path argument": (
        {"pytest.ini": "[pytest]\naddopts = -x\n", "tests/pytest.ini": "[pytest]\naddopts = -v\n"},
        ".", ["tests"], "",
    ),
    "a node id and a missing path": (
        {"a/pytest.ini": "[pytest]\naddopts = -v\n", "a/test_x.py": ""},
        ".", ["a/test_x.py::test_y", "missing"], "",
    ),
    "the common ancestor": (
        {"pytest.ini": "[pytest]\naddopts = -x\n", "p/a/t.py": "", "p/b/t.py": "",
         "p/a/pytest.ini": "[pytest]\naddopts = -v\n"},
        ".", ["p/a/t.py", "p/b/t.py"], "",
    ),
    "the last -c wins": (
        {"one.ini": "[pytest]\naddopts = -x\n", "two.ini": "[pytest]\naddopts = -v\n"},
        ".", ["-c", "one.ini", "--config-file", "two.ini"], "",
    ),
    "-c in PYTEST_ADDOPTS, then the arguments'": (
        {"one.ini": "[pytest]\naddopts = -x\n", "two.ini": "[pytest]\naddopts = -v\n"},
        ".", ["-c", "two.ini"], "-c one.ini",
    ),
    "-c in PYTEST_ADDOPTS alone": (
        {"one.ini": "[pytest]\naddopts = -x\n"}, ".", [], "-c one.ini",
    ),
    "-c of a TOML file is read as pyproject.toml": (
        {"c.toml": '[tool.pytest.ini_options]\naddopts = "-x"\n'}, ".", ["-c", "c.toml"], "",
    ),
    "-c of a TOML file with a [pytest] table": (
        {"c.toml": '[pytest]\naddopts = ["-x"]\n'}, ".", ["-c", "c.toml"], "",
    ),
    "-c of a cfg file": (
        {"c.cfg": "[tool:pytest]\naddopts = -x\n"}, ".", ["-c", "c.cfg"], "",
    ),
    "-c of a file of another kind": (
        {"c.txt": "[pytest]\naddopts = -x\n"}, ".", ["-c", "c.txt"], "",
    ),
    "-c of a missing file": ({}, ".", ["-c", "nothing.ini"], ""),
    "-o addopts, the last winning": (
        {"pytest.ini": "[pytest]\naddopts = -x\n"},
        ".", ["-o", "addopts=-v", "--override-ini=addopts=-l"], "-o addopts=-q",
    ),
    "an empty pytest.toml stops the search": (
        {"pytest.ini": "[pytest]\naddopts = -x\n", "s/pytest.toml": ""}, "s", [], "",
    ),
    "a pytest.toml without its table stops the search": (
        {"pytest.ini": "[pytest]\naddopts = -x\n", "s/.pytest.toml": "[other]\na = 1\n"},
        "s", [], "",
    ),
    "an empty [tool.pytest] is no configuration": (
        {"pytest.ini": "[pytest]\naddopts = -x\n", "s/pyproject.toml": "[tool.pytest]\n"},
        "s", [], "",
    ),
    "an empty [tool.pytest.ini_options] is": (
        {"pytest.ini": "[pytest]\naddopts = -x\n",
         "s/pyproject.toml": "[tool.pytest.ini_options]\n"},
        "s", [], "",
    ),
    "a pyproject.toml without pytest's section": (
        {"pyproject.toml": "[project]\nname = 'x'\n"}, ".", [], "",
    ),
    "native TOML addopts": (
        {"pyproject.toml": '[tool.pytest]\naddopts = ["-x", "-v"]\n'}, ".", [], "",
    ),
    "both TOML tables": (
        {"pyproject.toml": '[tool.pytest]\naddopts = ["-x"]\n[tool.pytest.ini_options]\n'},
        ".", [], "",
    ),
    "malformed TOML": ({"pyproject.toml": "[tool.pytest\n"}, ".", [], ""),
    "a sectionless pytest.ini": ({"pytest.ini": "addopts = -x\n"}, ".", [], ""),
    "[pytest] in setup.cfg": ({"setup.cfg": "[pytest]\naddopts = -x\n"}, ".", [], ""),
    "tox.ini and setup.cfg": (
        {"tox.ini": "[tox]\n", "setup.cfg": "[tool:pytest]\naddopts = -x\n"}, ".", [], "",
    ),
    "a symbolic link is not resolved": (
        {"real/sub/t.py": "", "real/pytest.ini": "[pytest]\naddopts = -v\n",
         "here/pytest.ini": "[pytest]\naddopts = -x\n", "here/link": "->../real/sub"},
        "here", ["link"], "",
    ),
}  # fmt: skip


def _build(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text.startswith("->"):
            path.symlink_to(text[2:])
        else:
            path.write_text(text)


def _pytests_view(words: list[str], cwd: Path) -> tuple[Path | None, list[str]] | None:
    """The config file and `addopts` pytest itself finds; None when it fails."""
    from _pytest.config import get_config
    from _pytest.config.findpaths import determine_setup

    ns = get_config()._parser.parse_known_args(words)
    try:
        _, path, cfg, _ = determine_setup(
            inifile=ns.inifilename, override_ini=ns.override_ini, args=ns.file_or_dir,
            rootdir_cmd_arg=ns.rootdir or None, invocation_dir=cwd,
        )  # fmt: skip
    except BaseException:  # noqa: BLE001 - pytest fails with UsageError, OSError or Failed
        return None
    value = cfg["addopts"].value if "addopts" in cfg else []
    return path, shlex.split(value) if isinstance(value, str) else list(value)


def _our_view(words: list[str], cwd: Path) -> tuple[Path | None, list[str]] | None:
    read = xdist._read_words(words)
    try:
        path, addopts = xdist.locate(read.free, read.config, cwd, read.rootdir)
    except xdist._Unreadable:
        return None
    overridden = xdist._overridden(read.overrides)
    return path, addopts if overridden is None else overridden


@pytest.mark.parametrize("case", list(_SETUPS))
def test_the_config_is_found_as_pytest_finds_it(tmp_path, monkeypatch, case):
    pytest.importorskip("_pytest.config.findpaths")
    files, where, args, env = _SETUPS[case]
    _build(tmp_path, files)
    cwd = tmp_path / where
    cwd.mkdir(exist_ok=True)
    monkeypatch.chdir(cwd)  # pytest makes relative paths absolute from here
    words = [*shlex.split(env), *args]
    expected = _pytests_view(words, cwd)
    assert _our_view(words, cwd) == expected
    if not xdist._read_words(words).overrides:
        assert config_addopts(words, cwd) == (expected[1] if expected else [])


def test_an_unreadable_config_is_none_for_pytest_and_for_us(tmp_path):
    pytest.importorskip("_pytest.config.findpaths")
    if os.geteuid() == 0:
        pytest.skip("root reads any file")
    _build(tmp_path, {"pytest.ini": "[pytest]\naddopts = -n 2\n"})
    (tmp_path / "pytest.ini").chmod(0)
    assert _pytests_view([], tmp_path) is None
    assert _our_view([], tmp_path) is None
    assert not run_starts_workers([], {}, tmp_path)


def test_an_addopts_override_replaces_the_configs(tmp_path):
    _build(tmp_path, {"pytest.ini": "[pytest]\naddopts = -n 2\n"})
    assert run_starts_workers([], {}, tmp_path)
    assert not run_starts_workers(["-o", "addopts=-x"], {}, tmp_path)
    assert run_starts_workers(["--override-ini=addopts=-n 3"], {}, tmp_path)
    assert not run_starts_workers(["-o", "addopts=-n 3"], {"PYTEST_ADDOPTS": "-n 0"}, tmp_path)
    assert not run_starts_workers([], {"PYTEST_ADDOPTS": "-o addopts=-x"}, tmp_path)


@pytest.mark.parametrize("make", ["/dev/zero", "fifo", "large"])
def test_only_regular_files_up_to_a_mib_are_read(tmp_path, make):
    if make == "fifo":
        target = tmp_path / "c.ini"
        os.mkfifo(target)
    elif make == "large":
        target = tmp_path / "c.ini"
        target.write_text("[pytest]\naddopts = -n 2\n" + "# padding\n" * 120_000)
    else:
        target = Path(make)
    assert not run_starts_workers(["-c", str(target)], {}, tmp_path)
    assert run_starts_workers(["-c", str(target), "-n", "2"], {}, tmp_path)


def test_an_addopts_override_needs_no_working_directory():
    def gone() -> Path:
        raise FileNotFoundError("the working directory was removed")

    assert run_starts_workers(["-o", "addopts=-n 2"], {}, gone)
    assert not run_starts_workers([], {}, gone)


def test_the_last_plugin_choice_wins_across_config_env_and_arguments(tmp_path):
    _build(tmp_path, {"pytest.ini": "[pytest]\naddopts = -p no:xdist\n"})
    assert not run_starts_workers(["-n", "2"], {}, tmp_path)
    assert run_starts_workers(["-n", "2"], {"PYTEST_ADDOPTS": "-p xdist"}, tmp_path)
    assert run_starts_workers(["-p", "xdist.plugin", "-n", "2"], {}, tmp_path)
    on = tmp_path / "on"
    _build(on, {"pytest.ini": "[pytest]\naddopts = -p xdist -n 2\n"})
    assert not run_starts_workers([], {"PYTEST_ADDOPTS": "-pno:xdist"}, on)
    assert run_starts_workers(["-p", "xdist"], {"PYTEST_ADDOPTS": "-p no:xdist"}, on)
    assert run_starts_workers(["-p", "xdist.looponfail"], {}, on)


@pytest.mark.parametrize("args", [["-qn=4"], ["-vn=4"], ["-qn=auto"], ["-xvn=2"]])
def test_an_equals_sign_after_a_cluster_of_short_flags(args):
    assert in_args(args) is True


def test_arguments_from_a_file_are_read(tmp_path):
    (tmp_path / "args.txt").write_text("-n\nauto\n")
    (tmp_path / "more.txt").write_text("@args.txt\n")
    assert run_starts_workers(["@args.txt"], {}, tmp_path)
    assert run_starts_workers(["@more.txt"], {}, tmp_path)
    assert not run_starts_workers(["@missing.txt"], {}, tmp_path)
    (tmp_path / "big.txt").write_text("-n\nauto\n" + "x\n" * 600_000)
    assert not run_starts_workers(["@big.txt"], {}, tmp_path)


@pytest.mark.parametrize(
    ("args", "expected"),
    [(["-n", "4"], 4), (["-n", "auto"], "auto"), (["-n", "4", "--maxprocesses", "2"], 2),
     (["-n0", "--dist", "load", "--tx", "2*popen"], None), (["--tx", "2*popen"], None),
     (["--dist", "load", "--tx", "2*popen", "--tx", "popen"], 3),
     (["-n", "4", "--co"], None), (["-n", "4", "--collect-only"], None),
     (["-n", "auto", "--pdb"], None), (["-n", "2", "--pdb"], None), (["--pdb"], None),
     (["-d", "--tx", "popen", "-n", "0"], None)],
)  # fmt: skip
def test_workers_are_counted_as_pytest_xdist_does(args, expected):
    assert xdist.workers_in_args(args) == expected


@pytest.mark.parametrize(
    ("args", "expected"),
    [(["-n", "auto", "--maxprocesses", "3"], 3), (["-n", "logical", "--maxprocesses=2"], 2),
     (["--maxprocesses", "3", "-n", "auto", "--pdb"], None)],
)  # fmt: skip
def test_auto_with_maxprocesses_starts_at_most_that_many(args, expected):
    """`-n auto` starts the job count of workers, or M with `--maxprocesses M` when fewer:
    a share of `jobs/M` gives each the same threads either way."""
    assert xdist.workers_in_args(args) == expected


def test_a_line_running_auto_with_maxprocesses_shares_the_jobs_among_them():
    rules = CommandSettings().shell_rules()
    found = list(shell.programs("pytest -n auto --maxprocesses 4"))
    assert shell.threads(rules, found) == "jobs/4"
    assert jobs.thread_count("jobs/4", 16) == 4 and jobs.thread_count("jobs/4", 2) == 1


def test_an_args_file_that_names_itself_starts_nothing_and_stops(tmp_path):
    """pytest fails on such a file, so it runs nothing."""
    (tmp_path / "self.txt").write_text("-v\n@self.txt\n")
    (tmp_path / "a.txt").write_text("@b.txt\n")
    (tmp_path / "b.txt").write_text("-n\n4\n@a.txt\n")
    started = time.monotonic()
    assert xdist._expand(["@self.txt"], tmp_path) is None
    assert xdist._expand(["@a.txt"], tmp_path) is None
    assert xdist.workers(["@a.txt"], {}, tmp_path) is None
    assert xdist.workers(["-n", "2", "@self.txt"], {}, tmp_path) is None
    assert xdist.workers(["-n", "2", "@b.txt"], {"PYTEST_ADDOPTS": ""}, tmp_path) is None
    assert time.monotonic() - started < 2


def test_args_files_expand_to_at_most_max_words_and_the_rest_counts_as_auto(tmp_path):
    (tmp_path / "wide.txt").write_text("-v\n" * 2_000)
    (tmp_path / "fan.txt").write_text("@wide.txt\n" * 50)
    started = time.monotonic()
    words, unknown = xdist._expand(["@fan.txt", "-n", "4"], tmp_path)
    assert unknown and words[-2:] == ["-n", "4"] and len(words) <= xdist.MAX_WORDS + 3
    assert xdist.workers(["@fan.txt", "-n", "4"], {}, tmp_path) == "auto"
    assert time.monotonic() - started < 2


def test_an_args_file_at_the_word_limit_still_lets_the_arguments_after_it_count(tmp_path):
    (tmp_path / "nodes.txt").write_text("".join(f"t.py::t{i}\n" for i in range(10_000)))
    assert xdist.workers(["@nodes.txt", "-n", "2"], {}, tmp_path) == 2
    rules = CommandSettings().shell_rules()
    found = list(shell.programs("pytest @nodes.txt -n 2"))
    assert shell.threads(rules, found, env={}, cwd=tmp_path) == "jobs/2"


def test_an_args_file_cut_at_the_limit_never_leaves_an_option_without_its_value(tmp_path):
    (tmp_path / "nodes.txt").write_text("".join(f"t{i}\n" for i in range(9_999)) + "-k\nx\n")
    assert xdist.workers(["@nodes.txt", "-n", "2"], {}, tmp_path) == "auto"
    (tmp_path / "fewer.txt").write_text("".join(f"t{i}\n" for i in range(9_999)))
    assert xdist.workers(["@fewer.txt", "-n", "auto"], {}, tmp_path) == "auto"
    assert xdist.workers(["@fewer.txt", "-n", "3"], {}, tmp_path) == 3


def test_args_files_nested_past_the_depth_limit_count_as_auto(tmp_path):
    for depth in range(9):
        (tmp_path / f"f{depth}.txt").write_text(f"@f{depth + 1}.txt\n")
    (tmp_path / "f9.txt").write_text("-n\n2\n")
    assert xdist.workers(["@f0.txt"], {}, tmp_path) == "auto"
    assert xdist.workers(["@f2.txt"], {}, tmp_path) == 2
    rules = CommandSettings().shell_rules()
    assert shell.threads(rules, list(shell.programs("pytest @f0.txt")), env={}, cwd=tmp_path) == 1


@pytest.mark.parametrize(
    "args",
    [["-n 3"], ["-n", " 3"], ["-n", "+3"], ["--numprocesses", "3 "], ["-n", "3", "--setup-plan"],
     ["-n", "3", "--setup-only"], ["-n", "5", "--maxprocesses", " 3"]],
)  # fmt: skip
def test_counts_are_read_as_pytest_xdist_reads_them(args):
    """xdist reads `-n` and `--maxprocesses` with `int`, so spaces and a `+` are fine, and
    `--setup-plan` still runs on its workers."""
    assert xdist.workers_in_args(args) == 3


def test_an_args_file_line_with_a_space_is_one_word(tmp_path):
    (tmp_path / "args.txt").write_text("-n 3\n")
    assert xdist.workers(["@args.txt"], {}, tmp_path) == 3
