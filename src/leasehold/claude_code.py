"""Claude Code's hooks (`integrations/claude-code`): a lease for each heavy Bash command an agent
runs, taken before the command starts and given back when it ends.

`PreToolUse` runs `leasehold hook claude-code pre`. When the command matches a `[commands]`
rule (`leasehold.shell`), it waits in line for that rule's weight, then lets the command run
with a renewer keeping the lease (`leasehold keep`, watching the Claude Code process, so an
agent that dies gives its leases back within one TTL). `PostToolUse` and `PostToolUseFailure`
run `... post`, which releases it. When the command ended by SIGKILL while the kernel's OOM
killer was ending processes (`leasehold.host`, from counters `pre` noted), `post` also tells
the agent, as context it reads, that the command was most likely killed for memory and is not
a test failure, and tells the person the same in the transcript (`leasehold.verdict`).

`SessionStart` runs `... start`, which registers the session with the daemon, so that it
watches everything the session's Claude Code process starts (`leasehold.sessions`), and adds to
the session's `CLAUDE_ENV_FILE` the line that names the session (`LEASEHOLD_SESSION`) to every
command, and the lines that put a leased command's shell in its lease's scope as it starts
(`leasehold.attach`). `SessionEnd` runs `... end`, which unregisters it. Claude Code
runs that file in each Bash command's shell, before the command. So a leased command is
contained, measured and, with a `memory_max`, capped, as under `hold`. When its scope counted
an OOM kill, `post` says so for certain, a worker's kill that made pytest exit 1 included.
Where the shell cannot be moved (it runs under Claude Code's own memory limit, or the platform
has no scopes), the lease only counts it, and the verdict stays "most likely".

The command itself is never changed, so the user's permission rules see exactly what the agent
wrote, and the hook does not size it. A rule's
range, or a `LEASEHOLD_WANT` range in Claude Code's own environment (`leasehold.weights`), is
asked for as a range: from its least up to the rule's `hook_ceiling`, both ends cut to it.
`hook_ceiling` is what the range alone would be granted on an idle machine
(`leasehold.admission.alone`), served by the daemon. The request waits in line for its least
and is granted a size as any range is. The note records the size granted, its job count and
thread count, so a PATH shim the command runs through (under `leasehold run`) sizes it to the
grant. The agent is told the size too, as context it reads before the command runs, so that
it can run the command with that many jobs where no shim does it.

A command that waited at least `busy.WAIT_NOTED_S` in line says so as it starts, both to the
person and to the agent, with the lease it waited behind: its wait is not the command's own
run time. The wait is `wait_s` whatever `LEASEHOLD_TOOL_TIMEOUT_S` says (`leasehold.toolwait`):
it happens before the command starts, outside the Bash tool's clock.

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
import dataclasses
import json
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from leasehold import (
    attach,
    busy,
    handoff,
    hooknotes,
    host,
    joining,
    measure,
    programs,
    shell,
    verdict,
    weights,
)
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import EXIT_BUSY, EXIT_UNAVAILABLE
from leasehold.hold import Waited, lease_request, wait_for_grant
from leasehold.proctable import scan
from leasehold.supervise import cap_bytes
from leasehold.units import memory_to_bytes, size

#: Lease ids are `cc.` and the tool call's id; sessions `cc-` and the Claude Code session's.
LEASE_PREFIX = "cc."
SESSION_PREFIX = "cc-"
#: The Bash tool's own limit on a call when it names none (its default is 2 minutes, its most
#: 10): the renewer keeps a lease at most this long past the call's `timeout`, so a call whose
#: end is never reported loses its lease soon after it must have ended.
BASH_MAX_S = 600.0
MARGIN_S = 60.0
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


def tell(event: Mapping, env: Mapping[str, str], reply: Reply, stage: str) -> None:
    """Add what the daemon left for this session (`hooknotes.notices`) to `reply`: the agent
    reads it as context, and the user sees it in the transcript."""
    said = hooknotes.notices(hooknotes.state_dir(env, create=False), session(event))
    if not said:
        return
    text = " ".join(said)
    out = reply.output or {}
    message = " ".join(x for x in (out.get("systemMessage"), text) if x)
    specific = dict(out.get("hookSpecificOutput") or {})
    specific["hookEventName"] = "PreToolUse" if stage == "pre" else "PostToolUse"
    specific["additionalContext"] = " ".join(
        x for x in (specific.get("additionalContext"), text) if x
    )
    reply.output = out | {"systemMessage": message, "hookSpecificOutput": specific}


def _threads(
    settings: shell.Served, command: str, event: Mapping, env: Mapping[str, str]
) -> int | str | None:
    """The `threads` setting the whole command line gives (`shell.threads`, with Claude
    Code's environment and the command's directory, so a pytest that starts xdist workers
    through its config is seen): a count, "jobs", or a share of the job count ("jobs/N",
    "jobs/N:K"), for a PATH shim under this lease; None when it cannot be read."""
    cwd = event.get("cwd")
    try:
        found = list(shell.programs(command))
        return shell.threads(
            settings.rules, found, env=env, cwd=Path(cwd) if isinstance(cwd, str) else None
        )
    except (OSError, ValueError, LookupError, TypeError, AttributeError, re.error):
        return None


def _granted(program: str, weight: int, least: int, most: int, jobs: int) -> str:
    """What the agent reads before a command granted `weight` slots (of the range `least`-`most`
    when that is a range) runs: the job count to run it with."""
    slots = f"{weight} of {weights.text(least, most)}" if most > least else str(weight)
    noun = "slot" if slots == "1" else "slots"
    return (
        f"leasehold: {program} was granted {slots} {noun}; run it with at most {jobs} "
        f"{'job' if jobs == 1 else 'jobs'} (or run the agent under `leasehold run`)."
    )


def _context(text: str) -> dict:
    """Context the agent reads before the command runs."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


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


def _repository(event: Mapping) -> str | None:
    """The top of the git work tree the agent's command starts in (its `cwd`), or None."""
    cwd = event.get("cwd")
    return measure.repository(cwd) if isinstance(cwd, str) and cwd else None


def pre(
    event: Mapping,
    client: Client,
    *,
    env: Mapping[str, str],
    watch_pid: int | None,
    spawn: Callable[[list[str]], int] = handoff.spawn_keeper,
) -> Reply:
    """Before a Bash command: wait for its lease, or let it run, or refuse it: when busy and
    `when_busy = "deny"`, and always when the daemon says no (a refusal, an error answer under
    500, a rule no lease could ever be granted for). When the daemon cannot answer (none is
    reached, or it answers 500 or more) a matched command runs without a lease, and says so."""
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
    except Unavailable as exc:
        reply.log.append(f"no lease taken: {exc}")
        return reply
    except ApiError as exc:
        if exc.status >= 500:  # the daemon cannot answer: as if there were none
            reply.log.append(f"no lease taken: {exc}")
            reply.output = _message(f"the daemon cannot answer ({exc}); running without a lease")
        else:  # it answered no
            reply.output = _deny(f"leasehold: the daemon refused to list its rules: {exc}")
        return reply
    _refresh_programs(settings, env)
    found = shell.match(settings.rules, command)
    if found is None:
        return reply
    if found.rule.never is not None:
        reply.output = _deny(f"leasehold: {found.program} refused: {found.rule.never}")
        return reply
    directory = hooknotes.state_dir(env)
    if directory is None:
        reply.log.append("no lease taken: no private directory to note it in for `post`")
        return reply
    rule = found.rule
    least, most = weights.asked(env, rule.weight, rule.max_weight, reply.log.append, rule.ceiling)
    if rule.hook_ceiling is not None and most > rule.hook_ceiling:
        wanted = weights.text(least, most)
        least, most = min(least, rule.hook_ceiling), rule.hook_ceiling
        reply.log.append(
            f"{found.program} asks {wanted}, cut to {weights.text(least, most)}: the rule's "
            "hook_ceiling, what the range alone would be granted"
        )
    request = lease_request(
        pool=rule.pool,
        weight=least,
        max_weight=most if most > least else None,
        labels=_labels(event, command),
        session=session(event),
        memory=rule.memory,
        memory_per_slot=rule.memory_per_slot,
        rule=rule.key,
        repo=_repository(event) if rule.key is not None else None,
    ) | {"lease_id": lid}
    waited = Waited()
    code, answer = wait_for_grant(client, request, settings.wait_s, reply.log.append, waited=waited)
    if code is None:
        lease = answer["lease"]
        granted = int(lease["weight"])
        top = int(lease.get("max_weight") or granted)
        low = int(lease.get("min_weight") or granted)
        jobs = int(lease.get("jobs") or granted)
        told = [_granted(found.program, granted, low, top, jobs)]
        if waited.seconds >= busy.WAIT_NOTED_S:
            line = busy.waited(found.program, waited.seconds, waited.behind)
            reply.output = {"systemMessage": line}
            told.insert(0, line)
        reply.output = (reply.output or {}) | _context(" ".join(told))
        contain_settings = answer.get("contain") or {}
        cap = cap_bytes(rule.memory_max, contain_settings)
        why = _no_attach(contain_settings)
        if cap is not None and why is not None:
            said = [reply.output.get("systemMessage"), _uncapped(found.program, cap, why)]
            reply.output["systemMessage"] = " ".join(line for line in said if line)
        argv = handoff.keeper_argv(
            str(client.path), lid, ttl_s=float(answer["lease"]["ttl_s"]),
            max_hold_s=max_hold(event), watch_pid=watch_pid, attach=why is None,
        )  # fmt: skip
        try:
            pid = spawn(argv)
        except OSError as exc:
            pid = None
            reply.log.append(f"cannot start the renewer, the lease ends in one TTL: {exc}")
        noted = hooknotes.write(
            directory,
            lid,
            pid,
            watch_pid,
            found.program,
            jobs=jobs,
            threads=_threads(settings, command, event, env) or rule.threads,
            weight=granted,
            mark=host.current().mark().encode(),
        )
        if noted and why is None:
            memory = rule.memory
            if memory is None and rule.memory_per_slot is not None:
                memory = f"{granted * memory_to_bytes(rule.memory_per_slot)}b"
            attach.offer(
                directory, lid, command=command, agent=watch_pid, session=session(event),
                program=found.program, weight=granted, memory=memory, cap_bytes=cap,
                hard_cpu=bool(contain_settings.get("hard_cpu")), max_hold_s=max_hold(event),
                rule=rule.key, repo=_repository(event) if rule.key is not None else None,
            )  # fmt: skip
        if not noted:
            # Unnoted, `post` might never be run for it (the pre-filter): give it back now.
            if pid is not None:
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            with contextlib.suppress(Unavailable, ApiError):
                client.release(lid)
            reply.log.append(f"no lease kept: cannot note it in {directory}")
        return reply
    if code == EXIT_BUSY:
        why, todo = busy.told(client, answer, pool=rule.pool, waited_s=settings.wait_s)
        if settings.when_busy == "deny":
            reply.output = _deny(f"leasehold: {why}. {todo}")
        else:
            reply.output = _message(f"{why}; running {found.program} without a lease")
        return reply
    if code == EXIT_UNAVAILABLE:
        why = answer.get("reason") or "no daemon answers"
        reply.output = _message(f"{why}; running {found.program} without a lease")
        return reply
    # Refused, or an error answer under 500: the daemon said no, so the command does not run
    # unleased, whatever `when_busy` says.
    why = answer.get("reason") or "the daemon refused the request"
    reply.output = _deny(f"leasehold: {found.program} refused: {why}")
    return reply


def _no_attach(contain_settings: Mapping) -> str | None:
    """Why a granted command's shell is not offered its lease's scope here; None when it is."""
    if contain_settings.get("mode", "auto") == "off":
        return 'the daemon has [contain] mode = "off"'
    found = host.current().support("adopt")
    return found.reason if found.how == "none" else None


def _uncapped(program: str, cap: int, why: str | None) -> str:
    return f"leasehold: no memory cap of {size(cap)} on {program}: {why}"


def start(
    event: Mapping,
    env: Mapping[str, str],
    *,
    python: str = sys.executable,
    client: Client | None = None,
    agent_pid: int | None = None,
) -> Reply:
    """At a session's start: register it with the daemon `client` reaches, its agent the
    Claude Code process `agent_pid` (`leasehold.joining`; none without that process), and add
    to the session's `CLAUDE_ENV_FILE`, once each, the line that names the session to its
    commands (`LEASEHOLD_SESSION`) and the lines that attach each leased command's shell to
    its lease's scope (`leasehold.attach.env_lines`). Where nothing can be attached here, add
    none of those, and say why on stderr."""
    reply = Reply()
    sid = session(event)
    if client is not None and sid is not None:
        why = "no Claude Code process found above the hook"
        if agent_pid is not None:
            why = joining.join(client, sid, pid=agent_pid, source="hook", agent="claude")
        if why is not None:
            reply.log.append(f"the session is not watched: {why}")
    target = env.get("CLAUDE_ENV_FILE")
    if not target:
        return reply
    lines = [] if sid is None else [f"export {joining.ENV_SESSION}={sid}"]
    attached, why = attach.env_lines(env, sid, python)
    if not attached:
        reply.log.append(f"commands are leased, not contained: {why}")
    lines += attached
    try:
        path = Path(target)
        have = path.read_text() if path.exists() else ""
        missing = [line for line in lines if line not in have.splitlines()]
        if missing:
            with path.open("a") as out:
                out.write("" if not have or have.endswith("\n") else "\n")
                out.write("\n".join(missing) + "\n")
    except OSError as exc:
        reply.log.append(f"cannot add leasehold's lines to {target}: {exc}")
    return reply


def end(event: Mapping, client: Client) -> Reply:
    """At a session's end: unregister it. A session the daemon does not watch (it never
    registered, or the daemon restarted since) is no error."""
    reply = Reply()
    sid = session(event)
    if sid is None:
        return reply
    try:
        client.unregister_session(sid)
    except ApiError as exc:
        if exc.payload.get("error") not in ("unknown_session", "not_found"):
            reply.log.append(f"cannot unregister session {sid}: {exc}")
    except Unavailable:
        pass
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
    the lease, one TTL later. When memory most likely ended the command (`_memory`), the reply
    says so to the agent and to the person."""
    reply = Reply()
    lid = lease_id(event)
    if event.get("tool_name") != "Bash" or lid is None:
        return reply
    directory = hooknotes.state_dir(env)
    noted = hooknotes.noted(directory, lid)
    hooknotes.stop(directory, lid)
    collected = attach.collect(directory, lid)
    with contextlib.suppress(Unavailable, ApiError):
        # One never taken, or already gone, is no error.
        client.release(lid, _outcome(event, collected))
    found = None if noted is None else _memory(event, *noted, collected=collected, env=env)
    program = noted[0] if noted else "the command"
    said = []
    if collected is not None and collected.attached is None:
        reply.log.append(f"{program} was not contained: {collected.reason}")
        if collected.cap_bytes is not None:
            said.append(_uncapped(program, collected.cap_bytes, collected.reason))
    if collected is not None and collected.left is not None and collected.tree is not None:
        pid, name, used = collected.left
        done = host.current().release(collected.tree)
        said.append(
            f"leasehold: {program} left {name} running (pid {pid}, {size(used)}) in its scope; "
            + (done.reason if done.how == "none" else "it no longer has the lease's limits")
        )
    if found is not None:
        reply.output = {
            "systemMessage": " ".join([found.person, *said]),
            "hookSpecificOutput": {
                "hookEventName": event.get("hook_event_name") or "PostToolUseFailure",
                "additionalContext": found.agent,
            },
        }
    elif said:
        reply.output = {"systemMessage": " ".join(said)}
    return reply


def _outcome(event: Mapping, collected: attach.Collected | None) -> dict | None:
    """What the command's scope used, to send with the release; None when it had none, or its
    shell was adopted by scan, after the command had started: what it used before that was
    not counted, and an outcome too small would teach leasehold too little memory."""
    if collected is None or collected.usage is None or collected.attached != "shell":
        return None
    status = exit_code(event)
    if status is None and event.get("hook_event_name") == "PostToolUse":
        status = 0
    return {
        "rule": collected.rule,
        "repo": collected.repo,
        **dataclasses.asdict(collected.usage),
        "wall_s": collected.wall_s or 0.0,
        "exit_status": status,
    }


_EXIT_CODE = re.compile(r"Exit code (\d+)")


def exit_code(event: Mapping) -> int | None:
    """The Bash command's exit code, from the first line of a `PostToolUseFailure` event's
    `error` (`Exit code 137`); None when the event gives none."""
    error = event.get("error")
    found = _EXIT_CODE.match(error) if isinstance(error, str) else None
    return int(found.group(1)) if found else None


def _memory(
    event: Mapping,
    program: str,
    mark: str | None,
    *,
    collected: attach.Collected | None,
    env: Mapping[str, str],
) -> verdict.Verdict | None:
    """The memory verdict on the command `program` that the event reports ended: `certain`
    when its scope counted an OOM kill (`collected`), else read against the host's counters
    noted before it started (`mark`); None when it ended some other way, or nothing was
    noted."""
    status = exit_code(event)
    usage = None if collected is None else collected.usage
    if status is None or (mark is None and usage is None):
        return None
    peak = largest = None
    tree = counted = None
    if usage is not None:
        peak, counted = usage.peak_bytes, usage.oom_kills
        tree = collected.tree
    # Certain when its scope counted the kill, or systemd says its own cap ended it.
    ending = host.current().ended(status, host.Mark.decode(mark), tree=tree, counted=counted)
    seen = None if collected is None else collected.largest
    # Named only when it held most of what was capped: otherwise it was read too early.
    if ending.memory == "cap" and seen is not None and seen[2] * 2 >= (ending.limit_bytes or 0):
        largest = seen
    agent_limit = env.get(verdict.AGENT_LIMIT_ENV) or None
    return verdict.describe(
        ending, program, status=status, peak_bytes=peak, limit_bytes=ending.limit_bytes,
        agent_limit=agent_limit, largest=largest,
    )  # fmt: skip
