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
(`Jobserver.set`): it takes each one a job gives back, so no running job is stopped. Once none
is owed, the follower gives the slots back (`POST /v1/resize`) and resizes the scope. A job
killed while it held a token never gives it back, so after `give_back_s` the tokens still owed
are taken as lost and the slots are given back all the same.

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

#: How long, in seconds, a lease asked to give slots back waits for its jobs' tokens before it
#: takes the ones still out as lost.
GIVE_BACK_S = 120.0

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
        give_back_s: float = GIVE_BACK_S,
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
        self.give_back_s = give_back_s
        self._say = say
        self._clock = clock
        self._fixed = False  # the daemon cannot make it elastic
        self._giving_since: float | None = None

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
        """Whether it should be stepped often: it waits for a job to take a token, or for jobs
        to give tokens back."""
        return (not self.elastic and not self._fixed) or self.target < self.weight

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
            self._giving_since = None
            return
        if self.server.settle(_SETTLE_S):
            now = self._clock()
            if self._giving_since is None:
                self._giving_since = now
                self._say(
                    f"giving back {self.weight - self.target} of {self.weight} slots as jobs "
                    "finish and return their tokens"
                )
                return
            if now - self._giving_since < self.give_back_s:
                return
            lost = self.server.forget()
            self._say(
                f"{lost} jobserver token(s) did not come back in {self.give_back_s:g} s and "
                "are taken as lost (a job killed while it held one never returns it)"
            )
        self._giving_since = None
        self._resize(self.target)

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
