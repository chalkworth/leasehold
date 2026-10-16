"""How `hold` follows an elastic lease's target (`leasehold.elastic`): the holder's side of
the promise to take slots when they are offered and give them back when asked.

`hold --jobserver` asks for a plain range. The lease becomes elastic (`POST /v1/elastic`)
only once a job takes a token from the jobserver, which shows the command's jobs follow it. A
command that never takes one (`make -jN` runs its own jobserver, for one) keeps the size it was
granted, neither growing nor giving any back. A daemon from before elastic leases leaves it so
too.

A `Follower` is told each renewal's answer (`heard`) and works toward the target (`step`).
When the target is above the lease's weight, it asks the daemon for the target
(`POST /v1/resize`). Once the daemon says yes, it puts the new jobs' tokens in the jobserver
and resizes the command's scope. A no (the slots went to someone else first) changes nothing,
and it asks again after the next renewal.

When the target is below the weight, the jobserver owes the tokens past the target's jobs
(`Jobserver.set`): it takes each one a job gives back, so no running job is stopped. A slot is
given back (`POST /v1/resize`, and the scope resized) only once the tokens of its jobs have
come back, so the jobs that can run never outnumber the lease's, however long they take. A job
killed while it holds a token never returns it, and the lease keeps that slot until the
command ends.

Only the jobserver's tokens and the scope follow the target: `LEASEHOLD_JOBS`, the job and
thread variables, `MAKEFLAGS`'s `-jN` and `LEASEHOLD_WEIGHT` keep the values the command
started with. Resizing the scope is best effort: a failure is reported, and the lease moves
anyway.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from leasehold import contain, jobs
from leasehold.client import ApiError, Client, Unavailable
from leasehold.units import memory_to_bytes

#: How long, in seconds, a follower whose lease is not elastic yet looks often for a job taking
#: a token; after that it looks at each renewal.
WATCH_S = 60.0

#: How long `step` waits for the free tokens owed to be taken out of the fifo.
_SETTLE_S = 0.05


class Follower:
    """One elastic lease's holder side. `granted` is the lease as granted (its `weight`,
    `jobs`, and from a daemon with elastic leases its `elastic`, `target` and `target_jobs`).
    `server` is the command's jobserver; `scope` its systemd scope, if it runs in one, sized
    with `memory_per_slot` (a size such as `2g`) for each slot when given, and `hard_cpu`.
    `client` should answer within a renewal interval, since `step` waits for it."""

    def __init__(
        self,
        client: Client,
        lease_id: str,
        granted: dict,
        *,
        server: jobs.Jobserver,
        scope: str | None = None,
        memory_per_slot: str | None = None,
        hard_cpu: bool = False,
        say: Callable[[str], None] = lambda _: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.lease_id = lease_id
        self.elastic = bool(granted.get("elastic"))
        self._take(granted)
        self.server = server
        self.scope = scope
        self.memory_per_slot = memory_per_slot
        self.hard_cpu = hard_cpu
        self._say = say
        self._clock = clock
        self._fixed = False  # the daemon cannot make it elastic
        self._giving = False
        self._since = clock()

    def _take(self, answer: dict) -> None:
        self.weight = int(answer["weight"])
        self.jobs = int(answer.get("jobs") or self.weight)
        self.target = int(answer.get("target") or self.weight)
        self.target_jobs = int(answer.get("target_jobs") or self.jobs)

    def heard(self, renewal: dict) -> None:
        """Take a renewal's answer: the lease's weight and target, and their job counts."""
        if renewal.get("status") != "ok" or renewal.get("weight") is None:
            return
        self._take(renewal)

    def busy(self) -> bool:
        """Whether it should be stepped often: for `WATCH_S` after it starts it waits for a job
        to take a token, and while asked to shrink it waits for jobs to give tokens back."""
        watching = not self.elastic and not self._fixed
        if watching and self._clock() - self._since < WATCH_S:
            return True
        return self.elastic and self.target < self.weight

    def step(self) -> None:
        """One move toward the target: become elastic, grow, put tokens in or owe them, give
        slots back."""
        if not self.elastic:
            self._become_elastic()
            if not self.elastic:
                return
        if self.target > self.weight:
            self._resize(self.target)
        self.server.set(self._wanted())
        if self.target >= self.weight:
            self._giving = False
            return
        owed = self.server.settle(_SETTLE_S)
        # Give back only the slots no job can still be running in: the command's own job and
        # every token in play, the owed ones included, keep theirs.
        keep = max(self.target, self._weight_for(self.server.tokens + owed + 1))
        if keep < self.weight:
            self._resize(keep)
        if owed and not self._giving:
            self._say(
                f"giving back {self.weight - self.target} of {self.weight} slots as jobs finish "
                "and return their tokens"
            )
        self._giving = bool(owed)

    def _become_elastic(self) -> None:
        if self._fixed or self.server.taken() == 0:
            return
        try:
            answer = self.client.make_elastic(self.lease_id)
        except Unavailable:
            return  # asked again at the next step
        except ApiError as exc:
            if exc.status < 500:
                self._fixed = True  # not a range, or a daemon from before elastic leases
            return
        self.elastic = True
        self._take(answer)

    def _weight_for(self, count: int) -> int:
        """The least weight whose job count is at least `count`, at this lease's jobs per slot."""
        return -(-count * self.weight // max(1, self.jobs))

    def _wanted(self) -> int:
        """The tokens the jobserver should have in play now."""
        count = self.target_jobs if self.target < self.weight else self.jobs
        return max(0, count - 1)

    def _resize(self, weight: int) -> None:
        try:
            answer = self.client.resize(self.lease_id, weight)
        except (Unavailable, ApiError):
            return  # not now
        before = self.weight
        self._take(answer)
        if self.weight != before:
            self._say(f"the lease is now {self.weight} slots (was {before})")
            self._resize_scope()

    def _resize_scope(self) -> None:
        if self.scope is None:
            return
        memory = None
        if self.memory_per_slot is not None:
            memory = f"{self.weight * memory_to_bytes(self.memory_per_slot)}b"
        why = contain.update_scope(
            self.scope, weight=self.weight, memory=memory, hard_cpu=self.hard_cpu
        )
        if why is not None:
            self._say(why)
