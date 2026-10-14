"""The submit-time quota: a user over their pool's `per_user_max_queued`, or over its
`create_rate_per_min`, is refused at submission (`QuotaExceeded`, code `quota_exceeded`) rather
than queued. Checked and counted in one ledger transaction, so two racing submits cannot both
slip under the limit."""

from __future__ import annotations

from collections.abc import Mapping

from leasehold.ledger import Ledger
from leasehold.model import Pool


class QuotaExceeded(RuntimeError):
    """A submit over its pool's per-user limit; `message` says which limit and by how much."""

    code = "quota_exceeded"

    def __init__(self, message: str, *, pool: str, user: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.pool = pool
        self.user = user


def limited(pools: Mapping[str, Pool], pool: str) -> bool:
    """Whether `pool` sets a per-user queued limit or a create rate: only then does a submit
    touch the ledger, so pools that set neither never need one open."""
    limits = pools[pool]
    return limits.per_user_max_queued is not None or limits.create_rate_per_min is not None


def check_submit(ledger: Ledger, pools: Mapping[str, Pool], user: str | None, pool: str) -> None:
    """Count one submit by `user` into `pool`, or raise `QuotaExceeded` (and count nothing). A
    pool with neither limit set costs no ledger write. `pools` is the pools in effect, such as
    a `PoolSet`'s."""
    if not limited(pools, pool):
        return
    limits = pools[pool]
    reason = ledger.record_submit(
        user, pool, max_queued=limits.per_user_max_queued, rate_per_min=limits.create_rate_per_min
    )
    if reason is not None:
        raise QuotaExceeded(reason, pool=pool, user=user)
