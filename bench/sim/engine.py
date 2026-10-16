"""One simulated run: agents take turns thinking and running jobs on one machine, and leasehold's
own admission decides when each job starts and with how many slots.

Admission is the library's: at every request, every lease's end and every tick, the run builds
the `State` a daemon's ledger would hold (the leases held, the line, whose turn was when) and
the policy `MachineConfig.policy` gives the daemon (`leasehold.agent.Agent.policy`: the memory
budget, and while the memory guard is on, what memory is free), and starts what
`leasehold.admission.grants` admits, at the size it grants. Nothing here decides who goes next.

What the run adds around it: how long a job takes (`bench.sim.model`), the time `hold` takes to
start a job and to end it, and the memory each job really uses, which the guard reads as the
machine's available memory goes down. Memory pressure (PSI) is not modelled, so the guard acts on
available memory alone.

Elastic leases (a lease that grows and shrinks while it runs) are applied when the library has
`leasehold.admission.targets`; a condition that asks for them without it is refused with
`Unsupported`.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable
from dataclasses import dataclass, field

import simpy

from bench.sim.model import Job, Kind, Machine
from leasehold import admission
from leasehold.admission import AdmissionPolicy, Held, State, Waiting, grants
from leasehold.guard import Guard, Reading
from leasehold.machine import MachineConfig
from leasehold.model import MACHINE

#: The pool every simulated job asks in (the user config's `work`).
POOL = "work"
#: The most events one run may process: a run that passes it has a bug, not a long plan.
MAX_EVENTS = 2_000_000


class Unsupported(Exception):
    """A condition this library cannot simulate (elastic leases without `targets`)."""


@dataclass(frozen=True)
class Overheads:
    """Seconds `hold` adds: from the agent's call to its request, and from the job's end to
    the lease's (and the call's) end. Without leasehold, from the call to the job's start and
    from its end to the call's."""

    start: float = 0.37
    end: float = 0.11
    start_unmanaged: float = 0.04
    end_unmanaged: float = 0.03


@dataclass(frozen=True)
class Setup:
    """How a condition starts jobs."""

    name: str
    #: The daemon's config; None: no leasehold, every job starts at once.
    config: MachineConfig | None = None
    #: Jobs use every CPU, whatever their lease.
    greedy: bool = False
    #: Each lease's job is held to its weight in CPUs (`[contain] hard_cpu`).
    hard_cpu: bool = False
    #: Without leasehold, each job runs `weight` workers instead of every CPU.
    static: bool = False
    #: Leases grow and shrink to `admission.targets` after `settle_s`.
    elastic: bool = False
    settle_s: float = 5.0
    #: Whether the memory guard runs (as the config says, when it does).
    guard: bool = True


@dataclass
class _Lease:
    job: Job
    weight: int
    granted_at: float


@dataclass
class _Request:
    job: Job
    key: tuple[str, str]
    order: tuple[float, int]
    event: simpy.Event


@dataclass
class Sample:
    """The machine at one moment, from then until the next sample."""

    t: float
    held: int
    waiting: int
    busy: float
    counted: int
    actual: int
    free: int


@dataclass
class Result:
    """What one run produced."""

    jobs: list[Job]
    samples: list[Sample]
    slots: int
    cpus: int
    budget: int | None
    #: The memory the leases counted, at its peak, and what the jobs really used.
    counted_peak: int = 0
    actual_peak: int = 0
    guard_on_s: float = 0.0
    notes: list[str] = field(default_factory=list)


class _Cpu:
    """The jobs running on the machine, each at the rate the machine gives it; fires each one's
    `finished` event when its work is done."""

    def __init__(self, env: simpy.Environment, machine: Machine, kinds: dict[str, Kind]):
        self.env = env
        self.machine = machine
        self.kinds = kinds
        #: Job -> [work left, workers, CPU cap or None, finished event].
        self.running: dict[int, list] = {}
        self.jobs: dict[int, Job] = {}
        self.last = 0.0
        self.changed = env.event()
        env.process(self._loop())

    def demand(self, job: Job, workers: int, cap: float | None) -> float:
        busy = self.kinds[job.kind].speedup(workers)
        return busy if cap is None else min(busy, cap)

    def busy(self) -> float:
        return sum(self.demand(self.jobs[i], r[1], r[2]) for i, r in self.running.items())

    def start(self, job: Job, workers: int, cap: float | None) -> simpy.Event:
        self._advance()
        done = self.env.event()
        self.running[id(job)] = [job.work, workers, cap, done]
        self.jobs[id(job)] = job
        self._poke()
        return done

    def resize(self, job: Job, workers: int, cap: float | None) -> None:
        self._advance()
        entry = self.running.get(id(job))
        if entry is not None:
            entry[1], entry[2] = workers, cap
            self._poke()

    def _rates(self) -> dict[int, float]:
        ids = list(self.running)
        demands = [self.demand(self.jobs[i], self.running[i][1], self.running[i][2]) for i in ids]
        return dict(zip(ids, self.machine.rates(demands), strict=True))

    def _advance(self) -> None:
        dt = self.env.now - self.last
        if dt > 0:
            for i, rate in self._rates().items():
                self.running[i][0] -= rate * dt
        self.last = self.env.now

    def _poke(self) -> None:
        if not self.changed.triggered:
            self.changed.succeed()

    def _loop(self):
        while True:
            rates = self._rates()
            ahead = [self.running[i][0] / r for i, r in rates.items() if r > 0]
            wake = self.env.timeout(max(0.0, min(ahead))) if ahead else None
            yield (wake | self.changed) if wake is not None else self.changed
            if self.changed.triggered:
                self.changed = self.env.event()
            self._advance()
            for i in [i for i, r in self.running.items() if r[0] <= 1e-9]:
                entry = self.running.pop(i)
                self.jobs.pop(i)
                entry[3].succeed()


class Simulation:
    """One run of `steps` (each agent's `[think_s, Job]` list) under `setup`."""

    def __init__(
        self,
        setup: Setup,
        machine: Machine,
        kinds: dict[str, Kind],
        steps: list[list[tuple[float, Job]]],
        *,
        overheads: Overheads | None = None,
        other_memory: int = 0,
        seed: int = 0,
        noise_cv: float = 0.0,
    ) -> None:
        self.env = simpy.Environment()
        self.setup = setup
        self.machine = machine
        self.kinds = kinds
        self.steps = steps
        self.overheads = overheads or Overheads()
        self.other_memory = other_memory
        self.cpu = _Cpu(self.env, machine, kinds)
        self.rng = random.Random(f"noise:{seed}")
        self.noise_cv = noise_cv
        self.config = setup.config
        self.leases: dict[tuple[str, str], _Lease] = {}
        self.line: dict[tuple[str, str], _Request] = {}
        self.served: dict = {}
        self.session_served: dict = {}
        self.turn = 0
        self.seq = 0
        self.jobs: list[Job] = []
        self.samples: list[Sample] = []
        self.actual = 0
        self.counted_peak = 0
        self.actual_peak = 0
        self.guard_on_s = 0.0
        self.notes: list[str] = []
        self.queued_at: dict[tuple[str, str], float] = {}
        self.targets: Callable | None = None
        if self.config is not None:
            self.limits = self.config.limits(cpus=machine.cpus, memory_bytes=machine.memory_bytes)
            self.budget = self.config.memory_budget(machine.memory_bytes)
            self.guard = Guard(
                self.config.guard, min_available=self.config.guard.floor(machine.memory_bytes)
            )
            self.guarding = setup.guard and self.config.guarded
            self.slots = self.config.ceiling.slots
        else:
            self.budget = None
            self.guarding = False
            self.slots = machine.cpus
        if setup.elastic:
            self.targets = elastic_targets()

    # --- the policy, as the daemon builds it ----------------------------------------------------

    def policy(self) -> AdmissionPolicy:
        assert self.config is not None
        on = self.guarding and self.guard.on
        counting = self.budget is not None
        free = self.memory_free() if on and counting else None
        return self.config.policy(
            capped=on and not counting,
            memory_bytes=self.machine.memory_bytes,
            memory_free=free,
            memory_short="memory guard on" if free is not None else None,
            memory_pressing=on and counting and self.guard.pressing,
            memory_overdue=self._overdue() if free is not None else frozenset(),
            lease_memory=self.limits.memory_bytes,
        )

    def available(self) -> int:
        return self.machine.memory_bytes - self.other_memory - self.actual

    def memory_free(self) -> int:
        """`Agent.memory_free`: what is available above the guard's floor, less what leases
        granted within `settle_s` count."""
        assert self.config is not None and self.budget is not None
        if self.guard.pressing:
            return 0
        since = self.env.now - self.config.guard.settle_s
        recent = sum(
            self._counted(lease.job, lease.weight)
            for lease in self.leases.values()
            if lease.granted_at >= since
        )
        return max(0, self.available() - self.guard.min_available - recent)

    def _overdue(self) -> frozenset[tuple[str, str]]:
        assert self.config is not None
        since = self.env.now - self.config.guard.settle_s
        return frozenset(k for k, at in self.queued_at.items() if at < since)

    def _counted(self, job: Job, weight: int) -> int:
        if self.budget is None:
            return 0
        if job.memory_per_slot is not None:
            return weight * job.memory_per_slot
        if job.memory is not None:
            return job.memory
        return weight * self.budget.assumed_per_slot

    def state(self) -> State:
        held = []
        keyed = "key" in {f.name for f in dataclasses.fields(Held)}
        for key, lease in self.leases.items():
            job = lease.job
            extra = {"key": key} if keyed else {}
            held.append(
                Held(
                    pool=POOL,
                    host=MACHINE,
                    user=job.user,
                    weight=lease.weight,
                    session=job.session,
                    memory=job.memory
                    if job.memory_per_slot is None
                    else lease.weight * job.memory_per_slot,
                    memory_per_slot=job.memory_per_slot,
                    **extra,
                )
            )
        waiting = tuple(
            Waiting(
                key=r.key,
                pool=POOL,
                user=r.job.user,
                weight=r.job.weight,
                order=r.order,
                session=r.job.session,
                memory=r.job.memory,
                max_weight=r.job.max_weight,
                memory_per_slot=r.job.memory_per_slot,
            )
            for r in self.line.values()
        )
        return State(
            held=tuple(held),
            waiting=waiting,
            served=dict(self.served),
            session_served=dict(self.session_served),
        )

    # --- admission ------------------------------------------------------------------------------

    def admit(self) -> None:
        if self.config is None:
            return
        if self.line:
            policy = self.policy()
            for grant in grants(self.state(), policy):
                request = self.line.pop(grant.key)
                self.queued_at.pop(grant.key, None)
                job = request.job
                self.turn += 1
                self.served[(POOL, job.user)] = self.turn
                self.session_served[(POOL, job.user, job.session)] = self.turn
                self.leases[grant.key] = _Lease(job, grant.weight, self.env.now)
                request.event.succeed(grant.weight)
        if self.targets is not None and self.leases:
            self._resize()
        self._sample()

    def _resize(self) -> None:
        """Apply `admission.targets`: each lease whose target differs from its weight is moved
        to it after `settle_s` (a job takes that long to notice and change its workers)."""
        assert self.targets is not None
        try:
            wanted = self.targets(self.state(), self.policy())
        except TypeError as exc:  # its signature is not the one this hook was written for
            raise Unsupported(f"admission.targets has an unexpected signature: {exc}") from exc
        items = wanted.items() if hasattr(wanted, "items") else ((t.key, t.weight) for t in wanted)
        for key, weight in items:
            lease = self.leases.get(key)
            if lease is not None and weight != lease.weight:
                self.env.process(self._settle(key, int(weight)))

    def _settle(self, key, weight: int):
        yield self.env.timeout(self.setup.settle_s)
        lease = self.leases.get(key)
        if lease is None:
            return
        lease.weight = weight
        workers = self._workers(lease.job, weight)
        self.cpu.resize(lease.job, workers, self._cap(weight))
        lease.job.workers = workers
        self.admit()

    def _workers(self, job: Job, weight: int) -> int:
        if self.setup.greedy or (self.config is None and not self.setup.static):
            return self.machine.cpus
        per_slot = self.config.jobs.per_slot if self.config is not None else 1.0
        return max(1, int(weight * per_slot))

    def _cap(self, weight: int) -> float | None:
        return float(weight) if self.setup.hard_cpu else None

    # --- the agents and their jobs ---------------------------------------------------------------

    def _agent(self, steps: list[tuple[float, Job]]):
        for think, job in steps:
            yield self.env.timeout(think)
            yield self.env.process(self._job(job))

    def _job(self, job: Job):
        o = self.overheads
        job.issued = self.env.now
        self.jobs.append(job)
        if self.noise_cv > 0:
            job.work *= self.rng.lognormvariate(-(self.noise_cv**2) / 2, self.noise_cv)
        managed = self.config is not None
        yield self.env.timeout(o.start if managed else o.start_unmanaged)
        weight = job.weight
        key = ("sim", f"{job.agent}.{job.seq}")
        if managed:
            self.seq += 1
            granted = self.env.event()
            self.line[key] = _Request(job, key, (self.env.now, self.seq), granted)
            self.queued_at[key] = self.env.now
            self.admit()
            weight = yield granted
        job.granted = weight
        job.workers = self._workers(job, weight)
        job.started = self.env.now
        kind = self.kinds[job.kind]
        used = kind.memory + kind.memory_per_worker * job.workers
        self.actual += used
        self.actual_peak = max(self.actual_peak, self.actual)
        finished = self.cpu.start(job, job.workers, self._cap(weight) if managed else None)
        self._sample()
        yield finished
        job.ended = self.env.now
        self.actual -= used
        self._sample()
        yield self.env.timeout(o.end if managed else o.end_unmanaged)
        job.done = self.env.now
        if managed:
            self.leases.pop(key, None)
            self.admit()

    def _ticks(self):
        assert self.config is not None
        tick = self.config.tick_s
        while True:
            yield self.env.timeout(tick)
            if self.guarding:
                free = self.available()
                reading = Reading(now=self.env.now, memory_pressure=None, available=free)
                if self.guard.observe(reading):
                    self.guard_on_s += tick
            self.admit()

    def _sample(self) -> None:
        held = sum(lease.weight for lease in self.leases.values())
        counted = sum(self._counted(lease.job, lease.weight) for lease in self.leases.values())
        self.counted_peak = max(self.counted_peak, counted)
        self.samples.append(
            Sample(
                t=self.env.now,
                held=held,
                waiting=len(self.line),
                busy=self.cpu.busy(),
                counted=counted,
                actual=self.actual,
                free=max(0, self.slots - held),
            )
        )

    def run(self, horizon: float = 1e6) -> Result:
        agents = [self.env.process(self._agent(s)) for s in self.steps]
        if self.config is not None:
            self.env.process(self._ticks())
        everyone = self.env.all_of(agents)
        self._sample()
        events = 0
        while not everyone.processed and self.env.peek() < horizon:
            self.env.step()
            events += 1
            if events > MAX_EVENTS:
                raise RuntimeError("the run passed its event limit")
        unfinished = sum(job.done is None for job in self.jobs)
        if unfinished:
            self.notes.append(f"{unfinished} jobs unfinished at the horizon")
        return Result(
            jobs=self.jobs,
            samples=self.samples,
            slots=self.slots,
            cpus=self.machine.cpus,
            budget=None if self.budget is None else self.budget.budget,
            counted_peak=self.counted_peak,
            actual_peak=self.actual_peak,
            guard_on_s=self.guard_on_s,
            notes=self.notes,
        )


def elastic_targets() -> Callable:
    """`leasehold.admission.targets`, or `Unsupported` when this library has none."""
    found = getattr(admission, "targets", None)
    if found is None:
        raise Unsupported(
            "this leasehold has no admission.targets (elastic leases), so elastic "
            "conditions are skipped"
        )
    return found
