"""Memory under a cgroup limit: agents' jobs share one machine's slots and one memory cap, and
a memory policy decides what each job counts at admission and what happens when the cap
overflows. `bench.sim.memstudy` calibrates it against the bench's memory runs and compares
policies with it.

Admission is the library's: at every request, every end and every tick, the run builds the
`State` and `AdmissionPolicy` a daemon would (one `work` pool on 6 slots, a memory budget, and
what each lease counts) and starts what `leasehold.admission.grants` admits. The policy
(`Policy`) only chooses what each item counts: its weight times the assumed memory per slot,
what it declares, or a learned estimate. A rule that makes heavy work wait (plateau, parked
memory) makes a heavy waiting item count more than the budget, so it blocks its pool as an
item whose turn it is does, and nothing behind it starts.

The machine (`Physics`), stepped every `dt_s` seconds:

* CPU. Each running job keeps `weight` threads busy (the bench's jobs fork one worker per
  slot). With `k` threads busy the machine runs `min(k, cores) + smt_yield * (k - cores)`
  cores' worth, shared evenly.
* Memory. A job's footprint grows linearly with its progress over the first `ramp` of its work,
  to its size. The jobs, `base_gib` and each cpu job's `cpu_job_gib` share the cap. What does
  not fit (the overflow `X`) is in swap: once the cap is full, footprints grow no faster than
  `swap_out_gib_s` in all, every `mem` job runs `1 + thrash_per_gib * X` times slower, and
  `churn_gib_s` of swap is written each second besides the overflow itself.
* Swap slots. The slots the jobs hold grow at `slot_growth_gib_s` while the cap overflows, from
  `X` up to `X + slot_dup_gib` (pages read back from swap keep their slot), and are freed only
  when a job ends. When the slots reach the cap's swap limit, or a job's own swap limit under
  parking, the kernel kills the largest job (the youngest of equals; a protected job last).
* Parking (`Policy.park`, after `bench/park.py`). Each running `mem` job may hold
  `overflow_gib` of swap. When the cap overflows with two or more `mem` jobs running, the
  youngest is frozen after `detect_s` and its memory pushed out at `reclaim_gib_s`. No park
  follows within `thaw_gap_s` of a thaw. The oldest parked job is thawed once the others and
  it fit under `thaw_at` of the cap, when no other `mem` job runs, or after `max_park_s`; it
  then pages back in at `page_in_gib_s`, running only as fast as its memory is back, and its
  swap counts against its own limit until it is read in.
* A CPU brake (`Policy.brake`) slows the youngest growing `mem` job to `brake_share` of its
  speed once the cap is `brake_at` full, so it stops growing before the cap overflows.

Not modelled: the page cache, memory bandwidth, the guard's PSI readings, swap device
contention with other I/O, and which pages the kernel picks; the swap terms are fitted to one
kind of job (one that sweeps all of its memory again and again).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, replace

import simpy

from bench.run import Preset, make_plan
from leasehold.admission import AdmissionPolicy, Held, PoolPolicy, State, Waiting, grants
from leasehold.model import MACHINE

GIB = 1 << 30
POOL = "work"
#: What a waiting item counts, in GiB, while a rule makes heavy work wait: more than any budget.
BLOCKED_GIB = 1 << 20
#: A `mem` job is at most the cap less the base and this much (`Simulation._draw_size`).
FIT_SPARE_GIB = 0.25
#: Where in its work a job's late growth (`Workload.late_share`) starts.
LATE_AT = 0.6
#: Plans are drawn for this nominal length of each job alone on the machine (the bench's).
NOMINAL = {"cpu": 10.0, "mem": 6.42}


@dataclass(frozen=True)
class Physics:
    """The machine and the kernel, as fitted to the 8 CPU / 16 GiB VM's memory runs."""

    cpus: int = 8
    cores: float = 4.0
    smt_yield: float = 0.44
    slots: int = 6
    #: Core-seconds of work in one job of each kind.
    work_cpu: float = 60.0
    work_mem: float = 36.4
    base_gib: float = 0.25
    cpu_job_gib: float = 0.03
    thrash_per_gib: float = 0.9
    swap_out_gib_s: float = 0.5
    churn_gib_s: float = 0.1
    slot_growth_gib_s: float = 0.3
    slot_dup_gib: float = 2.3
    page_in_gib_s: float = 0.3
    reclaim_gib_s: float = 1.4
    detect_s: float = 0.3
    thaw_gap_s: float = 2.0
    thaw_at: float = 0.8
    max_park_s: float = 120.0
    overflow_gib: float = 1.0
    #: Pages read back from swap keep their slot until the job ends (a job whose forked workers
    #: share its memory, as the bench's do). False frees a slot as its page is read in.
    sticky_slots: bool = False
    #: The share of a parked job's memory that reclaim can push out (park effectiveness).
    reclaim_share: float = 1.0
    #: zswap's pool (max_pool_percent of RAM: 20% of 16 GiB), and how fast it pages back in.
    zswap_pool_gib: float = 3.2
    zswap_page_in_gib_s: float = 2.0
    #: A job has settled once its growth, averaged over `settle_tau_s`, stays under the larger
    #: of `settle_floor_gib_s` and `settle_share` of its footprint a second for the cooldown.
    settle_floor_gib_s: float = 16 / 1024
    settle_share: float = 0.01
    settle_tau_s: float = 1.0
    settle_max_s: float = 60.0
    settle_cooldown_s: float = 5.0
    #: Seconds `hold` adds before a request and after a job.
    start_s: float = 0.37
    end_s: float = 0.11
    tick_s: float = 1.0
    dt_s: float = 0.1

    def capacity(self, threads: float) -> float:
        k = min(max(0.0, threads), float(self.cpus))
        return min(k, self.cores) + self.smt_yield * max(0.0, k - self.cores)


@dataclass(frozen=True)
class Workload:
    """What the agents run. `mem_share` is a `mem` job's mean size as a share of the cap."""

    agents: int = 8
    duration_s: float = 240.0
    think_s: float = 30.0
    p_mem: float = 0.35
    cap_gib: float = 7.0
    swap_gib: float = 5.0
    mem_share: float = 3.75 / 7.0
    #: The spread of `mem` job sizes (coefficient of variation of a lognormal).
    size_cv: float = 0.0
    #: The share of a job's work over which its memory grows.
    ramp: float = 0.03
    #: The share of its size a `mem` job adds late: it grows to the rest, holds there, and from
    #: `LATE_AT` of its work grows again by this much over another `ramp` (a link step).
    late_share: float = 0.0
    #: The share of `mem` jobs that are surprises: `outlier_scale` times their drawn size, as a
    #: job whose input grew since the estimate learned it.
    outlier_share: float = 0.0
    outlier_scale: float = 1.5
    #: The share of jobs that are important (killed and parked last when `Policy.protect`).
    p_important: float = 0.2
    #: Whether an agent runs a killed job again (once more at most twice).
    retry: bool = False
    #: Drift: from `drift_at` of each agent's plan on, `mem` jobs are `1 + drift` times larger
    #: (a dependency upgrade the estimate has not seen).
    drift: float = 0.0
    drift_at: float = 0.4
    #: A heavier tail: `tail_share` of `mem` jobs are scaled by a Pareto factor with shape
    #: `tail_alpha` (at least 1), on top of `outlier_share`.
    tail_share: float = 0.0
    tail_alpha: float = 2.5

    @property
    def mem_gib(self) -> float:
        return self.mem_share * self.cap_gib


@dataclass(frozen=True)
class Policy:
    """A memory policy. `estimator`: `assumed` (weight times budget per slot), `declared` (mem
    jobs declare their typical size plus `declared_pad_gib`; others assumed), `learned` (the
    0.5 plan's estimate, below), or `oracle` (each job's true size)."""

    name: str = "w3"
    weight: int = 3
    estimator: str = "assumed"
    budget_share: float = 0.75
    slack_gib: float = 0.0
    declared_pad_gib: float = 0.5
    #: Learned: `max(weighted p95, max of the last 2) * margin`, floored at `floor_gib`, once a
    #: kind has `trust_after` clean runs. Before that, `cold`: `assumed` (the assumed figure,
    #: raised by anything seen) or `alone` (the whole budget).
    margin: float = 1.15
    #: The weighted quantile of past peaks the estimate starts from (the plan's p95). Below
    #: 0.9 the last two runs' maximum is left out.
    quantile: float = 0.95
    floor_gib: float = 0.25
    trust_after: int = 3
    cold: str = "assumed"
    #: Runs already in the ledger, drawn from the workload's own sizes, when the run starts.
    prior_runs: int = 0
    #: A running job counts the larger of its estimate and what it uses now.
    observe: bool = False
    #: Heavy work (counting `heavy_share` of the cap or more) waits while a running job grows.
    plateau: bool = False
    #: With `plateau`, a job whose memory has settled counts what it uses times
    #: `settled_margin` in place of its estimate, until it grows again.
    count_settled: bool = False
    settled_margin: float = 1.1
    plateau_cooldown_s: float = 5.0
    plateau_floor_gib_s: float = 16 / 1024
    heavy_share: float = 0.25
    park: bool = False
    #: Each running `mem` job may hold only `Physics.overflow_gib` of swap (always so when
    #: parking).
    swap_limit: bool = False
    #: Heavy work waits while a job is parked or settling after a thaw.
    park_hold: bool = False
    #: How heavy work waits: in line, holding up the items behind it as a daemon's admission
    #: does (False); or, as the bench harness's `hold=parked` did, `mem` jobs only, before they
    #: join the line, so other work goes ahead of them (True).
    hold_outside: bool = False
    brake: bool = False
    brake_at: float = 0.95
    brake_share: float = 0.05
    #: Kill and park unimportant jobs first.
    protect: bool = False
    #: Estimators beyond the plan's: `mean` (weighted mean of past peaks times `margin`) and
    #: `gauss` (weighted mean plus `k_sd` weighted standard deviations, times `margin`).
    k_sd: float = 2.0
    #: A park or a requeue pins the kind's estimate at 1.2 times what it counted, as a kill
    #: pins it at its peak, so the next estimate rises at once.
    bump_on_net: bool = False
    #: Chance-constrained packing on top of the item counts: a `mem` grant is held back unless
    #: the `mem` jobs then running pass `chance` ("gauss": sum of means plus `chance_z` times the
    #: root of the summed variances fits the budget; "mc": at most `chance_eps` of `mc_draws`
    #: sums of past peaks drawn for each job pass it). Cold kinds count their item figure.
    chance: str = ""
    chance_z: float = 3.5
    chance_eps: float = 2e-4
    mc_draws: int = 4000
    #: Heavy work waits while a running `mem` job is younger than `peak_wait` times the learned
    #: p95 time to its peak (wall seconds from its start).
    peak_wait: float = 0.0
    #: Heavy work does not start within `stagger_s` of the last heavy start.
    stagger_s: float = 0.0
    #: Heavy work waits while the cap's free swap is under this (GiB).
    hold_swap_gib: float = 0.0
    #: Heavy work waits while a job that was parked still runs (not only while it settles).
    hold_resumed: bool = False
    #: Park only when the cap's free swap holds the victim's memory with `park_room_gib` to spare.
    park_room: bool = False
    park_room_gib: float = 0.25
    #: Kill the victim and run it again (memory.oom.group and an automatic retry) instead of, or
    #: as well as, parking it: "always" (at `requeue_at` of the cap, before any park), "noroom"
    #: (when a park is due but swap cannot take the victim), "young" (when a park is due and the
    #: victim has run under `requeue_young_s`, or swap cannot take it).
    requeue: str = ""
    requeue_at: float = 1.0
    requeue_young_s: float = 20.0
    #: What a requeued job counts when it runs again: "alone" (the budget) or "estimate".
    requeue_count: str = "alone"
    #: A memory.high throttle on the youngest growing `mem` job once the cap is `throttle_at`
    #: full: it stays at what it uses, what more it touches goes to its own swap, and it runs at
    #: `throttle_share` of its speed while over its high.
    throttle: bool = False
    throttle_at: float = 0.95
    throttle_share: float = 0.3
    #: Park into zswap with writeback off: the compression ratio (0 parks to the swap device).
    #: A park then writes nothing to disk, but its pages still hold swap slots and take
    #: 1 / ratio of their size in the cap, and they stay out only while the pool has room.
    park_zswap: float = 0.0


@dataclass
class _Job:
    agent: int
    seq: int
    kind: str
    size: float
    important: bool
    attempt: int = 0
    issued: float = 0.0
    started: float | None = None
    done: float | None = None
    killed: bool = False
    weight: int = 0
    counted: float = 0.0
    progress: float = 0.0
    footprint: float = 0.0
    peak: float = 0.0
    #: Swap slots held: stale ones included.
    slots: float = 0.0
    parked_at: float | None = None
    #: After a thaw: how much of the footprint is still in swap, read back at the page-in rate.
    swapped: float = 0.0
    thawed_at: float | None = None
    braked: bool = False
    growth_ema: float = 0.0
    #: Requeued by the net (not an OOM kill), and whether this attempt runs counted alone.
    requeued: bool = False
    alone: bool = False
    #: When the footprint last grew by 1% of the size, and that mark.
    peak_at: float = 0.0
    peak_mark: float = 0.0
    #: memory.high under `Policy.throttle`, and the touched memory over it (in its own swap).
    high: float | None = None
    excess: float = 0.0
    #: Of what it holds in swap, how much a park put in zswap (uncompressed GiB).
    zpool: float = 0.0
    was_parked: bool = False
    #: Stale slots it held when it last finished reading its pages back (sticky slots).
    base_slots: float = 0.0
    quiet_since: float | None = None
    finished: simpy.Event | None = None

    @property
    def key(self) -> tuple[str, str]:
        return ("sim", f"{self.agent}.{self.seq}.{self.attempt}")

    @property
    def session(self) -> str:
        return f"agent{self.agent}"


@dataclass
class Result:
    makespan: float
    jobs: int
    #: `mem` jobs started or killed, retries included: the unit of the kill rate.
    mem_jobs: int
    kills: int
    important_kills: int
    swap_written_gib: float
    thrash_s: float
    parked_s: float
    parks: int
    #: Times the CPU brake engaged.
    brakes: int
    turnaround_mean: float
    wait_mean: float
    failed: int
    requeues: int = 0
    requeue_lost_s: float = 0.0
    throttles: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def any_oom(self) -> bool:
        return self.kills > 0


class _Estimator:
    """What each kind of job counts under a learned policy, from the runs it has seen."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.runs: dict[str, list[tuple[float, bool]]] = {}
        self.ttp: dict[str, list[float]] = {}

    def record(self, kind: str, peak: float, killed: bool) -> None:
        if killed:
            peak = max(peak * 1.2, peak + 0.25)
        self.runs.setdefault(kind, []).append((peak, killed))

    def record_ttp(self, kind: str, seconds: float) -> None:
        self.ttp.setdefault(kind, []).append(seconds)

    def time_to_peak(self, kind: str) -> float | None:
        """The p95 of the last 40 clean runs' wall seconds to their peak, from 3 runs."""
        ts = sorted(self.ttp.get(kind, [])[-40:])
        if len(ts) < self.policy.trust_after:
            return None
        return ts[min(len(ts) - 1, int(0.95 * len(ts)))]

    def _weighted(self, kind: str) -> tuple[list[float], list[float]]:
        recent = self.runs.get(kind, [])[-40:]
        weights = [0.5 ** ((len(recent) - 1 - i) / 10) for i in range(len(recent))]
        return [p for p, _ in recent], weights

    def stats(self, kind: str) -> tuple[float, float] | None:
        """(weighted mean, weighted sd) of past peaks, once trusted."""
        runs = self.runs.get(kind, [])
        if sum(1 for _, k in runs if not k) < self.policy.trust_after:
            return None
        xs, ws = self._weighted(kind)
        total = sum(ws)
        mean = sum(x * w for x, w in zip(xs, ws, strict=True)) / total
        var = sum(w * (x - mean) ** 2 for x, w in zip(xs, ws, strict=True)) / total
        return mean, math.sqrt(var)

    def peaks(self, kind: str) -> list[float]:
        return [p for p, _ in self.runs.get(kind, [])[-40:]]

    def learned(self, kind: str) -> tuple[float | None, bool]:
        """(estimate, trusted): None when nothing has been seen."""
        runs = self.runs.get(kind, [])
        if not runs:
            return None, False
        clean = sum(1 for _, k in runs if not k)
        pinned = max((p for p, k in runs if k), default=0.0)
        if self.policy.estimator in ("mean", "gauss"):
            xs, ws = self._weighted(kind)
            total = sum(ws)
            mean = sum(x * w for x, w in zip(xs, ws, strict=True)) / total
            var = sum(w * (x - mean) ** 2 for x, w in zip(xs, ws, strict=True)) / total
            k = self.policy.k_sd if self.policy.estimator == "gauss" else 0.0
            # A pin (a kill, or a net event under `bump_on_net`) lifts the figure as it does the
            # plan's estimate, decaying out of the last 40 runs.
            estimate = max(mean + k * math.sqrt(var), pinned)
            estimate *= self.policy.margin
            return max(self.policy.floor_gib, estimate), clean >= self.policy.trust_after
        recent = runs[-40:]
        weights = [0.5 ** ((len(recent) - 1 - i) / 10) for i in range(len(recent))]
        order = sorted(zip((p for p, _ in recent), weights, strict=True))
        total, acc, p95 = sum(weights), 0.0, order[-1][0]
        for value, w in order:
            acc += w
            if acc >= self.policy.quantile * total:
                p95 = value
                break
        last2 = max(p for p, _ in runs[-2:]) if self.policy.quantile >= 0.9 else 0.0
        estimate = max(p95, last2, pinned) * self.policy.margin
        return max(self.policy.floor_gib, estimate), clean >= self.policy.trust_after


class Simulation:
    """One run of `workload` under `policy` on `physics`, with plan seed `seed`."""

    def __init__(
        self, policy: Policy, workload: Workload, physics: Physics | None = None, seed: int = 0
    ) -> None:
        self.policy, self.workload, self.ph = policy, workload, physics or Physics()
        self.seed = seed
        self.env = simpy.Environment()
        preset = Preset(
            duration_s=workload.duration_s,
            think_s=workload.think_s,
            cpu_s=NOMINAL["cpu"],
            mem_s=6.0,
            p_mem=workload.p_mem,
        )
        self.plan = make_plan(seed, workload.agents, preset, NOMINAL)
        self.cap = workload.cap_gib
        self.budget = policy.budget_share * self.cap + policy.slack_gib
        self.per_slot = policy.budget_share * self.cap / self.ph.slots
        self.estimator = _Estimator(policy)
        rng = random.Random(f"prior:{seed}")
        for _ in range(policy.prior_runs):
            self.estimator.record("mem", self._draw_size(rng), False)
            self.estimator.record("cpu", self.ph.cpu_job_gib, False)
        self.running: list[_Job] = []
        self.line: dict[tuple[str, str], tuple[_Job, tuple[float, int], simpy.Event]] = {}
        self.served: dict = {}
        self.session_served: dict = {}
        self.turn = 0
        self.order = 0
        self.jobs: list[_Job] = []
        self.kills = 0
        self.important_kills = 0
        self.written = 0.0
        self.thrash_s = 0.0
        self.parked_s = 0.0
        self.parks = 0
        self.brakes = 0
        self.over = 0.0
        self.requeues = 0
        self.requeue_lost_s = 0.0
        self.throttles = 0
        self.last_heavy_start = -math.inf
        self.mc_rng = random.Random(f"mc:{seed}")
        self.spill_since: float | None = None
        self.last_thaw = -math.inf
        self.start = 0.0
        self.ends: list[float] = []

    # --- sizes and estimates --------------------------------------------------------------------

    def _draw_size(self, rng: random.Random, late: bool = False) -> float:
        wl = self.workload
        cv = wl.size_cv
        mean = wl.mem_gib * (1 + wl.drift if late else 1.0)
        most = self.cap - self.ph.base_gib - FIT_SPARE_GIB
        if wl.outlier_share > 0 and rng.random() < wl.outlier_share:
            mean *= wl.outlier_scale
        if wl.tail_share > 0 and rng.random() < wl.tail_share:
            mean *= rng.paretovariate(wl.tail_alpha)
        if cv <= 0:
            return min(mean, most)
        s2 = math.log(1 + cv * cv)
        # A job that cannot fit in the cap alone is no admission policy's to save: drawn again.
        for _ in range(50):
            size = mean * rng.lognormvariate(-s2 / 2, math.sqrt(s2))
            if size <= most:
                return size
        return most

    def _estimate(self, job: _Job, weight: int) -> float:
        """What `job` counts: never more than the budget, so a job whose estimate passes it
        runs alone instead of being refused."""
        return min(self.budget, self._raw_estimate(job, weight))

    def _raw_estimate(self, job: _Job, weight: int) -> float:
        p = self.policy
        assumed = weight * self.per_slot
        if job.alone:
            return self.budget
        if p.estimator == "assumed":
            return assumed
        if p.estimator == "oracle":
            return job.size
        if p.estimator == "declared":
            return self.workload.mem_gib + p.declared_pad_gib if job.kind == "mem" else assumed
        learned, trusted = self.estimator.learned(job.kind)
        if trusted and learned is not None:
            return learned
        if p.cold == "alone":
            return self.budget
        return max(assumed, learned or 0.0)

    def _heavy(self, counted: float) -> bool:
        return counted >= self.policy.heavy_share * self.cap

    # --- admission ------------------------------------------------------------------------------

    def _swap_free(self) -> float:
        return self.workload.swap_gib - sum(j.slots for j in self.running)

    def _holding_heavy(self) -> bool:
        p = self.policy
        now = self.env.now
        if p.stagger_s > 0 and now - self.last_heavy_start < p.stagger_s:
            return True
        if p.hold_swap_gib > 0 and self._swap_free() < p.hold_swap_gib:
            return True
        if p.peak_wait > 0:
            ttp = self.estimator.time_to_peak("mem")
            for j in self.running:
                young = j.started is not None and now - j.started < p.peak_wait * (ttp or 0.0)
                if j.kind == "mem" and ttp is not None and young:
                    return True
        for j in self.running:
            if p.park_hold and (j.parked_at is not None or self._settling(j)):
                return True
            if p.hold_resumed and j.was_parked:
                return True
            if p.plateau and j.kind == "mem" and not self._settled(j):
                return True
        return False

    def _settling(self, j: _Job) -> bool:
        return j.thawed_at is not None

    def _settled(self, j: _Job) -> bool:
        quiet = j.quiet_since is not None and j.footprint > 0
        return quiet and self.env.now - j.quiet_since >= self.policy.plateau_cooldown_s

    def _held_gib(self, j: _Job) -> float:
        p = self.policy
        if p.plateau and p.count_settled and j.kind == "mem" and self._settled(j):
            return j.footprint * p.settled_margin
        if p.observe or p.count_settled:
            return max(j.counted, j.footprint)
        return j.counted

    def admit(self) -> None:
        if not self.line:
            return
        hold = self._holding_heavy()
        held = tuple(
            Held(
                pool=POOL,
                host=MACHINE,
                user="user",
                weight=j.weight,
                session=j.session,
                memory=int(self._held_gib(j) * GIB),
            )
            for j in self.running
        )
        waiting = []
        for key, (job, order, _) in self.line.items():
            counted = self._estimate(job, self.policy.weight)
            if hold and self.policy.hold_outside:
                if job.kind == "mem":
                    continue
            elif hold and self._heavy(counted):
                counted = BLOCKED_GIB
            waiting.append(
                Waiting(
                    key=key,
                    pool=POOL,
                    user="user",
                    weight=self.policy.weight,
                    order=order,
                    session=job.session,
                    memory=int(counted * GIB),
                )
            )
        policy = AdmissionPolicy(
            slots={MACHINE: self.ph.slots},
            pools={POOL: PoolPolicy(host=MACHINE, reserved=3, max=self.ph.slots)},
            memory={MACHINE: int(self.budget * GIB)},
            assumed_memory={MACHINE: int(self.per_slot * GIB)},
        )
        state = State(
            held=held,
            waiting=tuple(waiting),
            served=dict(self.served),
            session_served=dict(self.session_served),
        )
        accepted: list[_Job] = []
        for grant in grants(state, policy):
            job = self.line[grant.key][0]
            if self.policy.chance and job.kind == "mem" and not self._chance_ok(job, accepted):
                continue
            accepted.append(job)
            job, _, event = self.line.pop(grant.key)
            if self._heavy(self._estimate(job, grant.weight)):
                self.last_heavy_start = self.env.now
            self.turn += 1
            self.served[(POOL, "user")] = self.turn
            self.session_served[(POOL, "user", job.session)] = self.turn
            job.weight = grant.weight
            job.counted = self._estimate(job, grant.weight)
            event.succeed()

    def _chance_ok(self, job: _Job, accepted: list[_Job]) -> bool:
        """Whether `job` may start beside the running and just-accepted `mem` jobs under the
        chance constraint. A job counted alone, or with no other `mem` job, always may."""
        p = self.policy
        others = [j for j in [*self.running, *accepted] if j.kind == "mem"]
        if not others or job.alone:
            return True
        stats = self.estimator.stats("mem")
        fixed = self.ph.base_gib + sum(j.counted for j in self.running if j.kind != "mem")
        if stats is None:
            return True
        mean, sd = stats
        if p.chance == "gauss":
            n = len(others) + 1
            return fixed + n * mean + p.chance_z * sd * math.sqrt(n) <= self.budget
        # "mc": what each running job uses now is known to be at least its footprint.
        peaks = self.estimator.peaks("mem")
        over = 0
        for _ in range(p.mc_draws):
            total = fixed + self.mc_rng.choice(peaks)
            for o in others:
                total += max(o.footprint, self.mc_rng.choice(peaks))
            over += total > self.budget
        return over <= p.chance_eps * p.mc_draws

    # --- agents ---------------------------------------------------------------------------------

    def _agent(self, agent: int, steps: list):
        rng = random.Random(f"sizes:{self.seed}:{agent}")
        for seq, (think, kind) in enumerate(steps):
            late = seq >= self.workload.drift_at * len(steps)
            size = self._draw_size(rng, late) if kind == "mem" else self.ph.cpu_job_gib
            important = rng.random() < self.workload.p_important
            yield self.env.timeout(think)
            attempt, kills, alone = 0, 0, False
            while True:
                job = _Job(agent, seq, kind, size, important, attempt=attempt, alone=alone)
                attempt += 1
                yield self.env.process(self._job(job))
                if job.requeued and attempt < 8:
                    alone = self.policy.requeue_count == "alone"
                    continue
                kills += job.killed
                if not (job.killed and self.workload.retry and kills < 3):
                    break

    def _job(self, job: _Job):
        ph = self.ph
        job.issued = self.env.now
        self.jobs.append(job)
        yield self.env.timeout(ph.start_s)
        granted = self.env.event()
        self.order += 1
        self.line[job.key] = (job, (self.env.now, self.order), granted)
        self.admit()
        yield granted
        job.started = self.env.now
        job.finished = self.env.event()
        self.running.append(job)
        yield job.finished
        yield self.env.timeout(ph.end_s)
        job.done = self.env.now
        self.ends.append(job.done)
        self.admit()

    # --- the machine ----------------------------------------------------------------------------

    def _work(self, job: _Job) -> float:
        return self.ph.work_mem if job.kind == "mem" else self.ph.work_cpu

    def _shape(self, u: float) -> float:
        """The share of its size a `mem` job has touched at `u` of its work."""
        ramp, late = self.workload.ramp, self.workload.late_share
        if ramp <= 0:
            return 1.0
        first = (1 - late) * min(1.0, u / ramp)
        return first + late * min(1.0, max(0.0, (u - LATE_AT) / ramp))

    def _growing(self, j: _Job) -> bool:
        if j.kind != "mem" or j.footprint >= j.size:
            return False
        u = j.progress / self._work(j)
        return self._shape(u + 1e-3) > self._shape(u)

    def _demand(self, exclude: _Job | None = None) -> float:
        """Memory the cap must hold now: what every unparked job has touched (a thawed job only
        what it has read back), a parked job's memory not yet pushed out, and the base."""
        total = self.ph.base_gib
        for j in self.running:
            if j is exclude:
                continue
            if j.parked_at is not None:
                total += max(0.0, j.footprint - j.slots)
            else:
                total += j.footprint - j.swapped
            # What a park put in zswap stays in the cap, compressed.
            if j.zpool > 0:
                total += j.zpool / self.policy.park_zswap
        return total

    def _end(self, job: _Job, killed: bool, requeued: bool = False) -> None:
        self.running.remove(job)
        job.killed = killed and not requeued
        job.requeued = requeued
        if requeued:
            self.requeues += 1
            self.requeue_lost_s += self.env.now - (job.started or self.env.now)
            if self.policy.bump_on_net and not job.alone:
                self.estimator.record(job.kind, job.counted, True)
        else:
            self.estimator.record(job.kind, job.peak, killed)
            if not killed and job.kind == "mem" and job.started is not None:
                self.estimator.record_ttp(job.kind, job.peak_at - job.started)
        assert job.finished is not None
        job.finished.succeed()

    def _victim(self) -> _Job:
        mem = [j for j in self.running if j.kind == "mem"] or self.running

        def score(j: _Job) -> tuple:
            protected = self.policy.protect and j.important
            return (not protected, j.footprint + j.slots, j.started or 0.0)

        return max(mem, key=score)

    def _kill(self) -> None:
        victim = self._victim()
        self.kills += 1
        self.important_kills += victim.important
        self._end(victim, killed=True)

    def _step(self) -> None:
        ph, p, dt, now = self.ph, self.policy, self.ph.dt_s, self.env.now
        cap = self.cap
        active = [j for j in self.running if j.parked_at is None]
        threads = sum(j.weight for j in active)
        each = ph.capacity(threads) / threads if threads else 0.0
        over = max(0.0, self._demand() - cap)
        slow = 1.0 + ph.thrash_per_gib * over
        growing = [j for j in active if self._growing(j) and not j.braked and j.high is None]
        room = cap - self._demand()
        # Footprints may grow into free memory at once, and past it only as fast as swap takes.
        allowance = max(0.0, room) + ph.swap_out_gib_s * dt
        share = allowance / len(growing) if growing else 0.0
        pushing = False
        for j in active:
            rate = j.weight * each
            if j.kind == "mem" and over > 0:
                rate /= slow
            if j.swapped > 0:
                rate *= max(0.05, 1 - j.swapped / j.footprint)
            if j.braked:
                rate *= p.brake_share
            if j.high is not None and j.excess > 0:
                rate *= p.throttle_share / (1.0 + ph.thrash_per_gib * j.excess)
            work = self._work(j)
            step = rate * dt
            if j.kind == "mem":
                grown = j.size * self._shape((j.progress + step) / work) - j.footprint
                if j in growing and grown > share > 0:
                    step *= share / grown
                elif j in growing and share <= 0:
                    step = 0.0
            before = j.footprint - j.swapped
            j.progress += step
            if j.kind == "mem":
                want = j.size * self._shape(j.progress / work)
                if j.high is not None:
                    # Over its memory.high the job's own pages go to its own swap.
                    excess = max(0.0, want - j.high)
                    if excess > j.excess:
                        self.written += excess - j.excess
                        j.slots += excess - j.excess
                        pushing = True
                    j.excess = excess
                    if excess > 0:
                        self.written += ph.churn_gib_s * dt
                    j.footprint = max(j.footprint, min(want, j.high))
                else:
                    j.footprint = max(j.footprint, want)
                if j.footprint + j.excess - j.peak_mark >= 0.01 * j.size:
                    j.peak_mark, j.peak_at = j.footprint + j.excess, now
            j.peak = max(j.peak, j.footprint + j.excess)
            if j.swapped > 0:
                fast = ph.zswap_page_in_gib_s if j.zpool > 0 else ph.page_in_gib_s
                j.swapped = max(0.0, j.swapped - fast * dt)
                j.zpool = min(j.zpool, j.swapped)
                if not ph.sticky_slots:
                    j.slots = min(j.slots, j.swapped)
                if j.swapped <= 0:
                    j.base_slots = j.slots
            resident = j.footprint - j.swapped
            j.growth_ema += dt / ph.settle_tau_s * ((resident - before) / dt - j.growth_ema)
            floor = max(p.plateau_floor_gib_s, ph.settle_share * j.footprint)
            if j.growth_ema > floor:
                j.quiet_since = None
            elif j.quiet_since is None:
                j.quiet_since = now
            if j.thawed_at is not None:
                quiet = j.swapped <= 0 and j.quiet_since is not None
                if (quiet and now - j.quiet_since >= ph.settle_cooldown_s) or (
                    now - j.thawed_at >= ph.settle_max_s
                ):
                    j.thawed_at = None
        for j in [j for j in self.running if j.parked_at is not None]:
            self.parked_s += dt
            # Reclaim stops where the cap's swap is full, or where it cannot reach further.
            free = max(0.0, self.workload.swap_gib - sum(o.slots for o in self.running))
            add = min(ph.reclaim_gib_s * dt, free, j.footprint * ph.reclaim_share - j.slots)
            if p.park_zswap > 0:
                pool = sum(o.zpool for o in self.running) / p.park_zswap
                add = min(add, (ph.zswap_pool_gib - pool) * p.park_zswap)
            if add > 0:
                j.slots += add
                if p.park_zswap > 0:
                    j.zpool += add
                else:
                    self.written += add
        # Overflow: what the cap cannot hold goes to swap, and pages cycle while it lasts.
        over = max(0.0, self._demand() - cap)
        if over > self.over:
            self.written += over - self.over
        hot = [j for j in self.running if j.kind == "mem" and j.parked_at is None]
        # A job still reading its own pages back cannot take more swap: its limit is used up.
        fresh = [j for j in hot if j.swapped <= 0]
        short = False
        if over > 0 and hot:
            self.thrash_s += dt
            self.written += ph.churn_gib_s * dt
            held = sum(j.slots - j.base_slots for j in fresh)
            target = max(over, min(held + ph.slot_growth_gib_s * dt, over + ph.slot_dup_gib))
            if fresh:
                add = max(0.0, target - held)
                weight = sum(j.footprint for j in fresh) or 1.0
                for j in fresh:
                    j.slots += add * j.footprint / weight
            # Under parking each running job may hold only its own overflow of swap.
            limit = ph.overflow_gib * len(fresh)
            short = (p.park or p.swap_limit) and target > limit + 1e-6
            short = short or not fresh
        self.over = over
        slots = sum(j.slots for j in self.running)
        full = (over > 0 or pushing) and slots >= self.workload.swap_gib - 1e-6
        if full or short:
            self._kill()
            self.over = max(0.0, self._demand() - cap)
            return
        for j in [j for j in self.running if j.progress >= self._work(j) - 1e-9]:
            self._end(j, killed=False)
        if p.brake:
            self._brake()
        if p.throttle:
            self._throttle()
        if p.park or p.requeue:
            self._net(over)

    def _brake(self) -> None:
        mem = [j for j in self.running if j.kind == "mem" and j.parked_at is None]
        braked = [j for j in mem if j.braked]
        full = self._demand() >= self.policy.brake_at * self.cap
        for j in braked:
            others = self._demand(exclude=j)
            alone = all(o is j or o.braked for o in mem)
            if alone or others + j.size <= self.ph.thaw_at * self.cap:
                j.braked = False
        growing = [j for j in mem if self._growing(j) and not j.braked]
        if full and len(mem) >= 2 and growing:
            self._order(growing)[-1].braked = True
            self.brakes += 1

    def _throttle(self) -> None:
        mem = [j for j in self.running if j.kind == "mem" and j.parked_at is None]
        for j in [j for j in mem if j.high is not None]:
            others = self._demand(exclude=j)
            alone = all(o is j for o in mem)
            if alone or others + j.footprint + j.excess <= self.ph.thaw_at * self.cap:
                # Lifted: what went over its high pages back in, as after a thaw.
                j.footprint += j.excess
                j.swapped = j.excess
                j.excess, j.high = 0.0, None
                if j.swapped > 0:
                    j.thawed_at = self.env.now
                    j.quiet_since = None
        full = self._demand() >= self.policy.throttle_at * self.cap
        growing = [j for j in mem if self._growing(j) and j.high is None and not j.braked]
        if full and len(mem) >= 2 and growing:
            youngest = self._order(growing)[-1]
            youngest.high = youngest.footprint
            self.throttles += 1

    def _order(self, jobs: list[_Job]) -> list[_Job]:
        """Oldest first; with `protect`, important jobs before the rest."""
        return sorted(jobs, key=lambda j: (not (self.policy.protect and j.important), j.started))

    def _requeue(self, victim: _Job) -> None:
        self._end(victim, killed=True, requeued=True)
        self.over = max(0.0, self._demand() - self.cap)

    def _net(self, over: float) -> None:
        """The net's response to a spill: park the youngest `mem` job, or requeue it."""
        ph, p, now = self.ph, self.policy, self.env.now
        mem = [j for j in self.running if j.kind == "mem"]
        unparked = [j for j in mem if j.parked_at is None]
        parked = sorted((j for j in mem if j.parked_at is not None), key=lambda j: j.parked_at)
        if p.requeue == "always":
            near = over > 0 if p.requeue_at >= 1 else self._demand() >= p.requeue_at * self.cap
            near = near and (over > 0 or any(self._growing(j) for j in unparked))
        else:
            near = over > 0
        due = near and len(unparked) >= 2 and now - self.last_thaw >= ph.thaw_gap_s
        if due:
            if self.spill_since is None:
                self.spill_since = now
            if now - self.spill_since >= ph.detect_s:
                self.spill_since = None
                victim = self._order(unparked)[-1]
                need = victim.footprint * ph.reclaim_share - victim.slots + p.park_room_gib
                room = self._swap_free() >= need
                if p.park_zswap > 0:
                    # In zswap the victim still takes 1 / ratio of its size in the cap, and
                    # the pool must take it.
                    pool = sum(o.zpool for o in self.running) / p.park_zswap
                    fits = ph.zswap_pool_gib - pool >= victim.footprint / p.park_zswap
                    rest = self._demand(exclude=victim) + victim.footprint / p.park_zswap
                    room = room and fits and rest <= self.cap
                young = now - (victim.started or now) < p.requeue_young_s
                if (
                    p.requeue == "always"
                    or (p.requeue == "noroom" and not room)
                    or (p.requeue == "young" and (young or not room))
                ):
                    self._requeue(victim)
                    return
                if p.park and (room or not p.park_room):
                    victim.parked_at = now
                    victim.was_parked = True
                    victim.braked = False
                    if p.bump_on_net:
                        self.estimator.record(victim.kind, victim.counted, True)
                    self.parks += 1
        else:
            self.spill_since = None
        if parked:
            j = parked[0]
            others = self._demand(exclude=j)
            alone = not [o for o in mem if o.parked_at is None]
            if (
                alone
                or others + j.footprint <= ph.thaw_at * self.cap
                or (now - j.parked_at >= ph.max_park_s)
            ):
                j.parked_at = None
                j.swapped = j.slots - j.base_slots if ph.sticky_slots else j.slots
                j.swapped = min(j.swapped, j.footprint)
                j.thawed_at = now
                j.quiet_since = None
                self.last_thaw = now

    def _physics(self):
        while True:
            yield self.env.timeout(self.ph.dt_s)
            before = len(self.running)
            self._step()
            if len(self.running) != before:
                self.admit()

    def _ticks(self):
        while True:
            yield self.env.timeout(self.ph.tick_s)
            self.admit()

    def run(self, horizon: float = 20_000.0) -> Result:
        agents = [self.env.process(self._agent(i, s)) for i, s in enumerate(self.plan)]
        self.env.process(self._physics())
        self.env.process(self._ticks())
        everyone = self.env.all_of(agents)
        while not everyone.processed and self.env.peek() < horizon:
            self.env.step()
        notes = [] if everyone.processed else [f"cut at {horizon:g} s"]
        done = [j for j in self.jobs if j.done is not None]
        turn = [j.done - j.issued for j in done if not j.killed]
        wait = [j.started - j.issued for j in done if j.started is not None]
        return Result(
            makespan=max(self.ends, default=0.0) - self.start,
            jobs=len(self.jobs),
            mem_jobs=sum(1 for j in self.jobs if j.kind == "mem"),
            kills=self.kills,
            important_kills=self.important_kills,
            swap_written_gib=self.written,
            thrash_s=self.thrash_s,
            parked_s=self.parked_s,
            parks=self.parks,
            brakes=self.brakes,
            turnaround_mean=sum(turn) / len(turn) if turn else 0.0,
            wait_mean=sum(wait) / len(wait) if wait else 0.0,
            failed=sum(1 for j in self.jobs if j.killed),
            requeues=self.requeues,
            requeue_lost_s=self.requeue_lost_s,
            throttles=self.throttles,
            notes=notes,
        )


def run(policy: Policy, workload: Workload, physics: Physics | None = None, seed: int = 0):
    """One run's `Result`."""
    return Simulation(policy, workload, physics, seed).run()


def with_physics(physics: Physics, **changes) -> Physics:
    return replace(physics, **changes)
