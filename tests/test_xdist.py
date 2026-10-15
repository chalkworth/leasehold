"""Whether a pytest run starts xdist workers (leasehold.xdist): from its arguments read one by
one, from `PYTEST_ADDOPTS`, and from the `addopts` of the config file pytest would read."""

from __future__ import annotations

from pathlib import Path

import pytest

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
     ["-p", "no:xdist"], ["--deselect", "-n4"], ["-m", "-n 4"], ["--no-header"], ["-kn", "4"]],
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
