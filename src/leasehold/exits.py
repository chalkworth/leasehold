"""The exit codes `leasehold` commands share, beyond a wrapped command's own, and how a command
ends by a signal.

| Code | Meaning |
|---|---|
| 69 | the daemon could not be reached (`EX_UNAVAILABLE`) |
| 75 | still waiting at `--timeout`: busy, not failed (`EX_TEMPFAIL`) |
| 76 | the lease was revoked (or, for `lend`, lost) |
| 77 | refused: the request could never be granted, or the daemon answered it with an error |
| 127 | the command could not be started |
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal

EXIT_UNAVAILABLE = 69
EXIT_BUSY = 75
EXIT_REVOKED = 76
EXIT_REFUSED = 77
EXIT_NOT_STARTED = 127


def end_by(sig: int) -> int:
    """End this process by `sig`, with the signal's default action (and no core file), as a
    process ends that the signal killed: a shell waiting on it then knows it was interrupted,
    and stops the loop or script it ran it from. Returns 128 + `sig`, the shell's code for that
    end, only when the process survives it."""
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))
    signal.signal(sig, signal.SIG_DFL)
    os.kill(os.getpid(), sig)
    return 128 + sig
