"""Whether a pytest run starts pytest-xdist worker processes: from its arguments, from
`PYTEST_ADDOPTS`, and from the `addopts` of the configuration file pytest would read.

pytest reads its options as the config file's `addopts`, then `PYTEST_ADDOPTS`, then the
command line, the last of a repeated option winning; `run_starts_workers` reads them in that
order. Workers start with `-n N`/`--numprocesses N` (N a count above 0, `auto` or `logical`),
also written `-nN`, `-n=N`, `--numprocesses=N` or after other short flags (`-vn 4`, `-xn4`).
`-n 0` and `-p no:xdist` start none. Arguments after `--`, and option values (`-k '-n 4'`),
are not options.

The config file is found as pytest finds it: `-c`/`--config-file` when given; otherwise, from
the common ancestor of the arguments that are existing paths (else the directory the command
runs in) upwards, the first directory with `pytest.toml`, `.pytest.toml`, `pytest.ini`,
`.pytest.ini`, a `pyproject.toml` with `[tool.pytest]` or `[tool.pytest.ini_options]`, a
`tox.ini` with `[pytest]`, or a `setup.cfg` with `[tool:pytest]`, in that order. A file that
cannot be read or parsed counts as no `addopts`. Standard library only: the shims and agent
hooks import it.
"""

from __future__ import annotations

import configparser
import os
import shlex
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

#: The built-in xdist rule's `args`, as a regular expression for clients that match a rule's
#: arguments as one string; this module decides for a rule with exactly this `args`
#: (`in_args`).
XDIST_ARGS = r"(^|\s)(-n\s*=?\s*|--numprocesses(=|\s+))(auto|logical|[1-9][0-9]*)(\s|$)"

#: pytest's short options that take a value: in `-kn`, the `n` is `-k`'s value.
_SHORT_VALUE = frozenset("kmcpoWrn")
#: pytest's long options that take the next word as their value.
_LONG_VALUE = frozenset(
    {
        "--numprocesses", "--config-file", "--rootdir", "--deselect", "--ignore",
        "--ignore-glob", "--confcutdir", "--basetemp", "--junit-xml", "--junitxml", "--maxfail",
        "--durations", "--tb", "--capture", "--log-level", "--log-file", "--override-ini",
        "--import-mode", "--cov", "--cov-report", "--dist", "--tx", "--maxprocesses",
        "--max-worker-restart", "--pyargs", "--keyword", "--markers-expr", "--log-cli-level",
        "--log-format", "--log-date-format", "--log-file-level", "--cache-show", "--rsyncdir",
        "--rsyncignore", "--junit-prefix",
    }
)  # fmt: skip
_CONFIGS = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml",
            "tox.ini", "setup.cfg")  # fmt: skip


def _count(value: str) -> bool | None:
    """Whether an `-n` value starts workers; None when it is not one."""
    if value in ("auto", "logical"):
        return True
    if value.isdigit():
        return int(value) > 0
    return None


def in_args(args: Sequence[str]) -> bool | None:
    """Whether `args` (pytest's arguments, one element each) start xdist workers: True or
    False as the last `-n` or `-p no:xdist` says, None when they say nothing."""
    said: bool | None = None
    i = 0
    while i < len(args):
        word = args[i]
        i += 1
        if word == "--":
            break
        value: str | None = None
        if word == "--numprocesses" or word.startswith("--numprocesses="):
            value = word.partition("=")[2] if "=" in word else (args[i] if i < len(args) else "")
            i += 0 if "=" in word else 1
        elif word.startswith("--"):
            if word in _LONG_VALUE:
                i += 1  # its value is not an option
            continue
        elif word.startswith("-") and len(word) > 1:
            letters = word[1:]
            for at, letter in enumerate(letters):
                if letter not in _SHORT_VALUE:
                    continue
                rest = letters[at + 1 :]
                if rest.startswith("="):
                    rest = rest[1:]
                if not rest:
                    rest = args[i] if i < len(args) else ""
                    i += 1
                if letter == "n":
                    value = rest
                elif letter == "p" and rest == "no:xdist":
                    said = False
                break
        if value is not None:
            found = _count(value)
            if found is not None:
                said = found
    return said


def _addopts_value(raw: object) -> list[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
    if isinstance(raw, list) and all(isinstance(word, str) for word in raw):
        return list(raw)
    return []


def _ini(path: Path, section: str) -> list[str] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding="utf-8")
    if not parser.has_section(section):
        return None
    return _addopts_value(parser.get(section, "addopts", fallback=""))


def _toml(path: Path, tables: Sequence[tuple[str, ...]]) -> list[str] | None:
    with path.open("rb") as f:
        data = tomllib.load(f)
    for keys in tables:
        table: object = data
        for key in keys:
            table = table.get(key) if isinstance(table, dict) else None
        if isinstance(table, dict):
            return _addopts_value(table.get("addopts", []))
    return None


def _read(path: Path) -> list[str] | None:
    """The `addopts` of the config file `path`, None when it is not pytest's config."""
    name = path.name
    if name == "pyproject.toml":
        return _toml(path, [("tool", "pytest", "ini_options"), ("tool", "pytest")])
    if name.endswith(".toml"):
        return _toml(path, [("pytest",)])
    if name in ("pytest.ini", ".pytest.ini"):
        return _ini(path, "pytest") or []
    if name.endswith(".cfg"):
        return _ini(path, "tool:pytest")
    return _ini(path, "pytest")


def _given_config(args: Sequence[str]) -> str | None:
    for i, word in enumerate(args):
        if word == "--":
            return None
        if word in ("-c", "--config-file") and i + 1 < len(args):
            return args[i + 1]
        if word.startswith("--config-file="):
            return word.partition("=")[2]
        if word.startswith("-c") and len(word) > 2 and not word.startswith("--"):
            return word[2:]
    return None


def _ancestor(args: Sequence[str], cwd: Path) -> Path:
    paths = []
    for word in args:
        if word == "--":
            break
        if word.startswith("-"):
            continue
        path = cwd / word.split("::", 1)[0]
        if path.exists():
            paths.append(path if path.is_dir() else path.parent)
    if not paths:
        return cwd
    return Path(os.path.commonpath([str(p.resolve()) for p in paths]))


def config_addopts(args: Sequence[str], cwd: Path) -> list[str]:
    """The `addopts` of the config file pytest run with `args` in `cwd` would read; empty when
    there is none, or it cannot be read."""
    try:
        given = _given_config(args)
        if given is not None:
            return _read(cwd / given) or []
        directory = _ancestor(args, cwd)
        for folder in (directory, *directory.parents):
            for name in _CONFIGS:
                path = folder / name
                if path.is_file():
                    found = _read(path)
                    if found is not None:
                        return found
    except (OSError, ValueError, UnicodeError, configparser.Error, tomllib.TOMLDecodeError):
        return []
    return []


def run_starts_workers(args: Sequence[str], env: Mapping[str, str], cwd: Path) -> bool:
    """Whether pytest run with `args` in `cwd`, under `env`, starts xdist workers."""
    try:
        extra = shlex.split(env.get("PYTEST_ADDOPTS", ""))
    except ValueError:
        extra = []
    return in_args([*config_addopts(args, cwd), *extra, *args]) is True
