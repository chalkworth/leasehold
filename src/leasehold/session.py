"""`leasehold run -- <agent>`: one agent session, its heavy commands under leases, and nothing
of it left running once it ends.

`run` gives the session an id (`LEASEHOLD_SESSION`, so its leases take turns with the user's
other sessions), puts the PATH shims first on its PATH where they are installed, and starts the
agent in the foreground, on the terminal, as a shell would. Where commands can be contained
(`leasehold.contain`), the agent runs in a scope of a slice of its own,
`leasehold-<session>_<token>.slice` (the token is this run's alone, so no two runs share a slice
whatever their session names), named to everything under it in `LEASEHOLD_SLICE`: the scopes of
the commands it holds leases for go in the same slice. When the agent exits, whatever it left in
that slice (a test server, a stray build) gets SIGTERM, then SIGKILL after a grace, unless
`leave_running`; nothing outside the slice is touched, and a clean-up that fails is reported
without changing the agent's exit code. Without containment the agent runs the same way,
uncontained, and nothing is stopped.

No daemon is needed to start: the shims and `hold` find one when they need it.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from leasehold import contain, shims
from leasehold.exits import EXIT_NOT_STARTED

ENV_SESSION = "LEASEHOLD_SESSION"
#: How long what an agent left behind has between SIGTERM and SIGKILL.
STOP_GRACE_S = 10.0


def new_session(argv: Sequence[str]) -> str:
    """A session id naming the agent: `claude_3f9a1c`."""
    name = contain.unit_part(Path(argv[0]).name)[:24] if argv else "session"
    return f"{name}_{uuid.uuid4().hex[:6]}"


def session_env(
    env: Mapping[str, str], session: str, *, slice_: str | None, shim_dir: Path | None
) -> dict[str, str]:
    """The agent's environment: the session, its slice when contained, and the shims first on
    PATH when there are any."""
    out = {**env, ENV_SESSION: session}
    out.pop(contain.ENV_SLICE, None)
    if slice_:
        out[contain.ENV_SLICE] = slice_
    if shim_dir is not None:
        rest = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != str(shim_dir)]
        out["PATH"] = os.pathsep.join([str(shim_dir), *rest])
    return out


def _say(message: str) -> None:
    sys.stderr.write(f"leasehold run: {message}\n")
    sys.stderr.flush()


def _pass(_signum, _frame) -> None:
    """The agent has the terminal: a Ctrl-C reaches it, and `run` waits for it to end."""


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    session: str | None = None,
    leave_running: bool = False,
    contain_mode: str = "auto",
    grace_s: float = STOP_GRACE_S,
    quiet: bool = False,
) -> int:
    """Run the agent `argv` as one session (module docstring); its exit code. `contain_mode`
    `off` runs it uncontained whatever `contain.probe` finds."""
    if not argv:
        _say("no command given (leasehold run [options] -- agent ...)")
        return 2
    env = dict(os.environ if env is None else env)
    session = session or new_session(argv)
    found = (
        contain.Containment(False, "[contain] mode is off")
        if contain_mode == "off"
        else contain.probe(env=env)
    )
    shim_dir = shims.default_dir(env)
    if not shims.installed(shim_dir):
        shim_dir = None
    prefix = None
    slice_ = None
    if found.enforced:
        if shutil.which(argv[0], path=env.get("PATH")) is None:
            _say(f"cannot run {argv[0]}: not found or not executable")
            return EXIT_NOT_STARTED
        token = uuid.uuid4().hex[:8]
        slice_ = contain.session_slice(session, token)
        unit = f"agent-{contain.unit_part(session)[:40]}_{token}.scope"
        prefix = contain.scope_argv(unit, slice_, weight=1)
    if not quiet:
        shimmed = "shims on PATH" if shim_dir else "no shims (`leasehold shims install`)"
        _say(f"session {session}: {found.describe()}; {shimmed}")
    # Caught, not ignored, in this process: the agent starts with the default dispositions (exec
    # resets caught ones), so Ctrl-C and Ctrl-\ from the terminal reach it, and not us.
    previous = {s: signal.signal(s, _pass) for s in (signal.SIGINT, signal.SIGQUIT)}
    try:
        child_env = session_env(env, session, slice_=slice_, shim_dir=shim_dir)
        try:
            child, uncontained = contain.spawn(argv, prefix, env=child_env)
        except OSError as exc:
            _say(f"cannot run {argv[0]}: {exc}")
            return EXIT_NOT_STARTED
        if uncontained:
            _say(uncontained)
            slice_ = None
            child_env.pop(contain.ENV_SLICE, None)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous.setdefault(sig, signal.signal(sig, lambda s, _f: child.send_signal(s)))
        code = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if slice_ is not None and not leave_running:
        try:
            if contain.stop_slice(slice_, grace_s) and not quiet:
                _say(f"stopped what the session left running in {slice_}")
        except contain.CleanupError as exc:
            _say(f"could not stop what the session left running: {exc}")
    return code if code >= 0 else 128 - code
