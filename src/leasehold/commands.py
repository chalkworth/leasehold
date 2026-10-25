"""The `[commands]` config table: which commands are heavy enough to take a lease, for the
coding-agent hooks and the PATH shims (`leasehold.shell` matches commands against these rules).

A rule names programs (by basename) and, optionally, a regular expression their arguments must
contain. Its cases, tried in order on the arguments of a command it matches, weigh what they
recognise differently: `pytest --version` takes no lease (weight 0), a named test weighs 1.
Arguments no case recognises weigh the rule's weight. Rules from the config are tried before
the built-in ones (`default_rules`), so a config can re-weigh a program the defaults name;
among the rules of one source the heaviest match wins (by the least of a range), each weighed
by its case. The built-in rules come in two weights, `heavy_weight` for builds and test runs
and `light_weight` for package installs and bundlers, both set in `[commands]`.

A rule's `weight` is a count (`4`) or a range (`"2-4"`, `leasehold.weights`): the least its
command needs and the most it can use. A command asking a range is granted a size between them
when it is admitted, and keeps it.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from leasehold import shell, weights, xdist
from leasehold.model import Name
from leasehold.units import CAP_OFF, check_cap, declared_to_bytes


class CommandCase(BaseModel):
    """A case of a rule: a command the rule matches whose arguments contain `args` weighs
    `weight` instead, a count, or 0 for no lease. `note` says what the case is, for
    `leasehold commands`."""

    model_config = ConfigDict(extra="forbid")

    #: A regular expression searched in the program's arguments, joined by spaces.
    args: str
    weight: StrictInt = Field(ge=0)
    note: str | None = None

    @field_validator("args")
    @classmethod
    def _compiles(cls, value: str) -> str:
        return _regex(value)


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"{value!r} is not a regular expression: {exc}") from None
    return value


class CommandRule(BaseModel):
    """`weight` to `max_weight` slots (in `pool`, else the kind's) for a command that runs one
    of `programs` with arguments in which `args` is found, held to `memory` (or
    `memory_per_slot` for each slot granted) when contained, and killed whole past
    `memory_max` when given, each of its processes given `threads` threads (`leasehold.jobs`).
    In the config, `weight` is a count or a `"MIN-MAX"` range, which sets both."""

    model_config = ConfigDict(extra="forbid")

    programs: list[str] = Field(min_length=1)
    #: A regular expression searched in the program's arguments, joined by spaces.
    args: str | None = None
    #: The least weight the command needs.
    weight: int = Field(ge=1)
    #: The most it can use, from a range; None is `weight`.
    max_weight: int | None = None
    pool: Name | None = None
    #: Memory the command is held to when contained (`MemoryHigh`), a size such as `4g`.
    memory: str | None = None
    #: Memory each slot it is granted counts, and holds it to when contained, a size such as
    #: `2g`: what a command whose memory grows with its size declares instead of `memory`.
    memory_per_slot: str | None = None
    #: The hard cap on the command's memory when contained (`MemoryMax`), past which the kernel
    #: kills it whole: a size such as `8g`, a share of the machine's memory such as `50%`, or
    #: `off`. None: `[commands] memory_max`.
    memory_max: str | None = None
    #: The size of each process's own thread pool (OpenMP, BLAS and the like): "jobs", the
    #: lease's job count, or a count of at least 1 (never more than the job count).
    threads: Literal["jobs"] | Annotated[StrictInt, Field(ge=1)] = "jobs"
    #: Cases tried in order on the arguments of a command the rule matches; the first that
    #: matches gives its weight. None matching: the rule's own.
    case: list[CommandCase] = Field(default_factory=list)
    #: What the rule is, for `leasehold commands`.
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _range(cls, data: Any) -> Any:
        """`weight = "MIN-MAX"` (or `"N"`) as `weight` MIN and `max_weight` MAX."""
        if not isinstance(data, dict):
            return data
        if "max_weight" in data:
            raise ValueError('max_weight is not a key: give a range as weight = "2-4"')
        weight = data.get("weight")
        if isinstance(weight, str):
            least, most = weights.parse(weight)
            data = {**data, "weight": least, "max_weight": most}
        return data

    @model_validator(mode="after")
    def _one_memory(self) -> CommandRule:
        if self.memory is not None and self.memory_per_slot is not None:
            raise ValueError("memory and memory_per_slot are both set: give one or the other")
        return self

    @field_validator("memory", "memory_per_slot")
    @classmethod
    def _size(cls, value: str | None) -> str | None:
        if value is not None:
            declared_to_bytes(value)
        return value

    @field_validator("memory_max")
    @classmethod
    def _cap(cls, value: str | None) -> str | None:
        return None if value is None else check_cap(value)

    @property
    def most(self) -> int:
        """The most weight the command can use."""
        return self.weight if self.max_weight is None else self.max_weight

    @field_validator("args")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        return None if value is None else _regex(value)


def _rule(
    programs: str,
    weight: int,
    args: str | None = None,
    threads: int | str = "jobs",
    cases: tuple[CommandCase, ...] = (),
) -> CommandRule:
    return CommandRule(
        programs=programs.split(), weight=weight, args=args, threads=threads, case=list(cases)
    )


def _word(options: str) -> str:
    """Any of `options` (alternatives) as a whole argument."""
    return rf"(?:^| )(?:{options})(?: |$)"


def _before_dashes(options: str) -> str:
    """Any of `options` as a whole argument before a `--`, after which arguments are not the
    program's own."""
    return rf"^(?!--(?: |$))(?:(?! --(?: |$)).)*?(?:^| )(?:{options})(?: |$)"


def _case(args: str, weight: int, note: str) -> CommandCase:
    return CommandCase(args=args, weight=weight, note=note)


#: Help and version, before a `--`: no lease.
_HELP = _case(_before_dashes("-h|--help|--version"), 0, "help or version")
#: pytest's help, version and listings: no lease.
_PYTEST_LISTS = _case(
    _word("-h|--help|--version|-V|--co|--collect-only|--fixtures|--fixtures-per-test|--markers"),
    0,
    "help, version, or a listing (--collect-only, --fixtures, --markers)",
)
#: A pytest run on named tests: a node id (`tests/test_x.py::test_y`, not one given to
#: `--deselect`), or a `-k` selection that does not start with `not`.
_PYTEST_NAMED = _case(
    r"(?:^| )(?<!--deselect )(?!-)\S*::|(?:^| )(?:-k ?|--keyword[= ])(?!not(?: |$))[^ ]",
    1,
    "named tests: a node id (path::test) or a -k selection",
)
#: make's options that build nothing; those that change no target; and the targets that only
#: clean or print help.
_MAKE_NOTHING = "-h|--help|-v|--version|-n|--dry-run|--just-print|--recon|-q|--question"
_MAKE_OPTION = (
    r"(?:-C|-f|--directory|--file) \S+|-C\S+|-f\S+|--directory=\S+|--file=\S+|-s|--silent"
    r"|-j ?\d*"
)
_MAKE_CLEAN = "clean|distclean|mrproper|help"
_MAKE_CASE = _case(
    _word(_MAKE_NOTHING)
    + rf"|^(?:(?:{_MAKE_OPTION}) )*(?:{_MAKE_CLEAN})(?: (?:{_MAKE_OPTION}|{_MAKE_CLEAN}))*$",
    0,
    "help, version, a dry run, or only clean or help targets",
)
_NINJA_CASE = _case(
    _word("-h|--help|--version|-n|-t"), 0, "help, version, a dry run or a tool (-t)"
)
_BAZEL_CASE = _case(
    r"^(?:--\S+ )*(?:info|version|query|shutdown|help|clean|license)(?: |$)",
    0,
    "info, version, query, shutdown, help or clean",
)
_GO_RUN = _case(
    r"^test(?!.*(?:\.\.\.|-bench))(?: \S+)* -run[= ]\S",
    1,
    "a -run selection in named packages (no ./..., no -bench)",
)
_LISTS = _case(
    _before_dashes("-h|--help|--version|-l|--list|--listenvs"), 0, "help, version or a listing"
)
_VERSION = _case(_before_dashes("-h|--help|-v|--version"), 0, "help or version")
#: A JavaScript test runner's or compiler's version, listing or config: no lease.
_JS_LISTS = _case(
    _word("-v|--listTests|--list|--showConfig|--init|--all") + r"|^list(?: |$)",
    0,
    "version, a listing of the tests, or the config",
)
#: A package manager's test script or tool asked for its help or version, after a `--` too.
_JS_SCRIPT_HELP = _case(
    r"^(?:test|t|run test\S*|jest|vitest|tsc|playwright)\b.*" + _word("-h|--help|--version"),
    0,
    "the test runner's help or version",
)


#: The built-in xdist rule's `args`: a pytest run whose arguments start xdist workers. The
#: regular expression is what a client without `leasehold.xdist` matches; this one reads the
#: arguments one by one (`xdist.in_args`).
XDIST_ARGS = xdist.XDIST_ARGS

#: The weights of the built-in rules when `[commands]` sets none.
HEAVY_WEIGHT = 4
LIGHT_WEIGHT = 2


def default_rules(heavy: int = HEAVY_WEIGHT, light: int = LIGHT_WEIGHT) -> tuple[CommandRule, ...]:
    """The built-in rules: builds and test runs that use every core they are given, at weight
    `heavy`, and package installs and bundlers at weight `light`. Their cases take no lease
    for help, version, listings, dry runs and cleaning, and weigh 1 for a pytest run on named
    tests and a `go test -run` in named packages; other arguments weigh the rule's weight. A
    pytest run with xdist workers (`XDIST_ARGS`) runs its jobs as worker processes, each of
    which may run a BLAS or OpenMP pool, so each gets one thread; it comes before the plain
    pytest rule of the same weight, so it is the one an xdist run matches, and its named
    tests weigh `heavy` too."""
    return (
        _rule("pytest py.test", heavy, XDIST_ARGS, threads=1, cases=(_PYTEST_LISTS,)),
        _rule("pytest py.test", heavy, cases=(_PYTEST_LISTS, _PYTEST_NAMED)),
        _rule("tox nox", heavy, cases=(_LISTS,)),
        _rule(
            "cargo",
            heavy,
            r"^(build|b|test|t|check|c|clippy|bench|run|r|nextest|doc|install)\b",
            cases=(_HELP,),
        ),
        _rule("make gmake", heavy, cases=(_MAKE_CASE,)),
        _rule("ninja", heavy, cases=(_NINJA_CASE,)),
        _rule("bazel bazelisk", heavy, cases=(_HELP, _BAZEL_CASE)),
        _rule("cmake", heavy, r"--build", cases=(_HELP,)),
        _rule("go", heavy, r"^(build|test|vet|install|generate)\b", cases=(_HELP, _GO_RUN)),
        _rule("mvn mvnw gradle gradlew", heavy, cases=(_VERSION,)),
        _rule(
            "docker podman",
            heavy,
            r"^(build|buildx\s+build|compose\s+(build|up))\b",
            cases=(_HELP,),
        ),
        _rule(
            "npm pnpm yarn bun",
            heavy,
            r"^(test|t|build|run\s+(build|test)\S*|jest|vitest|tsc|playwright)\b",
            cases=(_HELP, _JS_SCRIPT_HELP),
        ),
        _rule("npm pnpm yarn bun", light, r"^(ci|install|i|add)\b", cases=(_HELP,)),
        _rule("jest vitest playwright tsc", heavy, cases=(_HELP, _JS_LISTS)),
        _rule("webpack vite", light, cases=(_HELP,)),
        _rule("just", heavy, r"^(test|build|check)\b"),
    )


#: The built-in rules at their default weights.
DEFAULT_RULES: tuple[CommandRule, ...] = default_rules()


class CommandSettings(BaseModel):
    """The `[commands]` table: the rules, and what an agent hook does on a busy machine."""

    model_config = ConfigDict(extra="forbid")

    #: Whether the built-in rules apply after the config's own (`leasehold commands` lists
    #: them).
    defaults: bool = True
    rule: list[CommandRule] = Field(default_factory=list)
    #: The weight of the built-in rules for builds and test runs (pytest, cargo, make, ...).
    heavy_weight: int = Field(HEAVY_WEIGHT, ge=1)
    #: The weight of the built-in rules for package installs and bundlers (npm install,
    #: webpack, vite).
    light_weight: int = Field(LIGHT_WEIGHT, ge=1)
    #: How long an agent's hook, or a shim, waits in line for a lease before `when_busy`
    #: applies. Long enough for a busy machine's line to move (docs/bench.md: at 8 agents
    #: the mean wait was 50 s, the slowest about 100 s).
    wait_s: float = Field(300.0, ge=0, le=540)  # inside the plugin's 600 s hook timeout
    #: After `wait_s`: run the command without a lease (and say so), or refuse it with a
    #: message that tells the agent the machine is busy.
    when_busy: Literal["run", "deny"] = "deny"
    #: The hard cap on every leased command's memory when contained, where its rule sets
    #: none: a size such as `12g`, a share of the machine's memory such as `50%`, or `off`.
    memory_max: str = CAP_OFF

    @field_validator("memory_max")
    @classmethod
    def _cap(cls, value: str) -> str:
        return check_cap(value)

    def rules(self) -> list[tuple[str, CommandRule]]:
        """Every rule in the order they are tried, each with its source."""
        mine = [("config", r) for r in self.rule]
        built_in = default_rules(self.heavy_weight, self.light_weight) if self.defaults else ()
        return mine + [("default", r) for r in built_in]

    def shell_rules(self) -> list[shell.Rule]:
        return [shell.Rule.of(rule.model_dump() | {"source": src}) for src, rule in self.rules()]


def match(settings: CommandSettings, command: str) -> shell.Match | None:
    """`shell.match` against these settings' rules, as the daemon would serve them (but
    with each weight as configured)."""
    return shell.match(settings.shell_rules(), command)
