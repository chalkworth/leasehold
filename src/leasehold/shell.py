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

import re
import shlex
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

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
class Rule:
    """`weight` slots in `pool` for a command that runs one of `programs` (basenames) with
    arguments in which the regular expression `args` is found. `source` is `config` or
    `default`: the config's rules are tried first."""

    programs: frozenset[str]
    weight: int
    pool: str | None = None
    args: str | None = None
    source: str = "config"
    memory: str | None = None

    @classmethod
    def of(cls, row: Mapping) -> Rule:
        return cls(
            programs=frozenset(row["programs"]), weight=int(row["weight"]), pool=row.get("pool"),
            args=row.get("args"), source=row.get("source", "config"), memory=row.get("memory"),
        )  # fmt: skip

    def matches(self, program: str, args: Sequence[str]) -> bool:
        if program.rsplit("/", 1)[-1] not in self.programs:
            return False
        return self.args is None or re.search(self.args, " ".join(args)) is not None


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
    if _RUNNERS.get(program) and rest[:1] == [_RUNNERS[program]]:
        rest = rest[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        yield from _unwrap(rest, depth)
        return
    if re.fullmatch(r"python(\d(\.\d+)?)?", program) and rest[:1] == ["-m"] and len(rest) > 1:
        yield rest[1], rest[2:]
        return
    if program in _SHELLS and depth < 3:
        script = _shell_script(rest)
        if script is not None:
            yield from programs(script, depth=depth + 1)
            return
    yield program, rest


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
    """The rule `command` takes a lease under: the heaviest match among the config's rules,
    else among the built-in ones; None when no rule matches."""
    return match_programs(rules, list(programs(command)))


def match_programs(
    rules: Iterable[Rule], found: Sequence[tuple[str, Sequence[str]]]
) -> Match | None:
    """`match` for programs already known: (program, arguments) pairs."""
    rules = list(rules)
    for source in ("config", "default"):
        best: Match | None = None
        for rule in rules:
            if rule.source != source:
                continue
            for program, args in found:
                if rule.matches(program, args) and (best is None or rule.weight > best.rule.weight):
                    best = Match(rule, program, tuple(args))
        if best is not None:
            return best
    return None
