"""The memory verdict: one line that tells an agent, or a person, that a command was killed for
memory and is not a test failure, and what to do next.

`describe` is shared by every caller that can know it: `hold` and the PATH shims (on stderr,
last), the Python API (`Command.memory`), and the Claude Code hook (as context the agent
reads, and as a message the person sees). The command's exit code is never changed: the
verdict travels beside it.

Each line starts with `leasehold:` and says "not a test failure". A `certain` verdict comes
from the command's own cgroup, which counted the kill. When the command had its own hard cap
(`memory_max`) and that was the tightest limit on it, the verdict names the cap, and the
largest of its processes when leasehold saw it. A `likely` one comes from the machine's
count of OOM kills (`leasehold.host`), which may have been another process's, so it says
"most likely".
"""

from __future__ import annotations

import signal
from dataclasses import dataclass

from leasehold.host import Ending, killed_by
from leasehold.units import size

#: Claude Code's own memory cap on the commands of a session, when the user sets one.
AGENT_LIMIT_ENV = "CLAUDE_CODE_TOOL_MEMORY_LIMIT"
NOT_A_FAILURE = "This is not a test failure."
_AFTER_OTHERS = "Run it again only after the other heavy work has finished, with fewer workers."


@dataclass(frozen=True)
class Verdict:
    """Why memory ended a command. `kind` is `cap` (the command's own hard cap, `memory_max`),
    `kernel` (the OOM killer, for the machine or a memory limit above the command) or
    `agent_cap` (the agent's own memory cap); `certain` is false when it is only the likeliest
    reading. `person` says what happened; `agent` adds what to do about it. `largest` is the
    largest of the command's processes, as (pid, name, bytes), when it was seen."""

    kind: str
    certain: bool
    program: str
    peak_bytes: int | None
    limit_bytes: int | None
    person: str
    agent: str
    largest: tuple[int, str, int] | None = None


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return f"signal {sig}"


def describe(
    ending: Ending,
    program: str,
    *,
    status: int,
    peak_bytes: int | None = None,
    limit_bytes: int | None = None,
    agent_limit: str | None = None,
    largest: tuple[int, str, int] | None = None,
) -> Verdict | None:
    """The verdict on `program`, which ended with `status` (negative for a signal, as
    `subprocess` gives it, or a shell's 128 and the signal), when `ending` says memory ended
    it; None when it does not. `peak_bytes` is the most its cgroup used, `limit_bytes` the
    `memory.max` above it (None: the machine's memory was the limit), and `agent_limit` the
    value of `CLAUDE_CODE_TOOL_MEMORY_LIMIT` it ran under (None: not set). For an `ending` at
    the command's own cap, `limit_bytes` is that cap, and `largest` the largest of its
    processes, as (pid, name, bytes), when known."""
    if ending.memory is None:
        return None
    if ending.memory == "cap" and limit_bytes is not None:
        biggest = ""
        if largest is not None:
            pid, name, used = largest
            biggest = f": its largest process, {name} (pid {pid}), had {size(used)}"
        person = (
            f"leasehold: {program} was stopped by leasehold's cap of {size(limit_bytes)} for "
            f"this command{biggest}. {NOT_A_FAILURE}"
        )
        agent = (
            f"{person} Do not run it again unchanged: run fewer workers or a narrower "
            "selection, find what allocates, or raise its memory_max."
        )
        return Verdict("cap", True, program, peak_bytes, limit_bytes, person, agent, largest)
    peak = f" (peak {size(peak_bytes)})" if peak_bytes is not None else ""
    if ending.confidence == "certain":
        sig = killed_by(status)
        whole = sig == signal.SIGKILL
        whom = program if whole else f"a process of {program}"
        if limit_bytes is not None:
            where = f"when the memory limit above it ({size(limit_bytes)}) ran out"
            advice = "Run it with fewer workers, or raise that limit."
        else:
            where, advice = "when the machine ran out of memory", _AFTER_OTHERS
        what = f"leasehold: the kernel's OOM killer ended {whom}{peak} {where}"
        if sig is not None and not whole:
            what += f", and {program} ended by {_signal_name(sig)}"
        elif not whole:
            what += f", and {program} exited {status}"
        person = f"{what}. {NOT_A_FAILURE}"
        agent = f"{person} {advice}"
        return Verdict("kernel", True, program, peak_bytes, limit_bytes, person, agent)
    if agent_limit:
        person = (
            f"leasehold: {program} was most likely killed by Claude Code's tool memory limit "
            f"({AGENT_LIMIT_ENV}={agent_limit}, shared by every command of this session). "
            f"{NOT_A_FAILURE}"
        )
        agent = f"{person} Run it alone, or raise the limit."
        return Verdict("agent_cap", False, program, peak_bytes, None, person, agent)
    person = (
        f"leasehold: {program} ended by SIGKILL while the kernel's OOM killer was ending "
        "processes; most likely it was killed for memory, not a test failure."
    )
    return Verdict("kernel", False, program, peak_bytes, None, person, f"{person} {_AFTER_OTHERS}")
