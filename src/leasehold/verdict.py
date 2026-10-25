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
"most likely". When leasehold's own watchdog stopped the command (`leasehold`), it says so for
certain, and whether to run it again as is.
"""

from __future__ import annotations

import re
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
    `kernel` (the OOM killer, for the machine or a memory limit above the command),
    `agent_cap` (the agent's own memory cap) or `leasehold` (leasehold's watchdog); `certain`
    is false when it is only the likeliest reading. `person` says what happened; `agent` adds
    what to do about it. `largest` is the largest of the command's processes, as (pid, name,
    bytes), when it was seen. `watchdog` is, for `leasehold`, what the watchdog said of the
    stop: `why` (`critical`, `runaway`, `large` or `max_pause`), `runaway`, `rerun` (whether
    leasehold runs the command's next request alone) and `paused_s`."""

    kind: str
    certain: bool
    program: str
    peak_bytes: int | None
    limit_bytes: int | None
    person: str
    agent: str
    largest: tuple[int, str, int] | None = None
    watchdog: dict | None = None


#: A command line or rule that runs tests.
_TESTS = re.compile(
    r"(^|[\s/,])(pytest|py\.test|jest|vitest|playwright|mocha|tox|nox|ctest|rspec|phpunit)"
    r"(\s|,|$)|(^|\s)test(\s|$)"
)


def test_like(text: str | None) -> bool:
    """Whether `text` (a command line, or a rule's key) runs tests."""
    return bool(text) and _TESTS.search(text or "") is not None


def stopped_note(stop: dict) -> str:
    """What a stop by the watchdog was not: a test failure, for tests (`stop["test"]`); else,
    unless the command ran away, a failure of the command."""
    if stop.get("test"):
        return NOT_A_FAILURE
    return "" if stop.get("runaway") else "This is not a failure of your command."


def stopped_todo(stop: dict) -> str:
    """What to do after a stop by the watchdog."""
    if stop.get("runaway"):
        return "It grew without bound: do not run it again unchanged; find what allocates."
    if stop.get("alone"):
        return "It was stopped even running alone: run it again only with fewer workers."
    if stop.get("rerun"):
        return "It can run again as is. If leasehold has already rerun it, it has to run alone."
    return _AFTER_OTHERS


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
    watchdog: dict | None = None,
) -> Verdict | None:
    """The verdict on `program`, which ended with `status` (negative for a signal, as
    `subprocess` gives it, or a shell's 128 and the signal), when `ending` says memory ended
    it; None when it does not. `peak_bytes` is the most its cgroup used, `limit_bytes` the
    `memory.max` above it (None: the machine's memory was the limit), and `agent_limit` the
    value of `CLAUDE_CODE_TOOL_MEMORY_LIMIT` it ran under (None: not set). For an `ending` at
    the command's own cap, `limit_bytes` is that cap, and `largest` the largest of its
    processes, as (pid, name, bytes), when known. For an `ending` by leasehold's watchdog,
    `watchdog` is what it said of the stop (`Verdict.watchdog`)."""
    if ending.memory is None:
        return None
    if ending.memory == "leasehold":
        stop = watchdog or {}
        paused = stop.get("paused_s")
        first = f" after {paused:.0f}s paused" if paused is not None else ""
        person = (
            f"leasehold: {program} was stopped{first} by leasehold's watchdog to keep the "
            f"machine responsive, as memory ran short. {stopped_note(stop)}"
        ).strip()
        todo = stopped_todo(stop)
        return Verdict(
            "leasehold", True, program, peak_bytes, None, person, f"{person} {todo}",
            watchdog=dict(stop),
        )  # fmt: skip
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
