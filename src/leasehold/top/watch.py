"""Polling the daemon for the live view: `Watch` keeps the last status, recent events and
pressure history."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from leasehold.client import ApiError, Client, Unavailable
from leasehold.top.render import RESOURCES

#: Events `run` keeps.
KEEP_EVENTS = 200
#: Pressure samples `run` keeps per resource, one per poll.
HISTORY = 150


class Watch:
    """What the live view knows: the last status, recent events and pressure history, and
    whether the last poll failed. `poll` asks the daemon once."""

    def __init__(self, client: Client, clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.clock = clock
        self.status: dict | None = None
        self.received = 0.0
        self.events: deque[dict] = deque(maxlen=KEEP_EVENTS)
        self.history: dict[str, deque[float]] = {r: deque(maxlen=HISTORY) for r in RESOURCES}
        self.since = 0
        self.error: str | None = None

    def poll(self) -> bool:
        """Ask for the status and the events since the last poll; False (and `error` set) when
        the daemon did not answer."""
        try:
            status = self.client.status()
            batch = self.client.events(self.since)
        except (Unavailable, ApiError) as exc:
            self.error = str(exc)
            return False
        self.error = None
        self.status = status
        self.received = self.clock()
        self.events.extend(batch.get("events") or [])
        self.since = int(batch.get("next") or 0)
        pressure = (status.get("load") or {}).get("pressure") or {}
        for res in RESOURCES:
            some = ((pressure.get(res) or {}).get("some") or {}).get("avg10")
            if some is not None:
                self.history[res].append(float(some))
        return True

    def now(self) -> float:
        """Daemon time now: its last `time` plus what has passed here since, so ages are right
        whatever this machine's clock says."""
        assert self.status is not None
        return float(self.status.get("time") or 0.0) + (self.clock() - self.received)
