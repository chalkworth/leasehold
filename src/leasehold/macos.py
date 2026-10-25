"""What leasehold says, once, on macOS: what does not run there yet and what works meanwhile.

Shown by `serve`, `init`, `setup` and `doctor`, never by a command an agent's hook runs.
"""

from __future__ import annotations

import sys

NOTE = (
    "macOS is untested. A daemon (`leasehold serve --socket PATH`), leases, `hold`, the shims "
    "and the SDK run, but the daemon counts slots only (it reads no memory or load), and "
    "nothing contains a command, caps its memory or reports a memory kill."
)


def note(platform: str | None = None) -> str | None:
    """`NOTE` on macOS (`platform`, default this one), else None."""
    return NOTE if (platform or sys.platform) == "darwin" else None
