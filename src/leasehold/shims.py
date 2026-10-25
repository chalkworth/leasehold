"""PATH shims: small scripts named like heavy programs (`pytest`, `cargo`, `make`, ...) in a
directory early on PATH, each running the real program under a lease when its arguments match
a `[commands]` rule (`leasehold shims install`).

They work for any agent, and for people: whatever runs `cargo build` through PATH gets the
lease, for the life of the process itself, so a background command is covered too. What
bypasses PATH bypasses them: an absolute path (`/usr/bin/make`), `.venv/bin/pytest`, `python -m
pytest`. The agent hooks (`leasehold.claude_code`) read the command line instead, and see
those.

A shim passes straight to the real program, with no lease, when one is held already (a
`make` under a leased `cargo`, any command under `leasehold hold`: `LEASEHOLD_LEASE_ID` is
set; or an agent hook's lease for this program under this agent, `leasehold.hooknotes`, whose
job count and threads the program is then given, `covered_env`), when
`LEASEHOLD_SHIMS=off`, when no rule matches, and when no daemon answers. Under a lease that
set the job variables (`jobs.LEASEHOLD_SET`), a program whose own rule gives fewer threads
(`pytest -n 4` under `hold -- make test`) runs with that thread count (`within_lease`). On a
busy machine the shim does what `[commands] when_busy` says: wait `wait_s`, then exit 75, or
run without a lease. Under an agent whose tool timeout `LEASEHOLD_TOOL_TIMEOUT_S` gives, it
waits at most half of that (`leasehold.toolwait`), so the agent hears why the command did not
run before its own timeout ends the call. A program granted after a wait of
`busy.WAIT_NOTED_S` or more says how long it waited, and behind what. When the daemon
says no (a refusal, an error answer under 500, or a matching rule no lease could ever be
granted for) the shim exits 77 without running the program; when it cannot answer (an error
of 500 or more) the program runs without a lease, and the shim says why.

A shim asks for the matching rule's weight (a count or a range), unless the caller's
environment sets `LEASEHOLD_WANT` (`leasehold.weights`), which it asks for instead, cut to the
most the rule's lease could ever be granted. The program runs sized to the weight granted.

A shim costs a Python start (about 0.1 s) on every run of its program, `--version` included.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from leasehold import busy, discovery, hooknotes, jobs, measure, shell, toolwait, weights
from leasehold.client import ApiError, Client, Unavailable
from leasehold.exits import EXIT_BUSY, EXIT_NOT_STARTED, EXIT_REFUSED, EXIT_UNAVAILABLE
from leasehold.hold import Waited, lease_request, run_leased, wait_for_grant

#: The first line after the shebang of every shim this module writes: what `remove` and
#: `install` (without `--force`) recognise as theirs.
MARKER = "# leasehold shim"


def default_dir(env: Mapping[str, str]) -> Path:
    data = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME", "~"), ".local", "share")
    return Path(data).expanduser() / "leasehold" / "shims"


def script(program: str, python: str, directory: Path) -> str:
    """The shim: the interpreter leasehold is installed with, the directory to skip when
    looking for the real program, and the program's name, each quoted for sh."""
    run = " ".join(_sh_quote(w) for w in (python, "-m", "leasehold.shims", str(directory), program))
    return (
        "#!/bin/sh\n"
        f"{MARKER} for {program}: runs it under a lease when a [commands] rule matches.\n"
        "# `leasehold shims remove` deletes it.\n"
        f'exec {run} "$@"\n'
    )


def _sh_quote(word: str) -> str:
    return "'" + word.replace("'", "'\\''") + "'"


def is_shim(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return MARKER.encode() in f.read(256)
    except OSError:
        return False


def real(program: str, path_env: str, shims: Path) -> str | None:
    """The program a shell would run for `program` were the shims not there: the first
    executable of that name on `path_env` outside `shims`, and not a shim itself."""
    skip = os.path.realpath(shims)
    for entry in path_env.split(os.pathsep):
        entry = entry or "."  # an empty entry is the current directory, as the shell reads it
        if os.path.realpath(entry) == skip:
            continue
        candidate = os.path.join(entry, program)
        if (
            os.path.isfile(candidate)
            and os.access(candidate, os.X_OK)
            and not is_shim(Path(candidate))
        ):
            return candidate
    return None


def command(target: str, args: list[str]) -> list[str]:
    """How to run `target`: itself, or through /bin/sh when it is a script with no `#!` line,
    which a shell runs that way but exec refuses (ENOEXEC)."""
    try:
        with open(target, "rb") as f:
            head = f.read(4)
    except OSError:
        return [target, *args]
    if head.startswith(b"#!") or head == b"\x7fELF":
        return [target, *args]
    return ["/bin/sh", target, *args]


def install(
    directory: Path, programs: Iterable[str], *, python: str, force: bool = False
) -> list[tuple[str, str]]:
    """Write a shim for each of `programs` into `directory`: (program, what happened)."""
    directory.mkdir(parents=True, exist_ok=True)
    done = []
    for program in sorted(set(programs)):
        if "/" in program or not program or program.startswith("."):
            done.append((program, "skipped: not a plain program name"))
            continue
        target = directory / program
        if target.exists() and not is_shim(target) and not force:
            done.append((program, "skipped: a file that is not a shim is there (--force)"))
            continue
        partial = directory / f".{program}.{os.getpid()}"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
        with os.fdopen(fd, "w") as out:
            out.write(script(program, python, directory.resolve()))
        os.chmod(partial, 0o755)  # whatever the umask
        os.replace(partial, target)
        done.append((program, "installed"))
    return done


def installed(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.is_file() and is_shim(p))


def remove(directory: Path, programs: Iterable[str] | None = None) -> list[str]:
    """Delete the shims in `directory` (those named, or all); never a file that is not one."""
    chosen = installed(directory) if programs is None else programs
    gone = []
    for program in chosen:
        path = directory / program
        if is_shim(path):
            path.unlink()
            gone.append(program)
    return gone


def run(shims: Path, program: str, args: list[str], env: Mapping[str, str]) -> int:
    """What the shim in `shims` does: run the real `program`, under a lease when a rule
    matches. Returns only on a lease (the command's exit code) or an error; otherwise becomes
    the program."""
    target = real(program, env.get("PATH", os.defpath), shims)
    if target is None:
        sys.stderr.write(f"leasehold shim: {program}: not found on PATH\n")
        return EXIT_NOT_STARTED
    argv = command(target, args)
    target = argv[0]
    if env.get("LEASEHOLD_SHIMS") == "off":
        os.execv(target, argv)
    if env.get("LEASEHOLD_LEASE_ID"):  # no second lease, but perhaps fewer threads
        os.execve(target, argv, within_lease(program, args, env))
    covered = hooknotes.covering(env, program, os.getpid())
    if covered is not None:  # an agent hook holds this command's lease already
        os.execve(target, argv, covered_env(env, covered))
    if weights.ENV_WANT in env and hooknotes.under_agent(env, os.getpid()):
        # The agent's LEASEHOLD_WANT is what its hook asked for its own command's lease.
        env = {k: v for k, v in env.items() if k != weights.ENV_WANT}
    try:
        client = Client(discovery.find(None, None, env))
        settings = shell.served(client.commands())
    except (discovery.DiscoveryError, Unavailable, OSError):
        os.execv(target, argv)  # no daemon: as if the shim were not there
    except ApiError as exc:
        if exc.status < 500:  # the daemon answered no
            _say(f"not running {program}: the daemon refused to list its rules: {exc}")
            return EXIT_REFUSED
        _say(f"the daemon cannot answer ({exc}); running {program} without a lease")
        os.execv(target, argv)
    found = shell.match_programs(settings.rules, [(program, args)])
    if found is None:
        os.execv(target, argv)
    rule = found.rule
    threads = _threads(settings.rules, program, args, env) or rule.threads
    if rule.never is not None:
        _say(f"not running {program}: refused: {rule.never}")
        return EXIT_REFUSED
    least, most = weights.asked(env, rule.weight, rule.max_weight, _say, rule.ceiling)
    request = lease_request(
        pool=rule.pool,
        weight=least,
        max_weight=most,
        labels={"shim": program, "cmd": " ".join([program, *args])[:120]},
        session=env.get("LEASEHOLD_SESSION") or None,
        memory=rule.memory,
        memory_per_slot=rule.memory_per_slot,
        rule=rule.key,
        repo=measure.repository() if rule.key is not None else None,
    )
    wait_s = toolwait.limit(env, settings.wait_s)
    waited = Waited()
    code, answer = wait_for_grant(client, request, wait_s, _say, waited=waited)
    if code is None:
        if waited.seconds >= busy.WAIT_NOTED_S:
            sys.stderr.write(busy.waited(program, waited.seconds, waited.behind) + "\n")
        return run_leased(
            client, argv, request, answer, memory=rule.memory, threads=threads,
            memory_per_slot=rule.memory_per_slot, rule=rule.key, memory_max=rule.memory_max,
        )  # fmt: skip
    if code == EXIT_BUSY and (cut := toolwait.said(env, settings.wait_s)):
        _say(cut)
    if code == EXIT_BUSY and settings.when_busy == "deny":
        why, todo = busy.told(client, answer, pool=rule.pool, waited_s=wait_s)
        _say(f"not running {program}: {why}. {todo}")
        return EXIT_BUSY
    if code == EXIT_BUSY:
        why, _ = busy.told(client, answer, pool=rule.pool, waited_s=wait_s)
        _say(f"{why}; running {program} without a lease")
    elif code == EXIT_UNAVAILABLE:
        why = answer.get("reason") or "no daemon answers"
        _say(f"{why}; running {program} without a lease")
    else:
        # Refused, or an error answer under 500: the daemon said no.
        why = answer.get("reason") or "the daemon refused the request"
        _say(f"not running {program}: refused: {why}")
        return EXIT_REFUSED
    os.execv(target, argv)


#: What reading the rules inside a lease may raise: no daemon, a daemon that does not answer in
#: time or answers no, and rows that are not rules.
_ANY_FAULT = (
    discovery.DiscoveryError, Unavailable, ApiError, OSError, LookupError, ValueError,
    TypeError, AttributeError, re.error,
)  # fmt: skip
#: How long a shim inside a lease waits for the daemon's rules before running as it is.
WITHIN_LEASE_TIMEOUT_S = 0.3


def _threads(
    rules: Sequence[shell.Rule], program: str, args: list[str], env: Mapping[str, str]
) -> int | str | None:
    """`program`'s thread count by the rules (`shell.threads`), reading a pytest run's config
    and `PYTEST_ADDOPTS` from the directory it runs in; None when no rule matches or anything
    fails, so the program still runs."""
    try:
        return shell.threads(rules, [(program, args)], env=env, cwd=Path.cwd)
    except Exception:  # noqa: BLE001 - sizing threads must never stop the program running
        return None


def covered_env(env: Mapping[str, str], lease_id: str) -> dict[str, str]:
    """The environment a program runs with under an agent hook's lease `lease_id`: `env` with
    `LEASEHOLD_LEASE_ID`, without `LEASEHOLD_WANT`, and with the job and thread variables `hold`
    sets (`jobs.sized_env`) for the job count and thread count the hook's note gives. Sizing
    left in `env` by another lease is not this lease's and is dropped first; a value the user
    set is kept, and `LEASEHOLD_THREADS` is the thread count chosen. `LEASEHOLD_WEIGHT` is the
    weight the note says was granted, and is dropped when the note does not say. A note that
    gives no sizing leaves the job and thread variables as they are."""
    out = {k: v for k, v in env.items() if k not in (weights.ENV_WANT, "LEASEHOLD_WEIGHT")}
    granted = hooknotes.weight(env, lease_id)
    if granted is not None:
        out["LEASEHOLD_WEIGHT"] = str(granted)
    sizing = hooknotes.sizing(env, lease_id)
    if sizing is None:
        return out | {"LEASEHOLD_LEASE_ID": lease_id}
    out = jobs.unsized_env(out) | {"LEASEHOLD_LEASE_ID": lease_id}
    return jobs.sized_env(out, *sizing, cap=True)


def within_lease(program: str, args: list[str], env: Mapping[str, str]) -> dict[str, str]:
    """The environment `program` runs with inside a lease already held: `env`, with each thread
    variable the lease set lowered to the count `program`'s own rule gives (`_threads`,
    `jobs.resized`) where it is higher; never raised. `OPENBLAS_NUM_THREADS` and
    `MKL_NUM_THREADS` follow the user's own `OMP_NUM_THREADS` instead, even when it is higher.
    At a thread count of 1 already, the daemon is not asked and the count stays 1. `env` as it
    is when the lease recorded no variables, no rule matches, the daemon does not answer with
    rules within `WITHIN_LEASE_TIMEOUT_S`, or anything else fails."""
    try:
        lease_jobs = jobs.count_of(env.get("LEASEHOLD_JOBS"))
        if not jobs.recorded(env) or lease_jobs is None:
            return dict(env)
        if 1 in (lease_jobs, jobs.count_of(env.get("LEASEHOLD_THREADS"))):
            return jobs.resized(env, 1) or dict(env)
        try:
            client = Client(discovery.find(None, None, env), timeout=WITHIN_LEASE_TIMEOUT_S)
            rules = shell.served(client.commands()).rules
        except _ANY_FAULT:
            return dict(env)
        wanted = _threads(rules, program, args, env)
        if wanted is None:
            return dict(env)
        return jobs.resized(env, wanted) or dict(env)
    except Exception:  # noqa: BLE001 - whatever goes wrong here, the program runs as it is
        return dict(env)


def _say(message: str) -> None:
    sys.stderr.write(f"leasehold shim: {message}\n")
    sys.stderr.flush()


def main() -> int:
    if len(sys.argv) < 3:
        sys.stderr.write("usage: python -m leasehold.shims SHIM_DIR PROGRAM [ARGS...]\n")
        return 2
    return run(Path(sys.argv[1]), sys.argv[2], sys.argv[3:], os.environ)


if __name__ == "__main__":
    sys.exit(main())
