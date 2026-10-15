"""The `[commands]` config table: which commands are heavy enough to take a lease, for the
coding-agent hooks and the PATH shims (`leasehold.shell` matches commands against these rules).

A rule names programs (by basename) and, optionally, a regular expression their arguments must
contain. Rules from the config are tried before the built-in ones (`default_rules`), so a
config can re-weigh a program the defaults name; among the rules of one source the heaviest
match wins. The built-in rules come in two weights, `heavy_weight` for builds and test runs
and `light_weight` for JavaScript tooling, both set in `[commands]`.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from leasehold import shell
from leasehold.model import Name
from leasehold.units import declared_to_bytes


class CommandRule(BaseModel):
    """`weight` slots (in `pool`, else the kind's) for a command that runs one of `programs`
    with arguments in which `args` is found, held to `memory` when contained, each of its
    processes given `threads` threads (`leasehold.jobs`)."""

    model_config = ConfigDict(extra="forbid")

    programs: list[str] = Field(min_length=1)
    #: A regular expression searched in the program's arguments, joined by spaces.
    args: str | None = None
    weight: int = Field(ge=1)
    pool: Name | None = None
    #: Memory the command is held to when contained (`MemoryHigh`), a size such as `4g`.
    memory: str | None = None
    #: The size of each process's own thread pool (OpenMP, BLAS and the like): "jobs", the
    #: lease's job count, or a count of at least 1 (never more than the job count).
    threads: Literal["jobs"] | Annotated[StrictInt, Field(ge=1)] = "jobs"

    @field_validator("memory")
    @classmethod
    def _size(cls, value: str | None) -> str | None:
        if value is not None:
            declared_to_bytes(value)
        return value

    @field_validator("args")
    @classmethod
    def _compiles(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{value!r} is not a regular expression: {exc}") from None
        return value


def _rule(
    programs: str, weight: int, args: str | None = None, threads: int | str = "jobs"
) -> CommandRule:
    return CommandRule(programs=programs.split(), weight=weight, args=args, threads=threads)


#: The weights of the built-in rules when `[commands]` sets none.
HEAVY_WEIGHT = 4
LIGHT_WEIGHT = 2


def default_rules(heavy: int = HEAVY_WEIGHT, light: int = LIGHT_WEIGHT) -> tuple[CommandRule, ...]:
    """The built-in rules: builds and test runs that use every core they are given, at weight
    `heavy`, and JavaScript tooling at weight `light`. pytest's jobs are worker processes
    (pytest-xdist), each of which may run a BLAS or OpenMP pool, so each gets one thread."""
    return (
        _rule("pytest py.test tox nox", heavy, threads=1),
        _rule(
            "cargo", heavy, r"^(build|b|test|t|check|c|clippy|bench|run|r|nextest|doc|install)\b"
        ),
        _rule("make gmake ninja bazel bazelisk", heavy),
        _rule("cmake", heavy, r"--build"),
        _rule("go", heavy, r"^(build|test|vet|install|generate)\b"),
        _rule("mvn mvnw gradle gradlew", heavy),
        _rule("docker podman", heavy, r"^(build|buildx\s+build|compose\s+(build|up))\b"),
        _rule(
            "npm pnpm yarn bun", light, r"^(test|t|ci|install|i|add|build|run\s+(build|test)\S*)\b"
        ),
        _rule("jest vitest playwright tsc webpack vite", light),
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
    #: The weight of the built-in rules for JavaScript tooling (npm test, jest, tsc, ...).
    light_weight: int = Field(LIGHT_WEIGHT, ge=1)
    #: How long an agent's hook, or a shim, waits in line for a lease before `when_busy`
    #: applies. Long enough for a busy machine's line to move (docs/bench.md: at 8 agents
    #: the mean wait was 50 s, the slowest about 100 s).
    wait_s: float = Field(300.0, ge=0, le=540)  # inside the plugin's 600 s hook timeout
    #: After `wait_s`: run the command without a lease (and say so), or refuse it with a
    #: message that tells the agent the machine is busy.
    when_busy: Literal["run", "deny"] = "deny"

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
