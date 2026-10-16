"""What the simulated agents run, and the conditions they run it under.

A workload is either the plan `bench/run.py` makes (its seeded steps of think time and job
kind, `make_plan`), the plan a measured run recorded in its `spec.json`, or a measured run's
`jobs.csv` replayed (think time is the gap between one job's end and the agent's next call).

A condition is a `Setup`: no leasehold (`unmanaged`, `static-jobs`), or a daemon on the config
`leasehold init --user` writes for the machine, changed only where the condition says
(`config_for`).
"""

from __future__ import annotations

import csv
import json
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from bench.run import Preset, make_plan
from bench.sim.engine import Setup
from bench.sim.model import Job, Kind, Machine, Speedup
from leasehold.machine import MachineConfig
from leasehold.template import render, render_user_0_2

GIB = 1 << 30
MIB = 1 << 20


@dataclass(frozen=True)
class Params:
    """The machine and job parameters a calibration fits (`bench.sim.calibrate`). The
    defaults are the fit to the 8-CPU results under `bench/results/8cpu-16g/`."""

    #: Full cores as a share of the CPUs, and what each CPU past them adds.
    core_share: float = 0.5
    smt_yield: float = 0.443
    #: Bytes a `cpu` job hashes per CPU-second, and a `mem` job.
    rate_cpu: float = 1.419e9
    rate_mem: float = 1.310e9
    #: Seconds a `mem` job takes to touch one GiB (one thread).
    touch_s_per_gib: float = 1.290
    cpu: Speedup = field(default_factory=lambda: Speedup(A=7.70, sigma=0.008))
    mem: Speedup = field(default_factory=lambda: Speedup(A=5.67, sigma=0.429))
    #: The spread of a job's work, as a coefficient of variation.
    noise_cv: float = 0.057

    def machine(self, cpus: int, memory_bytes: int, background: float) -> Machine:
        return Machine(
            cpus=cpus,
            cores=cpus * self.core_share,
            smt_yield=self.smt_yield,
            background=background,
            memory_bytes=memory_bytes,
        )

    def kinds(self, mem_bytes: int) -> dict[str, Kind]:
        return {
            "cpu": Kind(self.cpu, memory=32 << 20),
            "mem": Kind(self.mem, memory=mem_bytes + (32 << 20)),
        }

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["cpu"] = {"A": self.cpu.A, "sigma": self.cpu.sigma}
        out["mem"] = {"A": self.mem.A, "sigma": self.mem.sigma}
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> Params:
        raw = dict(raw)
        raw["cpu"] = Speedup(**raw["cpu"])
        raw["mem"] = Speedup(**raw["mem"])
        return cls(**raw)


@dataclass(frozen=True)
class Weights:
    """What each job asks for: a count (`4`) or a range (`2-4`)."""

    least: int
    most: int

    @classmethod
    def parse(cls, text: str) -> Weights:
        least, _, most = str(text).partition("-")
        lo = int(least)
        return cls(lo, int(most) if most else lo)

    def __str__(self) -> str:
        return str(self.least) if self.least == self.most else f"{self.least}-{self.most}"


@dataclass(frozen=True)
class Workload:
    """A synthetic workload as `bench/run.py` plans one: `agents` agents (spread over
    `users` users, each agent its own session), each thinking for an exponential time and
    then running a `cpu` or a `mem` job, until its planned time passes `duration_s`. A job is
    sized to take `cpu_s` (or `mem_s` plus its touch) alone on every CPU of the machine."""

    name: str = "light"
    agents: int = 8
    users: int = 1
    duration_s: float = 240.0
    think_s: float = 30.0
    cpu_s: float = 10.0
    mem_s: float = 6.0
    p_mem: float = 0.35
    #: A `mem` job's memory as a share of the machine's.
    mem_share: float = 0.16
    #: What a `mem` job declares: "none", "total" (its size plus 512 MiB) or "per-slot".
    declare: str = "none"


#: The mixes of the policy study. `light` is the bench's main mix (2.5 GiB `mem` jobs on
#: 16 GiB); `heavy` has more `mem` jobs, each the size the 0.2 bench used (0.7 x RAM / 3,
#: three of them pass 70% of the memory); `heavy-declared` is `heavy` with each `mem` job
#: declaring its size plus 512 MiB, as `lh-enforced-declared` does.
WORKLOADS = {
    "light": Workload(name="light"),
    "heavy": Workload(name="heavy", p_mem=0.6, mem_share=0.7 / 3),
    "heavy-declared": Workload(
        name="heavy-declared", p_mem=0.6, mem_share=0.7 / 3, declare="total"
    ),
}


def plan_steps(
    workload: Workload,
    seed: int,
    params: Params,
    machine: Machine,
    weights: Weights,
    *,
    per_slot_memory: int | None = None,
) -> tuple[list[list[tuple[float, Job]]], int]:
    """Each agent's `(think_s, job)` steps for `workload` on `machine`, from `bench/run.py`'s
    plan for `seed`, and a `mem` job's size in bytes."""
    mem_bytes = int(workload.mem_share * machine.memory_bytes)
    touch = params.touch_s_per_gib * mem_bytes / GIB
    preset = Preset(
        duration_s=workload.duration_s,
        think_s=workload.think_s,
        cpu_s=workload.cpu_s,
        mem_s=workload.mem_s,
        p_mem=workload.p_mem,
    )
    nominal = {"cpu": workload.cpu_s, "mem": workload.mem_s + touch}
    plan = make_plan(seed, workload.agents, preset, nominal)
    kinds = params.kinds(mem_bytes)
    # The work that takes the nominal time alone on every CPU, the background running beside.
    work = {
        k: nominal[k] * machine.rates([kinds[k].speedup(machine.cpus)])[0] for k in ("cpu", "mem")
    }
    declared = mem_bytes + 512 * MIB
    steps = []
    for agent, agent_steps in enumerate(plan):
        out = []
        for seq, (think, kind) in enumerate(agent_steps):
            job = Job(
                agent=agent,
                seq=seq,
                kind=kind,
                work=work[kind],
                weight=weights.least,
                max_weight=weights.most,
                user=f"user{agent % max(1, workload.users)}",
                session=f"agent{agent}",
            )
            if kind == "mem" and workload.declare == "total":
                job.memory = declared
            elif kind == "mem" and workload.declare == "per-slot":
                job.memory_per_slot = per_slot_memory or -(-declared // max(1, weights.least))
            out.append((float(think), job))
        steps.append(out)
    return steps, mem_bytes


def measured_steps(
    run_dir: Path, params: Params, weights: dict[str, Weights], *, declared: bool = False
) -> tuple[list[list[tuple[float, Job]]], int]:
    """The steps a measured run planned (its `spec.json`), with each job's work worked out from
    the bytes it hashed and touched, and a `mem` job's size."""
    spec = json.loads((run_dir / "spec.json").read_text())
    mem_bytes = int(spec["mem_bytes"])
    work = {
        "cpu": spec["work"]["cpu"] / params.rate_cpu,
        "mem": spec["work"]["mem"] / params.rate_mem + params.touch_s_per_gib * mem_bytes / GIB,
    }
    steps = []
    for agent, agent_steps in enumerate(spec["plan"]):
        out = []
        for seq, (think, kind) in enumerate(agent_steps):
            w = weights[kind]
            job = Job(
                agent=agent,
                seq=seq,
                kind=kind,
                work=work[kind],
                weight=w.least,
                max_weight=w.most,
                user="user0",
                session=f"agent{agent}",
            )
            if declared and kind == "mem":
                job.memory = mem_bytes + 512 * MIB
            out.append((float(think), job))
        steps.append(out)
    return steps, mem_bytes


def replay_steps(
    jobs_csv: Path, spec: dict, params: Params, weights: dict[str, Weights], start: float
) -> list[list[tuple[float, Job]]]:
    """A measured run's `jobs.csv` as steps: each agent thinks for the time between its last
    call's end (or the run's `start`) and its next call."""
    with jobs_csv.open(newline="") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: (int(r["agent"]), int(r["seq"])))
    mem_bytes = int(spec["mem_bytes"])
    work = {
        "cpu": spec["work"]["cpu"] / params.rate_cpu,
        "mem": spec["work"]["mem"] / params.rate_mem + params.touch_s_per_gib * mem_bytes / GIB,
    }
    agents: dict[int, list[tuple[float, Job]]] = {}
    last: dict[int, float] = {}
    for r in rows:
        agent = int(r["agent"])
        issued = float(r["issued"])
        think = max(0.0, issued - last.get(agent, start))
        last[agent] = float(r["done"])
        w = weights[r["kind"]]
        job = Job(agent, int(r["seq"]), r["kind"], work[r["kind"]], w.least, w.most,
                  user="user0", session=f"agent{agent}")  # fmt: skip
        agents.setdefault(agent, []).append((think, job))
    return [agents[a] for a in sorted(agents)]


# --- conditions ---------------------------------------------------------------------------------


def config_for(
    *,
    cpus: int,
    memory_bytes: int,
    kept: int = 2,
    headroom: str | int = "auto",
    budget: str | None = None,
    v020: bool = False,
    hard_cpu: bool = False,
) -> MachineConfig:
    """The config `leasehold init --user` writes for a machine of `cpus` CPUs and
    `memory_bytes` (`kept` CPUs left out of the slots), with the work pool's `headroom` and the
    `[memory] budget` replaced when given. `v020`: what 0.2.0's `init --user` wrote instead."""
    slots = max(1, cpus - kept)
    values = {
        "name": "sim",
        "slots": slots,
        "cpus": slots,
        "memory": f"{max(1, memory_bytes * 3 // 4 // GIB)}g",
        "about": "A simulated machine.",
    }
    text = render_user_0_2(**values, release="0.2.0") if v020 else render(mode="user", **values)
    raw = tomllib.loads(text)
    if not v020:
        raw["pools"]["work"]["headroom"] = headroom
        if budget is not None:
            raw["memory"]["budget"] = budget
    if hard_cpu:
        raw.setdefault("contain", {})["hard_cpu"] = True
    return MachineConfig.model_validate(raw)


def headroom_value(text: str, slots: int) -> str | int:
    """`auto`, a count, or `auto+N` (the auto value plus N)."""
    if text == "auto":
        return "auto"
    if text.startswith("auto+"):
        return slots // 4 + int(text[5:])
    return int(text)


#: The measured conditions the simulator can reproduce, and how.
CONDITIONS = {
    "unmanaged": "no leasehold; every job uses every CPU",
    "static-jobs": "no leasehold; every job uses its weight in workers",
    "lh-advisory": "leasehold, jobs take the workers their lease gives",
    "lh-enforced": "the same (containment does not change the model)",
    "lh-enforced-declared": "as lh-enforced, mem jobs declare their size plus 512 MiB",
    "lh-enforced-v020": "as lh-enforced, on 0.2.0's config (no memory counted), weight 4",
    "lh-advisory-greedy": "leasehold, jobs use every CPU",
    "lh-enforced-greedy": "the same",
    "lh-hardcpu-greedy": "jobs use every CPU, held to their weight in CPUs",
    "lh-elastic": "leases grow and shrink to admission.targets (needs elastic leases)",
}


def setup_for(
    condition: str,
    *,
    cpus: int,
    memory_bytes: int,
    kept: int = 2,
    headroom: str | int = "auto",
    budget: str | None = None,
    guard: bool = True,
) -> Setup:
    """The `Setup` of a condition named as `bench/run.py` names it."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition {condition!r}")
    if condition == "unmanaged":
        return Setup(condition)
    if condition == "static-jobs":
        return Setup(condition, static=True)
    config = config_for(
        cpus=cpus,
        memory_bytes=memory_bytes,
        kept=kept,
        headroom=headroom,
        budget=budget,
        v020=condition == "lh-enforced-v020",
    )
    return Setup(
        condition,
        config=config,
        greedy=condition.endswith("-greedy"),
        hard_cpu=condition == "lh-hardcpu-greedy",
        elastic=condition == "lh-elastic",
        guard=guard,
    )


def with_config(setup: Setup, config: MachineConfig) -> Setup:
    return replace(setup, config=config)
