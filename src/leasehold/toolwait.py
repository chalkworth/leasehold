"""How long a wait in line may take when an agent's tool call runs the command, so that the agent
hears leasehold's refusal before its own timeout ends the call.

An agent runs a shell command with a timeout of its own (Claude Code's Bash tool: 2 minutes
unless the call names another). A PATH shim or `hold` inside that command waits in line on the
same clock, so a wait of `[commands] wait_s` (300 s) is cut short by the agent, which then sees
only "timed out". `LEASEHOLD_TOOL_TIMEOUT_S` says how long the caller's tool call may run.
When it is set, a shim and `hold` wait at most `SHARE` of it, leaving the rest for the command
itself, and then refuse (exit 75) with the usual busy message.

The variable is set by whoever knows the agent: `leasehold run --tool-timeout S`, or for an
agent `run` recognises (`detect`), by `run` itself; the Claude Code plugin's `SessionStart`
hook; or the user. The agent hooks wait before the command starts, outside the tool's clock,
and ignore it.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence

#: The caller's tool timeout in seconds.
ENV = "LEASEHOLD_TOOL_TIMEOUT_S"
#: The part of the tool timeout a wait in line may use.
SHARE = 0.5
#: Claude Code's Bash tool: its default timeout, and the variable that changes it.
CLAUDE_CODE_BASH_S = 120.0
CLAUDE_CODE_BASH_ENV = "BASH_DEFAULT_TIMEOUT_MS"


def _seconds(text: str | None) -> float | None:
    try:
        value = float(text) if text else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value > 0 else None


def timeout(env: Mapping[str, str]) -> float | None:
    """The caller's tool timeout `env` gives (`ENV`), in seconds; None when it gives none, or
    one that is not a positive number."""
    return _seconds(env.get(ENV))


def detect(argv: Sequence[str], env: Mapping[str, str]) -> float | None:
    """The tool timeout of the agent `argv` starts, when leasehold knows it: Claude Code's
    (`claude`), `BASH_DEFAULT_TIMEOUT_MS` when set, else 2 minutes. None for any other
    program."""
    if not argv or os.path.basename(argv[0]) != "claude":
        return None
    ms = _seconds(env.get(CLAUDE_CODE_BASH_ENV))
    return CLAUDE_CODE_BASH_S if ms is None else ms / 1000


def limit(env: Mapping[str, str], wait_s: float | None) -> float | None:
    """How long to wait in line: `wait_s` (None: no limit), cut to `SHARE` of the caller's tool
    timeout when `env` gives one."""
    found = timeout(env)
    if found is None:
        return wait_s
    share = round(found * SHARE, 1)
    return share if wait_s is None else min(wait_s, share)


def said(env: Mapping[str, str], wait_s: float | None) -> str | None:
    """Why a wait is shorter than `wait_s`, when the tool timeout cut it; None when it did not."""
    cut = limit(env, wait_s)
    if cut is None or (wait_s is not None and cut >= wait_s):
        return None
    return (
        f"waiting at most {cut:g}s, half the caller's tool timeout of {timeout(env):g}s ({ENV}), "
        "so the caller hears the answer before it gives up"
    )
