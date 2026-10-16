"""Whether a pytest run starts pytest-xdist worker processes, and how many. The answer comes
from the run's arguments, from `PYTEST_ADDOPTS`, and from the `addopts` of the config file
pytest reads.

pytest reads the config's `addopts` first, then `PYTEST_ADDOPTS`, then the command line. When
an option is repeated, the last one wins. The count follows pytest-xdist 3:

- Workers start when the last `-n` or `--numprocesses` is `auto`, `logical` or a count above
  0. `--maxprocesses M` limits the count to M, and `auto` to the smaller of the job count and
  M.
- `--tx` starts workers only with `--dist` (other than `no`) or `-d`. `4*popen` counts 4.
  `-n 0` clears `--tx`.
- `--collect-only` starts none, and neither does `--pdb`.
- The last of `-p no:xdist`, `-p xdist` and `-p xdist.plugin` decides whether xdist loads.

Words are read the way argparse reads them. An option that takes a value takes the next word,
unless that word looks like an option. `-vn4` means `-v -n 4`. Nothing after `--` is an
option. `@FILE` stands for the lines of FILE.

The config file is found the way pytest 9 finds it (`_pytest.config.findpaths`). The last
`-c` or `--config-file`, from `PYTEST_ADDOPTS` or the arguments, names it. Without one, the
search starts at the common ancestor of the path arguments that exist, or at the working
directory when there are none, and goes up. In each directory it takes the first of
`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `pyproject.toml`, `tox.ini` and
`setup.cfg` that holds pytest's configuration. `-o addopts=...` replaces the file's `addopts`.

Only regular files of at most 1 MiB are read. A file that cannot be read or parsed adds no
`addopts`. ini files are parsed with `configparser`, so unusual syntax can read differently
than it does in pytest's `iniconfig`.

Standard library only: the shims and the agent hooks import it.
"""

from __future__ import annotations

import configparser
import contextlib
import os
import re
import shlex
import stat
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

#: The built-in xdist rule's `args`, as a regular expression for clients that match a rule's
#: arguments as one string; this module decides for a rule with exactly this `args`
#: (`in_args`).
XDIST_ARGS = r"(^|\s)(-n\s*=?\s*|--numprocesses(=|\s+))(auto|logical|[1-9][0-9]*)(\s|$)"

#: pytest's, pytest-xdist's and common plugins' short options that take a value.
_SHORT_VALUE = frozenset("kmrWcpon")
#: Long options that take a value (the next word, or after `=`).
_LONG_VALUE = frozenset(
    {
        "--maxfail", "--pdbcls", "--capture", "--lfnf", "--last-failed-no-failures",
        "--durations", "--durations-min", "--verbosity", "--report-chars", "--tb",
        "--show-capture", "--color", "--code-highlight", "--pastebin", "--junitxml",
        "--junit-xml", "--junitprefix", "--junit-prefix", "--pythonwarnings", "--max-warnings",
        "--ignore", "--ignore-glob", "--deselect", "--confcutdir", "--import-mode",
        "--doctest-report", "--doctest-glob", "--config-file", "--rootdir", "--basetemp",
        "--override-ini", "--assert", "--log-level", "--log-format", "--log-date-format",
        "--log-cli-level", "--log-cli-format", "--log-cli-date-format", "--log-file",
        "--log-file-mode", "--log-file-level", "--log-file-format", "--log-file-date-format",
        "--log-auto-indent", "--log-disable",
        # pytest-xdist
        "--numprocesses", "--maxprocesses", "--max-worker-restart", "--dist", "--tx", "--px",
        "--rsyncdir", "--rsyncignore", "--testrunuid", "--maxschedchunk",
        # pytest-cov, pytest-timeout
        "--cov-report", "--cov-config", "--cov-fail-under", "--cov-context", "--cov-precision",
        "--timeout", "--timeout-method",
    }
)  # fmt: skip
#: Long options whose value is optional (`nargs="?"`): they take the next word only when it
#: does not look like an option.
_LONG_OPTIONAL = frozenset({"--cache-show", "--debug", "--cov"})
_CONFIGS = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml",
            "tox.ini", "setup.cfg")  # fmt: skip
#: The most of a config file read; a larger one counts as unreadable.
MAX_CONFIG_BYTES = 1 << 20
#: The longest `-n` count read; a longer one is not a count.
_MAX_DIGITS = 6
_NEGATIVE = re.compile(r"-\d+$|-\d*\.\d+$")
#: The most words `@file` arguments expand to.
MAX_WORDS = 10_000
#: How deep `@file` arguments may name further `@file` arguments.
_MAX_FILE_DEPTH = 8
#: pytest-xdist's `--tx` spec with a count: `4*popen`.
_TX_COUNT = re.compile(r"([0-9]+)\*")
#: The `-p` values that load pytest-xdist's plugin, and the one that blocks it.
_XDIST_ON = frozenset({"xdist", "xdist.plugin"})
_XDIST_OFF = "no:xdist"
#: Long options that collect tests only: pytest-xdist then starts no workers.
_COLLECT_ONLY = frozenset({"--collect-only", "--collectonly", "--co"})


class _Unreadable(Exception):
    """A config file pytest would fail on, or that is not read."""


def _option_like(word: str) -> bool:
    """Whether argparse reads `word` as an option rather than a value. A word with a space is
    one only when it starts with a short option (`-n 3`), or a long one and `=`."""
    if not word.startswith("-") or word == "-" or _NEGATIVE.match(word):
        return False
    if " " not in word:
        return True
    if word.startswith("--"):
        name, eq, _ = word.partition("=")
        return bool(eq) and " " not in name
    return word[1].isalpha()


def _count(value: str) -> int | str | None:
    """An `-n` value as pytest-xdist reads it (`parse_numprocesses`): "auto" for `auto` and
    `logical`, else a count, which `int` reads with any surrounding whitespace and a leading
    `+`. None for anything else, or a count of more than `_MAX_DIGITS` digits."""
    if value in ("auto", "logical"):
        return "auto"
    digits = value.strip()
    digits = digits[1:] if digits.startswith("+") else digits
    if digits.isascii() and digits.isdigit() and len(digits) <= _MAX_DIGITS:
        return int(digits)
    return None


@dataclass
class _Read:
    """What a list of pytest words sets: the xdist options, `--pdb` and `--collect-only`, the
    last config file and `--rootdir` named, the `-o` overrides, and the free arguments.
    `plugin` is the last `-p` that loads or blocks xdist; `seen` whether any xdist option or
    such a `-p` was given; `missing` whether pytest refuses the words: an option that needs a
    value had none, or an `@file` names itself."""

    numprocesses: int | str | None = None
    maxprocesses: int | None = None
    dist: str = "no"
    distload: bool = False
    tx: list[str] = field(default_factory=list)
    usepdb: bool = False
    collectonly: bool = False
    missing: bool = False
    plugin: bool | None = None
    seen: bool = False
    config: str | None = None
    rootdir: str | None = None
    overrides: list[str] = field(default_factory=list)
    free: list[str] = field(default_factory=list)


def _expand(words: Sequence[str], cwd: Path | Callable[[], Path] | None) -> list[str] | None:
    """`words` with each `@file` replaced by the file's lines, one word each, as argparse does
    for pytest (`fromfile_prefix_chars="@"`). A file that is not a regular file of at most
    `MAX_CONFIG_BYTES`, or cannot be read, leaves its word as it is. None when a file names
    itself, directly or through others: pytest fails on that. At most `MAX_WORDS` words come
    out."""
    out: list[str] = []
    base: list[Path] = []

    def expand(items: Sequence[str], open_files: frozenset[str]) -> None:
        for word in items:
            if len(out) >= MAX_WORDS:
                return
            if not word.startswith("@") or len(open_files) >= _MAX_FILE_DEPTH:
                out.append(word)
                continue
            try:
                if not base:
                    base.append(Path.cwd() if cwd is None else cwd() if callable(cwd) else cwd)
                path = _absolute(base[0], word[1:])
                key = os.path.realpath(path)
                if key in open_files:
                    raise _Cycle
                lines = _text(path).splitlines()
            except (_Unreadable, OSError, ValueError):
                out.append(word)
                continue
            expand(lines, open_files | {key})

    try:
        expand(words, frozenset())
    except _Cycle:
        return None
    return out


class _Cycle(Exception):
    """An `@file` that names itself."""


def _read_words(words: Sequence[str], cwd: Path | Callable[[], Path] | None = None) -> _Read:
    expanded = _expand(words, cwd)
    if expanded is None:
        return _Read(missing=True)
    words = expanded
    read = _Read()
    i = 0

    def value_after() -> str | None:
        nonlocal i
        if i < len(words) and not _option_like(words[i]):
            i += 1
            return words[i - 1]
        return None

    def needed() -> str | None:
        value = value_after()
        read.missing = read.missing or value is None
        return value

    def take(name: str, value: str | None) -> None:
        if value is None:
            return
        if name in ("-n", "--numprocesses"):
            found = _count(value)
            if found is not None:
                read.numprocesses, read.seen = found, True
        elif name == "--maxprocesses":
            limit = _count(value)
            read.maxprocesses = limit if isinstance(limit, int) else None
        elif name == "--dist":
            read.dist, read.seen = value, True
        elif name == "--tx":
            read.tx.append(value)
            read.seen = True
        elif name in ("-c", "--config-file"):
            read.config = value
        elif name == "--rootdir":
            read.rootdir = value
        elif name in ("-o", "--override-ini"):
            read.overrides.append(value)

    def plugin(value: str | None) -> None:
        """`-p` as pytest's plugin manager reads it: only `-p NAME` and `-pNAME`."""
        name = (value or "").strip()
        if name in _XDIST_ON or name == _XDIST_OFF:
            read.plugin, read.seen = name != _XDIST_OFF, True

    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            read.free += words[i:]
            break
        if not _option_like(word):
            read.free.append(word)
        elif word.startswith("--"):
            name, eq, attached = word.partition("=")
            if name == "--pdb":
                read.usepdb = True
            elif name in _COLLECT_ONLY:
                read.collectonly = True
            elif eq:
                take(name, attached)
            elif name in _LONG_VALUE:
                take(name, needed())
            elif name in _LONG_OPTIONAL:
                take(name, value_after())
        elif word == "-p":
            plugin(needed())
        elif word.startswith("-p"):
            plugin(word[2:])
        else:
            letters = word[1:]
            for at, letter in enumerate(letters):
                if letter == "d":
                    read.distload, read.seen = True, True
                if letter not in _SHORT_VALUE:
                    continue
                rest = letters[at + 1 :]
                if rest.startswith("="):
                    rest = rest[1:]
                take(f"-{letter}", rest if rest else needed())
                break
    return read


def _workers(read: _Read) -> int | str | None:
    """How many xdist workers pytest starts, as pytest-xdist 3 decides it
    (`pytest_cmdline_main`, `pytest_configure`): a count, "auto" for `-n auto` or
    `logical`, or None when it starts none."""
    if read.plugin is False:
        return None
    numprocesses = read.numprocesses
    dist = "load" if read.distload else read.dist
    tx = list(read.tx)
    if numprocesses == "auto" and read.usepdb:
        numprocesses, dist = 0, "no"
    if numprocesses:
        dist = "load" if dist == "no" else dist
        if read.maxprocesses and numprocesses == "auto":
            numprocesses = int(read.maxprocesses)  # min(the job count, M): `jobs/M` gives that
        elif read.maxprocesses:
            numprocesses = min(int(numprocesses), int(read.maxprocesses))
        tx = ["popen"] * (1 if numprocesses == "auto" else int(numprocesses))
    if numprocesses == 0:
        dist, tx = "no", []
    if read.collectonly or dist == "no" or not tx or read.usepdb or read.missing:
        return None  # collecting only, not distributing, or a usage error
    if numprocesses:
        return numprocesses
    return sum(int(m[1]) if (m := _TX_COUNT.match(spec)) and len(m[1]) <= _MAX_DIGITS else 1
               for spec in tx)  # fmt: skip


def workers_in_args(args: Sequence[str]) -> int | str | None:
    """How many xdist workers `args` alone start (`_workers`): a count, "auto", or None."""
    return _workers(_read_words(args))


def in_args(args: Sequence[str]) -> bool | None:
    """Whether `args` (pytest's arguments, one element each) start xdist workers (`_workers`);
    None when they give no xdist option."""
    read = _read_words(args)
    return (_workers(read) is not None) if read.seen else None


def _text(path: Path) -> str:
    """`path`'s text: a regular file of at most `MAX_CONFIG_BYTES`, else `_Unreadable`."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise _Unreadable(path)
        with path.open("rb") as f:
            data = f.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise _Unreadable(path)
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise _Unreadable(path) from exc


def _words(raw: object, *, native: bool = False) -> list[str]:
    """An `addopts` value as words: a string split as a shell splits it, or a list. A TOML
    scalar of another type is a string in ini mode, and pytest fails on it in TOML mode."""
    if isinstance(raw, list):
        return [str(word) for word in raw]
    if isinstance(raw, str) or not native:
        return shlex.split(str(raw))
    raise ValueError(raw)


def _ini(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    return parser


def _toml(text: str) -> dict:
    return tomllib.loads(text)


def _load(path: Path) -> list[str] | None:
    """The `addopts` of `path` as pytest reads its config (`load_config_dict_from_file`): None
    when the file holds no pytest configuration, `_Unreadable` when pytest would fail on it."""
    try:
        if path.suffix == ".ini":
            parser = _ini(_text(path))
            if parser.has_section("pytest"):
                return _words(parser.get("pytest", "addopts", fallback=""))
            return [] if path.name in ("pytest.ini", ".pytest.ini") else None
        if path.suffix == ".cfg":
            parser = _ini(_text(path))
            if parser.has_section("tool:pytest"):
                return _words(parser.get("tool:pytest", "addopts", fallback=""))
            if parser.has_section("pytest"):
                raise _Unreadable(path)  # pytest refuses [pytest] in setup.cfg
            return None
        if path.suffix == ".toml":
            data = _toml(_text(path))
            if path.name in ("pytest.toml", ".pytest.toml"):
                table = data.get("pytest", {})
                return (
                    _words(table.get("addopts", []), native=True) if isinstance(table, dict) else []
                )
            tool = data.get("tool", {})
            pytest_table = tool.get("pytest", {}) if isinstance(tool, dict) else {}
            if not isinstance(pytest_table, dict):
                return None
            native = {k: v for k, v in pytest_table.items() if k != "ini_options"}
            ini = pytest_table.get("ini_options")
            if native and ini:
                raise _Unreadable(path)  # pytest refuses both at once
            if native:
                return _words(native.get("addopts", []), native=True)
            if isinstance(ini, dict):
                return _words(ini.get("addopts", ""))
            return None
    except (configparser.Error, tomllib.TOMLDecodeError, ValueError, AttributeError) as exc:
        raise _Unreadable(path) from exc
    return None


def _absolute(cwd: Path, word: str) -> Path:
    """`word` as a path from `cwd`, made absolute without resolving symbolic links."""
    return Path(os.path.normpath(os.path.join(cwd, word)))


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except (OSError, ValueError):
        return False


def _common_ancestor(cwd: Path, paths: Sequence[Path]) -> Path:
    """`get_common_ancestor`: the deepest directory holding every path that exists."""
    common: Path | None = None
    for path in paths:
        if not _exists(path):
            continue
        if common is None:
            common = path
        elif common in path.parents or path == common:
            continue
        elif path in common.parents:
            common = path
        else:
            with contextlib.suppress(ValueError):
                common = Path(os.path.commonpath((str(path), str(common))))
    if common is None:
        return cwd
    return common.parent if common.is_file() else common


def _locate(starts: Sequence[Path]) -> tuple[Path | None, Path | None, list[str]]:
    """`locate_config`: the directory and file of pytest's config found from `starts`
    upwards, and its `addopts`; a `pyproject.toml` seen on the way when there is none."""
    first_pyproject: Path | None = None
    for start in starts:
        for base in (start, *start.parents):
            for name in _CONFIGS:
                path = base / name
                if not path.is_file():
                    continue
                if name == "pyproject.toml" and first_pyproject is None:
                    first_pyproject = path
                found = _load(path)
                if found is not None:
                    return base, path, found
    if first_pyproject is not None:
        return first_pyproject.parent, first_pyproject, []
    return None, None, []


def locate(
    free: Sequence[str], config: str | None, cwd: Path, rootdir: str | None = None
) -> tuple[Path | None, list[str]]:
    """The config file pytest run in `cwd` with the free arguments `free` (and `-c config`,
    `--rootdir rootdir`) reads, and its `addopts` (`determine_setup`). `_Unreadable` when the
    file cannot be read or pytest would refuse it."""
    if config:
        path = _absolute(cwd, config)
        return path, _load(path) or []
    dirs = []
    for word in free:
        if word.startswith("-"):
            continue
        path = _absolute(cwd, word.split("::", 1)[0])
        if _exists(path):
            dirs.append(path if path.is_dir() else path.parent)
    ancestor = _common_ancestor(cwd, dirs)
    found_in, path, addopts = _locate([ancestor])
    if found_in is None and rootdir is None:
        has_setup = any((d / "setup.py").is_file() for d in (ancestor, *ancestor.parents))
        if not has_setup and dirs != [ancestor]:
            _, path, addopts = _locate(dirs)
    return path, addopts


def _overridden(overrides: Sequence[str]) -> list[str] | None:
    """The `addopts` the last `-o addopts=...` sets; None when none does."""
    found = None
    for item in overrides:
        key, eq, value = item.partition("=")
        if eq and key == "addopts":
            found = value
    return None if found is None else shlex.split(found)


def config_addopts(args: Sequence[str], cwd: Path | Callable[[], Path]) -> list[str]:
    """The `addopts` pytest run with `args` (its arguments and `PYTEST_ADDOPTS`'s words) in
    `cwd` takes from its config: the last `-o addopts=...`, else the config file's (`locate`).
    Empty when there is none or it cannot be read; `cwd` is called only when needed."""
    try:
        read = _read_words(args, cwd)
        addopts = _overridden(read.overrides)
        if addopts is None:
            directory = cwd() if callable(cwd) else cwd
            addopts = locate(read.free, read.config, directory, read.rootdir)[1]
    except (_Unreadable, OSError, ValueError, RecursionError):
        return []
    return addopts


def workers(
    args: Sequence[str], env: Mapping[str, str], cwd: Path | Callable[[], Path]
) -> int | str | None:
    """How many xdist workers pytest run with `args` in `cwd` (or the directory `cwd()` gives,
    asked only when needed), under `env`, starts: a count, "auto", or None for none. Reads the
    config's `addopts`, then `PYTEST_ADDOPTS`, then `args`. A `PYTEST_ADDOPTS` or config that
    cannot be read adds nothing."""
    try:
        extra = shlex.split(env.get("PYTEST_ADDOPTS", ""))
    except ValueError:
        extra = []
    addopts = config_addopts([*extra, *args], cwd)
    return _workers(_read_words([*addopts, *extra, *args], cwd))


def run_starts_workers(
    args: Sequence[str], env: Mapping[str, str], cwd: Path | Callable[[], Path]
) -> bool:
    """Whether pytest run with `args` in `cwd` under `env` starts xdist workers (`workers`)."""
    return workers(args, env, cwd) is not None
