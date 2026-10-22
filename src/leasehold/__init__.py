"""leasehold: fair, load-aware admission for one shared Linux machine.

The library:

* `model`: pools, the machine's slots and how they are split (`PoolSet`);
* `admission`: the pure decision of what fits now (reservations, priority, turns between users
  and between each user's sessions, FIFO per session, no backfill);
* `ledger`: the machine-wide SQLite ledger of leases, the queue and recent submits that applies
  it, its holders fenced by `process` identity or by a TTL;
* `quota`: the submit-time per-user queued limit and create rate;
* `client`: a client for the machine agent's socket API;
* `leasing`: leases for Python callers, the names this package exports.

The machine agent (`leasehold serve`) is built from `machine`, `agent`, `api` and `server`.
Only the standard library and pydantic.

The names in `__all__` are the supported API. Until 1.0 a minor release may change them, and
`CHANGELOG.md` lists every such change.
"""

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

__all__ = [
    "Busy",
    "Cancelled",
    "DaemonError",
    "Lease",
    "LeaseError",
    "LeaseLost",
    "Refused",
    "Unavailable",
    "Waiting",
    "__version__",
    "find_daemon",
    "lease",
]

#: The one place the version is written; the build reads it from here (`[tool.hatch.version]`).
__version__ = "0.3.0"
