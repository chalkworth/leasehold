"""Reading a shell command line for the programs it runs, and matching them against rules: for
the coding-agent hooks and the PATH shims, which run once per command and so import only the
standard library.

`cd api && uv run pytest -x | tail` runs `cd`, `pytest` (through `uv run`) and `tail`; `git
commit -m "run pytest"` runs only `git`. The command is read as the shell would split it,
without running or expanding anything: a program reached only through a variable, an alias, a
script or `eval` is not seen. It is a way to keep agents from piling heavy work onto the
machine, not a boundary.

The rules come from the daemon (`GET /v1/commands`, `served`), which builds them from the
config's `[commands]` (`leasehold.commands`).
"""

from __future__ import annotations

import dataclasses
import os
import re
import shlex
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from leasehold import jobs, xdist

#: Words that end one simple command and start the next.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "|&", "(", ")", ";;", "\n"})
#: Redirections: the word after one is a file, not an argument.
_REDIRECTS = frozenset({">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>", ">|", "<>"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Programs that run the program named after them: for each, its options that take a value as
#: the next word, and how many plain words come before the program (`timeout`'s duration).
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "nohup": (frozenset(), 0),
    "env": (frozenset({"-u", "--unset", "-C", "--chdir"}), 0),
    "command": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "builtin": (frozenset(), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "timeout": (frozenset({"-k", "--kill-after", "-s", "--signal"}), 1),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-t"}), 0),
    "taskset": (frozenset(), 1),
    "chrt": (frozenset(), 1),
    "sudo": (frozenset({"-u", "--user", "-g", "--group", "-C", "-D", "-h", "-p", "-U", "-r"}), 0),
    "xargs": (frozenset({"-n", "-P", "-I", "-L", "-d", "-a", "-s", "-E"}), 0),
    "npx": (frozenset({"-p", "--package"}), 0),
    "bunx": (frozenset({"-p", "--package"}), 0),
    "pnpx": (frozenset({"-p", "--package"}), 0),
}
#: Options of `uv run` that take the next word as their value (`uv run --with X pytest`).
_UV_RUN_VALUES = frozenset(
    {
        "--extra", "--no-extra", "--group", "--no-group", "--only-group", "--env-file",
        "-w", "--with", "--with-editable", "--with-requirements", "--package",
        "--no-editable-package", "--python-platform", "--index", "--default-index", "-i",
        "--index-url", "--extra-index-url", "-f", "--find-links", "--index-strategy",
        "--keyring-provider", "-P", "--upgrade-package", "--upgrade-group", "--resolution",
        "--prerelease", "--prerelease-package", "--fork-strategy", "--exclude-newer",
        "--exclude-newer-package", "--no-sources-package", "--reinstall-package", "--link-mode",
        "-C", "--config-setting", "--config-settings-package", "--no-build-isolation-package",
        "--no-build-package", "--no-binary-package", "--cache-dir", "--refresh-package", "-p",
        "--python", "--color", "--allow-insecure-host", "--directory", "--project",
        "--config-file",
    }
)  # fmt: skip
#: uv's global options, given before its command, that take the next word as their value.
_UV_GLOBAL_VALUES = frozenset(
    {"--color", "--allow-insecure-host", "--directory", "--project", "--config-file",
     "--cache-dir", "--python-preference"}
)  # fmt: skip
#: Python interpreters, free-threaded builds and PyPy included.
_PYTHON = re.compile(r"(python|pypy)(\d(\.\d+){0,2})?t?")
#: Options of `uvx` (`uv tool run`) that take the next word as their value.
_UVX_VALUES = _UV_RUN_VALUES | {"--from"}
#: `<runner> <verb> program ...`: run a project's tool inside its environment.
_RUNNERS = {
    "uv": "run",
    "poetry": "run",
    "pipenv": "run",
    "pdm": "run",
    "hatch": "run",
    "pnpm": "exec",
    "bundle": "exec",
}
_SHELLS = frozenset({"sh", "bash", "dash", "zsh"})
#: Shell words that start a command of their own after them (`for f in x; do pytest $f; done`).
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"})


@dataclass(frozen=True)
class Case:
    """A case of a rule: a command the rule matches whose arguments contain the regular
    expression `args` weighs `weight` instead, a count, or 0 for no lease. `note` says what
    the case is, in words."""

    args: str
    weight: int
    note: str | None = None


@dataclass(frozen=True)
class Rule:
    """`weight` to `max_weight` slots in `pool` for a command that runs one of `programs`
    (basenames) with arguments in which the regular expression `args` is found, unless one of
    its `cases` matches them first. `source` is `config` or `default`: the config's rules are
    tried first."""

    programs: frozenset[str]
    weight: int
    pool: str | None = None
    args: str | None = None
    source: str = "config"
    memory: str | None = None
    #: Why no lease for this rule could ever be granted; None when one can. A command it
    #: matches is refused, not run under another rule's lease.
    never: str | None = None
    #: The size of each process's own thread pool (`leasehold.jobs`): "jobs", the job count;
    #: a count of at least 1; or a share of the job count, "jobs/N" (divided among N workers)
    #: or "jobs/N:K" (and at most K).
    threads: int | str = "jobs"
    #: The most weight a command it matches can use; None, or less than `weight`, is `weight`.
    max_weight: int | None = None
    #: Memory each slot granted counts, a size such as `2g`; None: none declared per slot.
    memory_per_slot: str | None = None
    #: The most weight one lease under the rule could ever be granted, as the daemon serves it;
    #: None when it does not say.
    ceiling: int | None = None
    #: The most an agent hook asks under the rule: what its range alone would be granted on
    #: an idle machine (`admission.alone`), as the daemon serves it; None when it does not say.
    hook_ceiling: int | None = None
    #: Cases tried in order on the arguments of a command the rule matches: the first that
    #: matches gives its weight (`weighed`).
    cases: tuple[Case, ...] = ()
    #: What the rule, or the case it was weighed by, is, in words.
    note: str | None = None

    def __post_init__(self) -> None:
        if self.max_weight is None or self.max_weight < self.weight:
            object.__setattr__(self, "max_weight", self.weight)

    @classmethod
    def of(cls, row: Mapping) -> Rule:
        """The rule a served row describes. A row without `threads` (a daemon older than the
        setting) has `threads` "jobs", and one with a `threads` it does not know has 1; one
        without a `max_weight` that is a count (a daemon older than ranges) asks for its
        `weight` alone. A row with a `memory_per_slot` declares that alone: its `memory` is
        served only for clients from before memory per slot."""
        weight = int(row["weight"])
        per_slot = row.get("memory_per_slot")
        return cls(
            programs=frozenset(row["programs"]), weight=weight, pool=row.get("pool"),
            args=row.get("args"), source=row.get("source", "config"),
            memory=None if per_slot else row.get("memory"), never=row.get("never"),
            threads=_threads(row.get("threads")), max_weight=_count(row.get("max_weight")),
            memory_per_slot=per_slot or None, ceiling=_count(row.get("ceiling")),
            hook_ceiling=_count(row.get("hook_ceiling")),
            cases=tuple(_cases(row.get("case"))), note=row.get("note"),
        )  # fmt: skip

    def matches(self, program: str, args: Sequence[str]) -> bool:
        """Whether `program` with `args` is this rule's. A rule whose `args` is
        `xdist.XDIST_ARGS` matches a pytest run whose own arguments start xdist workers
        (`xdist.in_args`, which reads them one by one); any other `args` is searched in the
        arguments joined by spaces."""
        if program.rsplit("/", 1)[-1] not in self.programs:
            return False
        if self.args == xdist.XDIST_ARGS:
            return xdist.in_args(args) is True
        return self.args is None or re.search(self.args, " ".join(args)) is not None

    def weighed(self, program: str, args: Sequence[str]) -> Rule | None:
        """This rule as it weighs `program` with `args`: None when it does not match them; when
        one of its cases does (the first that does), a rule of the case's weight, `args` and
        `note`, with no cases; else this rule."""
        if not self.matches(program, args):
            return None
        joined = " ".join(args)
        for case in self.cases:
            if re.search(case.args, joined) is not None:
                return dataclasses.replace(
                    self, weight=case.weight, max_weight=case.weight, args=case.args,
                    note=case.note, cases=(), hook_ceiling=None,
                )  # fmt: skip
        return self

    @property
    def key(self) -> str | None:
        """The name a command's outcome is filed under for this rule: its programs, sorted and
        joined by `, `, then ` ~ ` and its `args` when it has them (`xdist` for the built-in
        pytest-xdist rule's: `py.test, pytest ~ xdist`). None for a rule with no programs,
        which no command matched."""
        if not self.programs:
            return None
        names = ", ".join(sorted(self.programs))
        args = "xdist" if self.args == xdist.XDIST_ARGS else self.args
        return f"{names} ~ {args}" if args else names


def _cases(rows: object) -> Iterator[Case]:
    """The cases a served rule lists, leaving out any without a regular expression that
    compiles or a weight of at least 0."""
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get("args"), str):
            continue
        weight = row.get("weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
            continue
        try:
            re.compile(row["args"])
        except re.error:
            continue
        note = row.get("note")
        yield Case(row["args"], weight, note if isinstance(note, str) else None)


def _threads(value: object) -> int | str:
    """A served `threads`: "jobs" when missing, the value when it is "jobs", a count of at
    least 1 or a share ("jobs/N", "jobs/N:K"), and 1 for anything else."""
    if value is None or value == "jobs":
        return "jobs"
    if _count(value) is not None:
        return value
    if jobs.is_share(value):
        return value
    return 1


def _count(value: object) -> int | None:
    """`value` if it is a count of at least 1, else None."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


@dataclass(frozen=True)
class Served:
    """The daemon's answer at `GET /v1/commands`: its rules in order, and what an agent's hook
    does on a busy machine."""

    rules: tuple[Rule, ...]
    wait_s: float
    when_busy: str


def served(answer: Mapping) -> Served:
    return Served(
        rules=tuple(Rule.of(r) for r in answer["rules"]),
        wait_s=float(answer["wait_s"]),
        when_busy=str(answer["when_busy"]),
    )


@dataclass(frozen=True)
class Match:
    rule: Rule
    program: str
    args: tuple[str, ...]


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    lexer.whitespace = " \t\r"  # a newline separates commands, so it is a token
    try:
        tokens = list(lexer)
    except ValueError:  # an unclosed quote: read it as plain words
        return command.split()
    return [part for token in tokens for part in _operators(token)]


#: The shell's operators, longest first: a run of punctuation (`)&&`) is split into them.
_OPERATORS = sorted(_SEPARATORS | _REDIRECTS, key=len, reverse=True)


def _operators(token: str) -> Iterator[str]:
    if not token or not set(token) <= set(";&|()<>\n"):
        yield token
        return
    while token:
        op = next((o for o in _OPERATORS if token.startswith(o)), token[0])
        yield op
        token = token[len(op) :]


#: What a simple command found means: its programs, and how it exits when its words alone say
#: (`true`, `false`, `:`, negated with `!`), else None.
_Found = tuple[list[tuple[str, list[str]]], bool | None]
_LITERAL = {"true": True, ":": True, "false": False}


def _list(tokens: list[str], i: int, depth: int, closer: str | None) -> tuple[_Found, int]:
    """Commands joined by `;`, `&`, `&&`, `||` and newlines, up to `closer`: the programs they
    run, leaving out a part that `&&` after a known failure or `||` after a known success
    skips, and the index after them."""
    found: list[tuple[str, list[str]]] = []
    known: bool | None = None
    op = ""
    while i < len(tokens):
        token = tokens[i]
        if token == closer:
            return (found, known), i + 1
        if token in ("&&", "||"):
            op, i = token, i + 1
            continue
        if token in (";", "&", "\n", ";;") or token in (")", "}"):
            op, i = "", i + 1  # a stray closer is read as a separator
            continue
        (sub, result), i = _pipeline(tokens, i, depth)
        if (op == "&&" and known is False) or (op == "||" and known is True):
            pass  # skipped: the known result stands
        else:
            found += sub
            known = result
        op = ""
    return (found, known), i


def _pipeline(tokens: list[str], i: int, depth: int) -> tuple[_Found, int]:
    """Commands joined by `|`, `!` before them negating the result."""
    found: list[tuple[str, list[str]]] = []
    negate = False
    while i < len(tokens) and tokens[i] == "!":
        negate, i = not negate, i + 1
    results: list[bool | None] = []
    while i < len(tokens):
        (sub, result), i = _command(tokens, i, depth)
        found += sub
        results.append(result)
        if i < len(tokens) and tokens[i] in ("|", "|&"):
            i += 1
            continue
        break
    result = results[-1] if len(results) == 1 else None
    return (found, (not result) if negate and result is not None else result), i


def _command(tokens: list[str], i: int, depth: int) -> tuple[_Found, int]:
    """A group (`( list )`, `{ list; }`) or a simple command."""
    if tokens[i] in ("(", "{"):
        return _list(tokens, i + 1, depth, ")" if tokens[i] == "(" else "}")
    words: list[str] = []
    while i < len(tokens):
        token = tokens[i]
        if token in _SEPARATORS or set(token) <= set(";&|()\n"):
            break
        if token in _REDIRECTS or set(token) <= set("<>&"):
            if words and words[-1].isdigit():
                words.pop()  # the 2 of 2>file: a file descriptor, not an argument
            i += 2  # the redirection and its file
            continue
        words.append(token)
        i += 1
    if not words:
        # Only a stray operator here: step over it, so reading always moves on.
        stray = i < len(tokens) and tokens[i] not in _SEPARATORS
        return ([], None), i + 1 if stray else i
    result = _LITERAL.get(words[0]) if len(words) == 1 else None
    return (list(_unwrap(words, depth)), result), i


def _unwrap(words: list[str], depth: int) -> Iterator[tuple[str, list[str]]]:
    """The program a simple command runs and its arguments, seeing through assignments,
    `time`/`nice`/`env`-like prefixes, project runners (`uv run`), `npx`, `python -m` and
    `sh -c`."""
    while words and _ASSIGNMENT.match(words[0]):
        words = words[1:]
    if not words:
        return
    program, rest = words[0].rsplit("/", 1)[-1], words[1:]
    if words[0] in _KEYWORDS:
        yield from _unwrap(rest, depth)
        return
    if program in _WRAPPERS:
        yield from _unwrap(_after_options(program, rest), depth)
        return
    if program == "uv":
        directory, rest = _uv_options(rest, _UV_GLOBAL_VALUES)
        if rest[:1] == ["run"]:
            more, rest = _uv_options(rest[1:], _UV_RUN_VALUES)
        elif rest[:2] == ["tool", "run"]:
            more, rest = _uv_options(rest[2:], _UVX_VALUES)
        else:
            yield Found(program, rest)
            return
        yield from _within(_join(directory, more), _unwrap(rest, depth))
        return
    if program == "uvx":
        directory, rest = _uv_options(rest, _UVX_VALUES)
        yield from _within(directory, _unwrap(rest, depth))
        return
    if _RUNNERS.get(program) and rest[:1] == [_RUNNERS[program]]:
        yield from _unwrap(_past_options(rest[1:], frozenset()), depth)
        return
    if _PYTHON.fullmatch(program):
        module = _python_module(rest)
        if module is not None:
            yield module
            return
    if program in _SHELLS and depth < 3:
        script = _shell_script(rest)
        if script is not None:
            yield from programs(script, depth=depth + 1)
            return
    yield Found(program, rest)


class Found(tuple):
    """A (program, arguments) pair `programs` found, and the directory it runs in relative
    to the command line's (`uv --directory`), or None for the same one."""

    directory: str | None

    def __new__(cls, program: str, args: list[str], directory: str | None = None) -> Found:
        found = super().__new__(cls, (program, args))
        found.directory = directory
        return found


def _join(outer: str | None, inner: str | None) -> str | None:
    if outer is None or inner is None:
        return outer if inner is None else inner
    return os.path.join(outer, inner)


def _within(
    directory: str | None, found: Iterable[tuple[str, list[str]]]
) -> Iterator[tuple[str, list[str]]]:
    """`found`, each running in `directory` (relative to its own)."""
    for item in found:
        if directory is None:
            yield item
        else:
            yield Found(item[0], item[1], _join(directory, getattr(item, "directory", None)))


def _uv_options(words: list[str], takes: frozenset[str]) -> tuple[str | None, list[str]]:
    """The last `--directory` among uv's options at the start of `words`, and the words past
    them (`_past_options`)."""
    directory = None
    while words and words[0].startswith("-") and words[0] != "--":
        name, eq, value = words[0].partition("=")
        if name == "--directory":
            directory = value if eq else (words[1] if len(words) > 1 else None)
        words = words[2:] if not eq and name in takes else words[1:]
    return directory, words[1:] if words[:1] == ["--"] else words


def _past_options(words: list[str], takes: frozenset[str]) -> list[str]:
    """`words` past a runner's options, skipping the value of each option in `takes`, and
    past a `--` that ends them."""
    while words and words[0].startswith("-"):
        if words[0] == "--":
            return words[1:]
        words = words[2:] if words[0] in takes else words[1:]
    return words


#: Python's options that take the next word as their value (`python -X dev -m pytest`).
_PYTHON_VALUES = frozenset("XW")


def _python_module(words: list[str]) -> tuple[str, list[str]] | None:
    """The module and its arguments when Python runs one (`-m`), past Python's own options;
    None when it runs a script, a command (`-c`) or its input."""
    i = 0
    while i < len(words):
        word = words[i]
        i += 1
        if word == "--check-hash-based-pycs":
            i += 1
            continue
        if not word.startswith("-") or word in ("-", "--") or word.startswith("--"):
            return None
        letters = word[1:]
        for at, letter in enumerate(letters):
            rest = letters[at + 1 :]
            if letter == "m":
                if rest:
                    return rest, words[i:]
                return (words[i], words[i + 1 :]) if i < len(words) else None
            if letter == "c":
                return None
            if letter in _PYTHON_VALUES:
                if not rest:
                    i += 1
                break
    return None


def _after_options(wrapper: str, words: list[str]) -> list[str]:
    """`words` from the program `wrapper` runs: past its options (and the values of those that
    take one), assignments (`env A=1`), and the plain words it takes first."""
    takes, plain = _WRAPPERS[wrapper]
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--":
            i += 1
            break
        if word.startswith("-") and len(word) > 1:
            i += 2 if word in takes else 1
        elif _ASSIGNMENT.match(word) and wrapper in ("env", "sudo"):
            i += 1
        else:
            break
    return words[i + plain :]


def _shell_script(words: list[str]) -> str | None:
    """The script of `sh -c SCRIPT` (also `bash -lc`, `-ec`, `-o pipefail -c`), or None when
    the shell runs a file or reads its input."""
    i = 0
    while i < len(words):
        word = words[i]
        if word in ("-o", "+o", "-O", "+O"):
            i += 2
            continue
        if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
            return words[i + 1] if i + 1 < len(words) else None
        if not word.startswith(("-", "+")):
            return None
        i += 1
    return None


def programs(command: str, *, depth: int = 0) -> Iterator[tuple[str, list[str]]]:
    """Every (program, arguments) a shell command line runs, as far as reading it shows. A
    command a literal `false &&` or `true ||` keeps from running is left out."""
    (found, _), _ = _list(_tokens(command), 0, depth, None)
    yield from found


def match(rules: Iterable[Rule], command: str) -> Match | None:
    """The rule `command` takes a lease under (`explain`); None when no rule matches, or when
    the one that does weighs it 0, which is no lease."""
    return match_programs(rules, list(programs(command)))


def explain(rules: Iterable[Rule], command: str) -> Match | None:
    """The rule that weighs `command`: the heaviest match among the config's rules, else among
    the built-in ones, each weighed by its first case that matches (`Rule.weighed`), by
    `weight` (the least of a range), the first of equals; None when no rule matches. A weight
    of 0 is a case that takes no lease."""
    return explain_programs(rules, list(programs(command)))


#: Programs that are pytest, for reading their configuration (`xdist`).
_PYTEST = frozenset({"pytest", "py.test"})


def threads(
    rules: Iterable[Rule],
    found: Sequence[tuple[str, Sequence[str]]],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | Callable[[], Path] | None = None,
) -> int | str | None:
    """The thread count for a command line running the programs `found`, as a `threads`
    setting (`jobs.thread_count`): the smallest any of its programs allows, so none runs more
    threads per process than its rule gives. None when no rule matches any of them and no
    pytest run on the line starts workers. "jobs/W:K" is the job count divided among W
    workers, and at most K.

    A pytest run that starts xdist workers, whatever rule it matched or none, allows "jobs/W"
    for W workers and 1 for `-n auto` or `logical`; its rule's own `threads` can only lower
    that. Its arguments say how many it starts (`xdist.workers_in_args`); with `env` and `cwd`,
    so do `PYTEST_ADDOPTS` and its config's `addopts` (`xdist.workers`). `cwd` is a directory,
    or a function giving it, called only for pytest."""
    rules = list(rules)
    chosen: list[int | str] = []
    matched = False
    for item in found:
        program, args = item
        own = match_programs(rules, [(program, args)])
        if own is not None:
            matched = True
            if not (own.rule.source == "default" and own.rule.args == xdist.XDIST_ARGS):
                chosen.append(own.rule.threads)  # the built-in xdist rule's is "auto"'s
        if program.rsplit("/", 1)[-1] not in _PYTEST:
            continue
        if env is not None and cwd is not None:
            here = _directory(cwd, getattr(item, "directory", None))
            workers = xdist.workers(args, env, here)
        else:
            workers = xdist.workers_in_args(args)
        if workers == "auto":
            chosen.append(1)
        elif workers is not None:
            chosen.append(f"jobs/{workers}")
    if not matched and not chosen:
        return None
    return _fewest(chosen)


def _fewest(chosen: Sequence[int | str]) -> int | str:
    """The `threads` setting no larger than any of `chosen`: the most workers any shares the
    job count among, and the smallest count any gives."""
    shares: list[int] = []
    counts: list[int] = []
    for value in chosen:
        if isinstance(value, int):
            counts.append(value)
        elif value.startswith("jobs/"):
            share, _, most = value[5:].partition(":")
            shares.append(int(share))
            if most:
                counts.append(int(most))
    if counts and min(counts) == 1:
        return 1
    if not shares:
        return min(counts) if counts else "jobs"
    return f"jobs/{max(shares)}" + (f":{min(counts)}" if counts else "")


def _directory(cwd: Path | Callable[[], Path], relative: str | None) -> Callable[[], Path]:
    """The directory a program runs in: `relative` to the command line's `cwd`."""

    def directory() -> Path:
        base = cwd() if callable(cwd) else cwd
        return base if relative is None else Path(os.path.normpath(base / relative))

    return directory


def match_programs(
    rules: Iterable[Rule], found: Sequence[tuple[str, Sequence[str]]]
) -> Match | None:
    """`match` for programs already known: (program, arguments) pairs."""
    best = explain_programs(rules, found)
    return None if best is None or best.rule.weight == 0 else best


def explain_programs(
    rules: Iterable[Rule], found: Sequence[tuple[str, Sequence[str]]]
) -> Match | None:
    """`explain` for programs already known: (program, arguments) pairs."""
    rules = list(rules)
    for source in ("config", "default"):
        best: Match | None = None
        for rule in rules:
            if rule.source != source:
                continue
            for program, args in found:
                own = rule.weighed(program, args)
                if own is not None and (best is None or own.weight > best.rule.weight):
                    best = Match(own, program, tuple(args))
        if best is not None:
            return best
    return None
