"""Claude Code's hooks (`integrations/claude-code`): a lease for each heavy Bash command an agent
runs, taken before the command starts and given back when it ends.

`PreToolUse` runs `leasehold hook claude-code pre`. When the command matches a `[commands]`
rule (`leasehold.shell`), it waits in line for that rule's weight, then lets the command run
with a renewer keeping the lease (`leasehold keep`, watching the Claude Code process, so an
agent that dies gives its leases back within one TTL). `PostToolUse` and `PostToolUseFailure`
run `... post`, which releases it. The command itself is never changed, so the user's
permission rules see exactly what the agent wrote.

Each Claude Code session is its own leasehold session: one user's agents take turns with each
other, and with that user's other work, as `admission` rotates sessions. A session's sub-agents
fire the same hooks under its session id, so a session that fans out shares its turn rather
than multiplying it; each lease names its sub-agent (`agent_id`) for the views.

A command run in the background (`run_in_background`) takes no lease: Claude Code reports it
done (`PostToolUse`) as it starts, so the hooks cannot see it end. PATH shims (`leasehold
shims`) hold a lease for the life of the process itself, background or not.

The plugin runs both through `hooks/prefilter.sh`, which starts no Python for a command none of
whose words is a program the rules name (`leasehold.programs`, kept in step by `pre`), nor for a
`post` with no lease noted for it. So `pre` takes a lease only where it can note it for `post`
(`leasehold.hooknotes`), and gives back one it could not note. The note also tells a PATH shim
the command runs through (under `leasehold run`) that this lease covers it.

Nothing here may stop an agent working because leasehold is absent or broken: no daemon, an
error, a command no rule names, all let the command run. Only a busy machine, with `[commands]
when_busy = "deny"`, refuses one, after `wait_s` in line, with a reason the agent reads that
says the command did not run.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from leasehold import busy, handoff, hooknotes, programs, shell
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import EXIT_BUSY
from leasehold.hold import lease_request, wait_for_grant
from leasehold.proctable import scan

#: Lease ids are `cc.` and the tool call's id; sessions `cc-` and the Claude Code session's.
LEASE_PREFIX = "cc."
SESSION_PREFIX = "cc-"
#: The Bash tool's own limit on a call when it names none (its default is 2 minutes, its most
#: 10): the renewer keeps a lease at most this long past the call's `timeout`, so a call whose
#: end is never reported loses its lease soon after it must have ended.
BASH_MAX_S = 600.0
MARGIN_S = 60.0
#: A command that waited at least this long for its lease says so when it starts, so a slow
#: start is not mistaken for a slow command.
WAIT_NOTED_S = 5.0
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]")


@dataclass
class Reply:
    """What the hook prints on stdout (JSON, or nothing) and on stderr; it always exits 0."""

    output: dict | None = None
    log: list[str] = field(default_factory=list)

    def text(self) -> str:
        return json.dumps(self.output) if self.output is not None else ""


def _id(prefix: str, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return prefix + _UNSAFE.sub("_", value)[-(64 - len(prefix)) :]


def lease_id(event: Mapping) -> str | None:
    return _id(LEASE_PREFIX, event.get("tool_use_id"))


def session(event: Mapping) -> str | None:
    return _id(SESSION_PREFIX, event.get("session_id"))


def agent_pid(start: int, comm: str = "claude", root: Path = Path("/proc")) -> int | None:
    """The Claude Code process this hook runs under: the nearest ancestor of `start` named
    `comm`. None when there is none (the renewer then runs to its TTL and `MAX_HOLD_S`)."""
    procs = scan(root)
    pid = start
    for _ in range(64):
        proc = procs.get(pid)
        if proc is None:
            return None
        if proc.comm == comm:
            return pid
        pid = proc.ppid
    return None


def max_hold(event: Mapping) -> float:
    """How long the renewer may keep this call's lease: the call's own timeout (milliseconds in
    `tool_input.timeout`) or the Bash tool's longest, and a margin."""
    timeout = (event.get("tool_input") or {}).get("timeout")
    if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0:
        return min(float(timeout) / 1000, BASH_MAX_S) + MARGIN_S
    return BASH_MAX_S + MARGIN_S


def _message(text: str) -> dict:
    """A line the user sees in the transcript; the agent does not."""
    return {"systemMessage": f"leasehold: {text}"}


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _labels(event: Mapping, command: str) -> dict[str, str]:
    labels = {"agent": "claude-code", "cmd": command[:120]}
    sub = event.get("agent_id")
    if isinstance(sub, str) and sub:
        labels["subagent"] = _UNSAFE.sub("_", sub)[:64]
    return labels


def pre(
    event: Mapping,
    client: Client,
    *,
    env: Mapping[str, str],
    watch_pid: int | None,
    spawn: Callable[[list[str]], int] = handoff.spawn_keeper,
) -> Reply:
    """Before a Bash command: wait for its lease, or let it run, or (busy, `when_busy =
    "deny"`) refuse it."""
    reply = Reply()
    command = (event.get("tool_input") or {}).get("command")
    lid = lease_id(event)
    if event.get("tool_name") != "Bash" or not isinstance(command, str) or lid is None:
        return reply
    if env.get("LEASEHOLD_LEASE_ID"):
        return reply  # the agent itself runs under a lease, which covers its commands
    if (event.get("tool_input") or {}).get("run_in_background"):
        reply.log.append("a background command takes no lease (its end is not reported)")
        return reply
    try:
        settings = shell.served(client.commands())
    except (Unavailable, ApiError) as exc:
        reply.log.append(f"no lease taken: {exc}")
        return reply
    _refresh_programs(settings, env)
    found = shell.match(settings.rules, command)
    if found is None:
        return reply
    directory = hooknotes.state_dir(env)
    if directory is None:
        reply.log.append("no lease taken: no private directory to note it in for `post`")
        return reply
    rule = found.rule
    request = lease_request(
        pool=rule.pool,
        weight=rule.weight,
        labels=_labels(event, command),
        session=session(event),
        memory=rule.memory,
    ) | {"lease_id": lid}
    asked = time.monotonic()
    code, answer = wait_for_grant(client, request, settings.wait_s, reply.log.append)
    waited = time.monotonic() - asked
    if code is None:
        if waited >= WAIT_NOTED_S:
            reply.output = _message(f"{found.program} waited {waited:.0f}s in line for its lease")
        argv = handoff.keeper_argv(
            str(client.path), lid, ttl_s=float(answer["lease"]["ttl_s"]),
            max_hold_s=max_hold(event), watch_pid=watch_pid,
        )  # fmt: skip
        try:
            pid = spawn(argv)
        except OSError as exc:
            pid = None
            reply.log.append(f"cannot start the renewer, the lease ends in one TTL: {exc}")
        if not hooknotes.write(directory, lid, pid, watch_pid, found.program):
            # Unnoted, `post` might never be run for it (the pre-filter): give it back now.
            if pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            with contextlib.suppress(Unavailable, ApiError):
                client.release(lid)
            reply.log.append(f"no lease kept: cannot note it in {directory}")
        return reply
    if code == EXIT_BUSY:
        why = busy.told(client, answer, pool=rule.pool, waited_s=settings.wait_s)
        if settings.when_busy == "deny":
            reply.output = _deny(f"leasehold: {why}. {busy.ADVICE}")
        else:
            reply.output = _message(f"{why}; running {found.program} without a lease")
        return reply
    reply.output = _message(f"running {found.program} without a lease (exit {code})")
    return reply


def _refresh_programs(settings: shell.Served, env: Mapping[str, str]) -> None:
    """Keep the pre-filter's word list (`leasehold.programs`) in step with the served rules."""
    target = programs.path(env)
    if target is not None:
        with contextlib.suppress(OSError):
            programs.write(target, programs.names(settings.rules))


def post(event: Mapping, client: Client, *, env: Mapping[str, str]) -> Reply:
    """After a Bash command, whether it succeeded, failed or was interrupted: stop its renewer
    and give its lease back. The renewer goes first, so a release the daemon misses still ends
    the lease, one TTL later."""
    reply = Reply()
    lid = lease_id(event)
    if event.get("tool_name") != "Bash" or lid is None:
        return reply
    hooknotes.stop(hooknotes.state_dir(env), lid)
    with contextlib.suppress(Unavailable, ApiError):
        client.release(lid)  # one never taken, or already gone, is no error
    return reply
