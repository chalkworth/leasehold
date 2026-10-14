"""The submit-time quota (leasehold.quota): a user over their pool's queued limit or create
rate is refused with `quota_exceeded`, a refused submit is not counted, and a pool with
neither limit never touches the ledger."""

from __future__ import annotations

import pytest
from support import Clock

from leasehold.admission import AdmissionPolicy
from leasehold.ledger import Ledger, LedgerItem, Queued
from leasehold.model import Pool, PoolSet
from leasehold.quota import QuotaExceeded, check_submit, limited


def pools(**ci) -> dict[str, Pool]:
    """A one-slot machine's pools: `ci`, with the limits given."""
    return PoolSet(slots=1, pools={"ci": Pool(max=1, **ci)}).pools


def ci_item(item_id: str, user: str = "ann") -> LedgerItem:
    return LedgerItem("ci", item_id, "work", user, "ci", 1)


@pytest.fixture
def clock() -> Clock:
    return Clock(1000.0)


@pytest.fixture
def ledger(tmp_path, clock) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3", now=clock)


class NoLedger:
    """A ledger stand-in that fails the test if a submit is recorded."""

    def record_submit(self, *args, **kwargs):
        raise AssertionError("the ledger was asked")


def test_a_pool_without_limits_never_touches_the_ledger():
    assert not limited(pools(), "ci")
    assert limited(pools(per_user_max_queued=1), "ci")
    assert limited(pools(create_rate_per_min=1), "ci")
    check_submit(NoLedger(), pools(), "ann", "ci")


def test_over_the_queued_quota_is_refused_with_quota_exceeded(ledger):
    limits = pools(per_user_max_queued=2)
    full = AdmissionPolicy.from_pools(PoolSet(slots=1, pools=limits))
    ledger.try_admit(ci_item("running"), full)
    for i in range(2):
        check_submit(ledger, limits, "ann", "ci")
        assert isinstance(ledger.try_admit(ci_item(f"q{i}"), full), Queued)
    with pytest.raises(QuotaExceeded) as info:
        check_submit(ledger, limits, "ann", "ci")
    assert info.value.code == "quota_exceeded"
    assert (info.value.pool, info.value.user) == ("ci", "ann")
    assert "2 item(s) waiting" in info.value.message
    check_submit(ledger, limits, "bob", "ci")  # per user: bob is not held to ann's queue
    ledger.dequeue(("ci", "q0"))
    check_submit(ledger, limits, "ann", "ci")  # room again once one of hers leaves the line


def test_the_create_rate_is_a_sliding_minute_per_user(ledger, clock):
    limits = pools(create_rate_per_min=3)
    for _ in range(3):
        check_submit(ledger, limits, "ann", "ci")
        clock.now += 10
    with pytest.raises(QuotaExceeded, match="per minute"):
        check_submit(ledger, limits, "ann", "ci")  # 3 in the last 30 s
    check_submit(ledger, limits, "bob", "ci")
    clock.now += 31  # the first submit (61 s ago) left the window
    check_submit(ledger, limits, "ann", "ci")
    with pytest.raises(QuotaExceeded):  # and the refused one was never counted
        check_submit(ledger, limits, "ann", "ci")
