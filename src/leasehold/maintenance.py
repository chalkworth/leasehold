"""What waiters are told about a maintenance lease: who holds the machine, why, and until when.

A maintenance lease carries its end and its reason as labels: `until`, a time in RFC 3339 (UTC,
`2026-10-21T07:00:00Z`), and `reason`, free text. A `name` label names the holder for scripts;
without one the holder is the lease's user. The end is advisory: leasehold shows it to
waiters and never ends the lease by it. The lease lives as long as its holder renews it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: The labels a maintenance lease is described by.
UNTIL, REASON, NAME = "until", "reason", "name"

_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_until(value: str, now: datetime | None = None) -> str:
    """`value` as the RFC 3339 UTC time `until` holds: `HH:MM` is the next such time in UTC
    after `now` (default: now), and an ISO 8601 time is taken as given (UTC when it names no
    zone). A ValueError for anything else."""
    now = now or datetime.now(UTC)
    clock = _CLOCK.match(value)
    if clock is not None:
        at = now.astimezone(UTC).replace(
            hour=int(clock.group(1)), minute=int(clock.group(2)), second=0, microsecond=0
        )
        if at <= now:
            at += timedelta(days=1)
    else:
        try:
            at = _iso(value)
        except ValueError:
            raise ValueError(f"{value!r} is not HH:MM or an ISO 8601 time") from None
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def describe(lease: dict, *, held: bool, now: float) -> dict:
    """What `status` and a waiting request say of the maintenance lease `lease` (a lease or a
    queued item as the API shows it): its `lease_id`, `state` (`held`, or `draining` while
    it waits for the machine to empty), `user`, `name`, `reason`, `until` (as its label gives
    it, or None) and `message`, the words waiters see."""
    labels = lease.get("labels") or {}
    until = labels.get(UNTIL)
    reason = labels.get(REASON)
    name = labels.get(NAME)
    state = "held" if held else "draining"
    words = "machine in maintenance" if held else "machine draining for maintenance"
    if until:
        words += f" until {_when(until, now)}"
    if reason or name:
        words += f" ({reason or name})"
    return {
        "lease_id": lease["lease_id"],
        "state": state,
        "user": lease.get("user"),
        "name": name or lease.get("user"),
        "reason": reason,
        "until": until,
        "message": words,
    }


def holder_line(info: dict) -> str:
    """One line a script can read: `held by NAME (maintenance) until RFC3339`, or `draining
    for NAME ...` while the lease waits for the machine to empty, without the `until` part
    when the lease gives no end."""
    verb = "held by" if info.get("state") == "held" else "draining for"
    line = f"{verb} {info.get('name') or 'someone'} (maintenance)"
    return f"{line} until {info['until']}" if info.get("until") else line


def _when(until: str, now: float) -> str:
    """`until` for people: `07:00 UTC` within a day of `now`, else with its date; as given
    when it is not a time."""
    try:
        at = _iso(until)
    except ValueError:
        return until
    if timedelta(0) <= at - datetime.fromtimestamp(now, UTC) < timedelta(days=1):
        return at.strftime("%H:%M UTC")
    return at.strftime("%Y-%m-%d %H:%M UTC")


def _iso(value: str) -> datetime:
    """An ISO 8601 time in UTC, taken as UTC when it names no zone."""
    at = datetime.fromisoformat(value)
    return (at if at.tzinfo is not None else at.replace(tzinfo=UTC)).astimezone(UTC)
