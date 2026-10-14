"""The terminal, for a command `hold` runs in a process group of its own.

When `hold` runs in the foreground of a terminal (`owned`), the command's group is given the
terminal (`hand_to`), so it can read it, and Ctrl-C and Ctrl-Z reach it as they would reach the
command run by the shell. While the command is stopped (Ctrl-Z, or reading the terminal from the
background), `hold` gives the terminal back and stops too, so the shell sees the job stop; when
the shell continues `hold` (`fg`, `bg`), it continues the command, handing it the terminal again
only if it is in the foreground (`follow_stops`). When the command ends, the terminal is given
back to `hold`'s own group (`take_back`).
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import Iterator

#: The terminal: `hold`'s standard input.
FD = 0


def owned() -> bool:
    """Whether standard input is a terminal whose foreground is this process's group."""
    try:
        return os.isatty(FD) and os.tcgetpgrp(FD) == os.getpgrp()
    except OSError:
        return False


def _set_foreground(pgid: int) -> None:
    """Make `pgid` the terminal's foreground group (SIGTTOU ignored: a group may hand the
    terminal on only while ignoring it)."""
    previous = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        with contextlib.suppress(OSError):
            os.tcsetpgrp(FD, pgid)
    finally:
        signal.signal(signal.SIGTTOU, previous)


def hand_to(pgid: int) -> None:
    """Give the terminal to the group `pgid` and continue it, in case it stopped reading the
    terminal before it had it."""
    _set_foreground(pgid)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGCONT)


def take_back(pgid: int) -> None:
    """Give the terminal back to this process's group, if `pgid` has it."""
    with contextlib.suppress(OSError):
        if os.tcgetpgrp(FD) == pgid:
            _set_foreground(os.getpgrp())


def _stopped(pid: int) -> bool:
    """Whether `pid` has stopped since last asked (its exit is left for `wait` to reap)."""
    try:
        info = os.waitid(os.P_PID, pid, os.WSTOPPED | os.WNOHANG)
    except ChildProcessError:
        return False
    return info is not None and info.si_code == os.CLD_STOPPED


@contextlib.contextmanager
def follow_stops(pid: int) -> Iterator[None]:
    """While inside, when the command `pid` (a group leader) stops, take the terminal back and
    stop this process; once continued, continue the command, with the terminal if this process
    is in the foreground again."""

    def on_child(_sig, _frame) -> None:
        if not _stopped(pid):
            return
        take_back(pid)
        os.kill(os.getpid(), signal.SIGSTOP)
        if owned():
            hand_to(pid)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGCONT)

    previous = signal.signal(signal.SIGCHLD, on_child)
    try:
        yield
    finally:
        signal.signal(signal.SIGCHLD, previous)
