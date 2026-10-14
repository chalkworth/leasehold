"""The `[commands]` config table: which commands are heavy enough to take a lease, for the
coding-agent hooks and the PATH shims (`leasehold.shell` matches commands against these rules).

A rule names programs (by basename) and, optionally, a regular expression their arguments must
contain. Rules from the config are tried before the built-in ones (`DEFAULT_RULES`), so a
config can re-weigh a program the defaults name; among the rules of one source the heaviest
match wins.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from leasehold import shell
from leasehold.model import Name
from leasehold.units import memory_to_bytes


class CommandRule(BaseModel):
    """`weight` slots (in `pool`, else the kind's) for a command that runs one of `programs`
    with arguments in which `args` is found, held to `memory` when contained."""

    model_config = ConfigDict(extra="forbid")

    programs: list[str] = Field(min_length=1)
    #: A regular expression searched in the program's arguments, joined by spaces.
    args: str | None = None
    weight: int = Field(ge=1)
    pool: Name | None = None
    #: Memory the command is held to when contained (`MemoryHigh`), a size such as `4g`.
    memory: str | None = None

    @field_validator("memory")
    @classmethod
    def _size(cls, value: str | None) -> str | None:
        if value is not None:
            memory_to_bytes(value)
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


def _rule(programs: str, weight: int, args: str | None = None) -> CommandRule:
    return CommandRule(programs=programs.split(), weight=weight, args=args)


#: The built-in rules: builds and test runs that use every core they are given.
DEFAULT_RULES: tuple[CommandRule, ...] = (
    _rule("pytest py.test tox nox", 4),
    _rule("cargo", 4, r"^(build|b|test|t|check|c|clippy|bench|run|r|nextest|doc|install)\b"),
    _rule("make gmake ninja bazel bazelisk", 4),
    _rule("cmake", 4, r"--build"),
    _rule("go", 4, r"^(build|test|vet|install|generate)\b"),
    _rule("mvn mvnw gradle gradlew", 4),
    _rule("docker podman", 4, r"^(build|buildx\s+build|compose\s+(build|up))\b"),
    _rule("npm pnpm yarn bun", 2, r"^(test|t|ci|install|i|add|build|run\s+(build|test)\S*)\b"),
    _rule("jest vitest playwright tsc webpack vite", 2),
)


class CommandSettings(BaseModel):
    """The `[commands]` table: the rules, and what an agent hook does on a busy machine."""

    model_config = ConfigDict(extra="forbid")

    #: Whether the built-in rules apply after the config's own (`leasehold commands` lists
    #: them).
    defaults: bool = True
    rule: list[CommandRule] = Field(default_factory=list)
    #: How long an agent's hook waits for a lease before `when_busy` applies.
    wait_s: float = Field(20.0, ge=0, le=540)  # inside the plugin's 600 s hook timeout
    #: After `wait_s`: run the command without a lease (and say so), or refuse it with a
    #: message that tells the agent the machine is busy.
    when_busy: Literal["run", "deny"] = "deny"

    def rules(self) -> list[tuple[str, CommandRule]]:
        """Every rule in the order they are tried, each with its source."""
        mine = [("config", r) for r in self.rule]
        return mine + ([("default", r) for r in DEFAULT_RULES] if self.defaults else [])

    def shell_rules(self) -> list[shell.Rule]:
        return [shell.Rule.of(rule.model_dump() | {"source": src}) for src, rule in self.rules()]


def match(settings: CommandSettings, command: str) -> shell.Match | None:
    """`shell.match` against these settings' rules, as the daemon would serve them (but
    with each weight as configured)."""
    return shell.match(settings.shell_rules(), command)
