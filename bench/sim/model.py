"""How fast a job runs: its speedup on n workers, and how a machine's CPUs share out among the
jobs running on it.

A job has `work`, in CPU-seconds: how long it would take on one thread with a core to itself.
On `n` workers alone it runs at `speedup(n)` such threads at once, a curve from Downey's model
of parallel programs ("A parallel workload model and its implications for processor
allocation", 1997), set by `A`, the job's average parallelism, and `sigma`, how much that
parallelism varies over its run (0: it is A throughout).

The machine has `cpus` CPUs, of which `cores` are full cores; the rest are SMT siblings (or a
host's oversubscribed vCPUs) that add `smt_yield` of a core each. `background` threads from
other work compete with the jobs for them. When `k` threads want to run, the machine runs them
at `capacity(k) / k` of a core each, so a job's rate is its busy threads times that. Memory
bandwidth, caches, swap and the page cache are not modelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Speedup:
    """Downey's speedup curve: `A` the average parallelism, `sigma` its variance."""

    A: float
    sigma: float = 0.0

    def __call__(self, n: float) -> float:
        """The speedup on `n` workers (at least 1): how many threads' worth of work the job
        gets done per second with a core for each worker."""
        a, s = max(1.0, self.A), max(0.0, self.sigma)
        n = max(1.0, n)
        if s <= 1:
            if n <= a:
                return a * n / (a + s * (n - 1) / 2)
            if n <= 2 * a - 1:
                return a * n / (s * (a - 0.5) + n * (1 - s / 2))
            return a
        if n <= a + a * s - s:
            return n * a * (s + 1) / (s * (n + a - 1) + a)
        return a


@dataclass(frozen=True)
class Machine:
    """A machine's CPUs as the jobs see them."""

    cpus: int
    #: Full cores among the CPUs; None: every CPU is one.
    cores: float | None = None
    #: What one CPU past `cores` adds, as a share of a core.
    smt_yield: float = 1.0
    #: Threads of other work that want a CPU all the time.
    background: float = 0.0
    #: The machine's memory, in bytes.
    memory_bytes: int = 16 << 30

    def capacity(self, threads: float) -> float:
        """How many cores' worth of work `threads` runnable threads get done per second."""
        cores = self.cpus if self.cores is None else min(self.cores, self.cpus)
        k = min(max(0.0, threads), float(self.cpus))
        return min(k, cores) + self.smt_yield * max(0.0, k - cores)

    def rates(self, demands: list[float]) -> list[float]:
        """Each job's rate, in CPU-seconds of work per second, given how many threads each
        keeps busy (`demands`): its threads times the share of a core each thread gets."""
        k = sum(demands) + self.background
        if k <= 0:
            return [0.0 for _ in demands]
        each = self.capacity(k) / k
        return [d * each for d in demands]


@dataclass(frozen=True)
class Kind:
    """One kind of job: its speedup curve and its memory."""

    speedup: Speedup
    #: Memory, in bytes, a job of this kind uses whatever its worker count.
    memory: int = 0
    #: Memory, in bytes, each worker adds.
    memory_per_worker: int = 8 << 20


@dataclass
class Job:
    """One job: its kind, how much work it is, and what it asks for."""

    agent: int
    seq: int
    kind: str
    work: float
    #: The least and the most slots it asks for.
    weight: int
    max_weight: int
    #: Memory it declares: a total, or per slot; None: none.
    memory: int | None = None
    memory_per_slot: int | None = None
    user: str = "user"
    session: str = ""
    #: Set as it runs.
    issued: float = 0.0
    started: float | None = None
    ended: float | None = None
    done: float | None = None
    granted: int | None = None
    workers: int | None = None
    labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Pressure:
    """An optional stand-in for swap, which the model otherwise leaves out. Once the memory the
    jobs use passes `limit` (the machine's memory less what other work uses and `reserve`, or
    `cap`, the jobs' own memory limit, when that is lower), every running job takes
    `1 + slowdown` times as long for each GiB past it, and the memory guard reads the share of
    time the jobs lose as memory pressure."""

    reserve: int
    slowdown: float = 0.0
    cap: int | None = None

    def limit(self, memory_bytes: int, other_memory: int) -> int:
        machine = memory_bytes - other_memory - self.reserve
        return machine if self.cap is None else min(machine, self.cap)
