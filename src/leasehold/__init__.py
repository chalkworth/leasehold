"""leasehold: fair, load-aware admission for one shared Linux machine.

The library:

* `model`: pools, the machine's slots and how they are split (`PoolSet`);
* `admission`: the pure decision of what fits now (reservations, priority, turns between users
  and between each user's sessions, FIFO per session, no backfill);
* `ledger`: the machine-wide SQLite ledger of leases, the queue and recent submits that applies
  it, its holders fenced by `process` identity or by a TTL;
* `quota`: the submit-time per-user queued limit and create rate;
* `client`: a client for the machine agent's socket API;
* `leasing`: leases for Python callers, the names this package exports, and `joining`:
  an agent session the daemon watches (`session()`).

The machine agent (`leasehold serve`) is built from `machine`, `agent`, `api` and `server`.
Only the standard library and pydantic.

The names in `__all__` are the supported API. Until 1.0 a minor release may change them, and
`CHANGELOG.md` lists every such change.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from leasehold.joining import Session, session
    from leasehold.leasing import (
        Busy,
        Cancelled,
        DaemonError,
        Lease,
        LeaseError,
        LeaseLost,
        Refused,
        Unavailable,
        Waiting,
        find_daemon,
        lease,
    )
    from leasehold.supervise import Command, NotStarted

#: Where each public name lives. `import leasehold` imports none of them: a name is imported
#: the first time it is read, so a process that needs only a submodule (the hook, the attach
#: step) does not pay for the whole SDK.
_HOME = {
    **dict.fromkeys(
        (
            "Busy",
            "Cancelled",
            "DaemonError",
            "Lease",
            "LeaseError",
            "LeaseLost",
            "Refused",
            "Unavailable",
            "Waiting",
            "find_daemon",
            "lease",
        ),
        "leasehold.leasing",
    ),
    **dict.fromkeys(("Command", "NotStarted"), "leasehold.supervise"),
    **dict.fromkeys(("Session", "session"), "leasehold.joining"),
}

__all__ = [
    "Busy",
    "Cancelled",
    "Command",
    "DaemonError",
    "Lease",
    "LeaseError",
    "LeaseLost",
    "NotStarted",
    "Refused",
    "Session",
    "Unavailable",
    "Waiting",
    "__version__",
    "find_daemon",
    "lease",
    "session",
]

#: The one place the version is written; the build reads it from here (`[tool.hatch.version]`).
__version__ = "0.5.0"


def __getattr__(name: str) -> Any:
    home = _HOME.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(home), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
