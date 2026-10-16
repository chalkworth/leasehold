"""How `hold` follows an elastic lease's target (`leasehold.elastic`): the holder's side of
the promise to take slots when they are offered and give them back when asked.

A `Follower` is told each renewal's answer (`heard`) and works toward the target (`step`).
When the target is above the lease's weight, it asks the daemon for the target
(`POST /v1/resize`). Once the daemon says yes, it puts the new jobs' tokens in the jobserver
and resizes the command's scope. A no (the slots went to someone else first, or the daemon is
from before elastic leases) changes nothing, and it asks again after the next renewal.

When the target is below the weight, it takes free tokens out of the jobserver as jobs give
them back, so no running job is stopped. Once no more tokens are in play than the target's jobs
need, it gives the slots back (`POST /v1/resize`) and resizes the scope. Without a jobserver it
gives them back at once.

Only the jobserver's tokens and the scope follow the target: `LEASEHOLD_JOBS`, the job and
thread variables, `MAKEFLAGS`'s `-jN` and `LEASEHOLD_WEIGHT` keep the values the command
started with. Resizing the scope is best effort: a failure is reported, and the lease moves
anyway.
"""

from __future__ import annotations

from collections.abc import Callable

from leasehold import contain, jobs
from leasehold.client import ApiError, Client, Unavailable
from leasehold.units import memory_to_bytes


class Follower:
    """One elastic lease's holder side. `granted` is the lease as granted (its `weight`,
    `jobs`, and from a daemon with elastic leases its `target` and `target_jobs`). `server` is
    the command's jobserver, if it has one; `scope` its systemd scope, if it runs in one, sized
    with `memory_per_slot` (a size such as `2g`) for each slot when given, and `hard_cpu`."""

    def __init__(
        self,
        client: Client,
        lease_id: str,
        granted: dict,
        *,
        server: jobs.Jobserver | None = None,
        scope: str | None = None,
        memory_per_slot: str | None = None,
        hard_cpu: bool = False,
        say: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.client = client
        self.lease_id = lease_id
        self.weight = int(granted["weight"])
        self.jobs = int(granted.get("jobs") or self.weight)
        self.target = int(granted.get("target") or self.weight)
        self.target_jobs = int(granted.get("target_jobs") or self.jobs)
        self.server = server
        self.scope = scope
        self.memory_per_slot = memory_per_slot
        self.hard_cpu = hard_cpu
        self._say = say

    def heard(self, renewal: dict) -> None:
        """Take a renewal's answer: the lease's weight and target, and their job counts."""
        if renewal.get("status") != "ok" or renewal.get("weight") is None:
            return
        self.weight = int(renewal["weight"])
        self.jobs = int(renewal.get("jobs") or self.weight)
        self.target = int(renewal.get("target") or self.weight)
        self.target_jobs = int(renewal.get("target_jobs") or self.jobs)

    def busy(self) -> bool:
        """Whether it waits on jobs to give tokens back, and so should be stepped often."""
        return self.target < self.weight or self._tokens() != self._wanted()

    def step(self) -> None:
        """One move toward the target: grow, put tokens in or take them out, give slots back."""
        if self.target > self.weight:
            self._resize(self.target)
        wanted = self._wanted()
        if self.server is not None:
            have = self.server.tokens
            if have < wanted:
                self.server.add(wanted - have)
            elif have > wanted:
                self.server.withhold(have - wanted)
        if self.target < self.weight and self._tokens() <= wanted:
            self._resize(self.target)

    def _wanted(self) -> int:
        """The tokens the jobserver should have in play now."""
        count = self.target_jobs if self.target < self.weight else self.jobs
        return max(0, count - 1)

    def _tokens(self) -> int:
        return self._wanted() if self.server is None else self.server.tokens

    def _resize(self, weight: int) -> None:
        try:
            answer = self.client.resize(self.lease_id, weight)
        except (Unavailable, ApiError):
            return  # not now
        before = self.weight
        self.weight = int(answer["weight"])
        self.jobs = int(answer.get("jobs") or self.weight)
        self.target = int(answer.get("target") or self.weight)
        self.target_jobs = int(answer.get("target_jobs") or self.jobs)
        if self.server is not None and self.server.tokens < self._wanted():
            self.server.add(self._wanted() - self.server.tokens)
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
