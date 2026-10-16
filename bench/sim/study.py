"""The policy study: weight defaults compared on simulated machines, for the real benchmark to
choose among.

Two parts, both on every one of `WORKLOADS` and on three machines (6, 8 and 16 slots: 8, 10
and 18 CPUs with 2 kept back, 2 GiB of memory a CPU, `OTHER_GIB` of it used by other work):

* `named`: today's fixed weight 4 (the control) against the ranges 2-4 and 1-4, each with the
  pool's headroom at `auto`, 0, and one more than `auto`; 3-3 and 3-4 with headroom 1; and 4-8
  (today's weight as the least) with headroom `auto`;
* `screen`: a Latin hypercube over the range's least and most, the headroom and the CPUs kept
  back, `points` configurations per machine and workload.

Every configuration runs the same `reps` plans (seeds 1 to `reps`), with the same job-to-job
spread on each seed. Each configuration's difference from the control is the mean over seeds
of the two runs' difference, with its 95% interval (`paired`); the named configurations' runs
are also kept seed by seed.

A configuration whose jobs used more memory than the budget in any replication is marked over
budget: the budget counts what jobs declare or are assumed to use, and the jobs used more.
`untrusted` marks the results the calibration cannot vouch for: those where every slot was
held for at least `FULL_SHARE` of the run, or where the jobs' memory left less available than
the measured machine had when it went into swap (`swap_limit_gib`).

Configurations are compared on four measures, all lower-better: makespan, p95 wait, the share
of slots left idle while something waited, and the share of the time the machine had at most
one CPU idle (where the measured wakeup latency went bad, docs/bench.md). `pareto` keeps, of
those never over budget, the ones no other configuration beats on all four.
"""

from __future__ import annotations

import csv
import json
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from bench.sim.engine import Simulation
from bench.sim.metrics import aggregate, paired, summarize
from bench.sim.search import latin_hypercube
from bench.sim.workload import (
    GIB,
    WORKLOADS,
    Params,
    Weights,
    Workload,
    config_for,
    headroom_value,
    plan_steps,
    setup_for,
)

#: (slots, CPUs) of the three machines, with 2 CPUs kept back as `init --user` keeps them.
MACHINES = {6: 8, 8: 10, 16: 18}
#: The person's own work beside the agents, in busy threads.
BACKGROUND = 1.0
#: The memory the person's own work uses, on every machine: about what the measured machine's
#: other processes used beside the benchmark.
OTHER_GIB = 6.0
OBJECTIVES = ("makespan_s", "wait_p95_s", "idle_while_waiting", "saturated_share")
#: The measures compared with the control seed by seed.
PAIRED = ("makespan_s", "wait_p95_s", "idle_while_waiting", "saturated_share", "actual_peak_gib")
#: Every slot held for this share of a run or more makes its result untrusted.
FULL_SHARE = 0.10
#: The available memory under which the measured machine went into swap (about 5 GiB, in the
#: two-at-once run, with jobs well inside their limit): jobs that leave less make a result
#: untrusted.
SWAP_AVAILABLE_GIB = 5.0
CONTROL = ("4", "auto", 2)


@dataclass(frozen=True)
class Config:
    """One policy to try: what every job asks for, the work pool's headroom and the CPUs kept
    out of the slots."""

    weights: str
    headroom: str = "auto"
    kept: int = 2
    design: str = "named"

    def key(self) -> tuple:
        return (self.weights, self.headroom, self.kept)

    def label(self) -> str:
        return f"w={self.weights} headroom={self.headroom} kept={self.kept}"


def swap_limit_gib(cpus: int) -> float:
    """The memory the jobs can use on the study machine of `cpus` CPUs before it leaves less
    available than `SWAP_AVAILABLE_GIB`."""
    return 2 * cpus - OTHER_GIB - SWAP_AVAILABLE_GIB


def run_one(args) -> dict:
    """One replication of `config` for `workload` on a machine of `cpus` CPUs."""
    config, workload, cpus, seed, params = args
    memory = 2 * GIB * cpus
    machine = params.machine(cpus, memory, BACKGROUND)
    slots = cpus - config.kept
    steps, mem_bytes = plan_steps(workload, seed, params, machine, Weights.parse(config.weights))
    setup = setup_for("lh-enforced", cpus=cpus, memory_bytes=memory, kept=config.kept)
    setup = replace(
        setup,
        config=config_for(
            cpus=cpus,
            memory_bytes=memory,
            kept=config.kept,
            headroom=headroom_value(config.headroom, slots),
        ),
    )
    sim = Simulation(
        setup,
        machine,
        params.kinds(mem_bytes),
        steps,
        other_memory=int(OTHER_GIB * GIB),
        pressure=params.pressure(),
        seed=seed,
        noise_cv=params.noise_cv,
    )
    return summarize(sim.run(), BACKGROUND)


def named_configs() -> list[Config]:
    out = [Config("4")]
    for weights in ("2-4", "1-4"):
        out += [Config(weights, headroom) for headroom in ("auto", "0", "auto+1")]
    return [*out, Config("3-3", "1"), Config("3-4", "1"), Config("4-8")]


def screen_configs(slots: int, points: int, seed: int = 7) -> list[Config]:
    """`points` configurations from a Latin hypercube over (least, most, headroom, kept), each
    factor's range cut into `points` strata: least 1 to 4, most from least to the slots,
    headroom 0 to 3, kept 0 to 3."""
    if points < 1:
        return []
    out = []
    for u_least, u_most, u_head, u_kept in latin_hypercube(4, points, seed):
        kept = min(3, int(u_kept * 4))
        top = slots + 2 - kept
        least = 1 + min(3, int(u_least * 4))
        most = least + min(top - least, int(u_most * (top - least + 1)))
        headroom = min(3, int(u_head * 4))
        out.append(Config(f"{least}-{most}", str(headroom), kept, "screen"))
    return list(dict.fromkeys(out))


def evaluate(configs, workload: Workload, cpus: int, reps: int, params: Params, pool):
    """Each configuration's mean measures, with its paired differences from the control
    (named configurations only), and the named configurations' per-seed measures."""
    configs = list(dict.fromkeys(configs))
    seeds = range(1, reps + 1)
    work = [(c, workload, cpus, seed, params) for c in configs for seed in seeds]
    results = list(pool.map(run_one, work, chunksize=8))
    by_config = {c: results[i * reps : (i + 1) * reps] for i, c in enumerate(configs)}
    control = next(c for c in configs if c.key() == CONTROL)
    limit = swap_limit_gib(cpus)
    rows, runs = [], []
    for c, results_c in by_config.items():
        head = {
            "workload": workload.name,
            "slots": cpus - c.kept,
            "machine_cpus": cpus,
            "design": c.design,
            "weights": c.weights,
            "headroom": c.headroom,
            "kept": c.kept,
        }
        agg = aggregate(results_c)
        row = {**head, **agg}
        row["untrusted"] = agg["full_share"] >= FULL_SHARE or agg["actual_peak_gib"] > limit
        for key in PAIRED:
            mean, half = paired(
                [float(r[key]) for r in results_c], [float(r[key]) for r in by_config[control]]
            )
            row[f"{key}_diff"], row[f"{key}_diff_ci"] = mean, half
        rows.append(row)
        if c.design == "named":
            for seed, r in zip(seeds, results_c, strict=True):
                runs.append({**head, "seed": seed, **{k: r[k] for k in RUN_KEYS}})
    return rows, runs


#: The per-seed measures kept for the named configurations.
RUN_KEYS = (
    "makespan_s",
    "job_mean_s",
    "wait_p95_s",
    "idle_while_waiting",
    "saturated_share",
    "full_share",
    "swap_share",
    "actual_peak_gib",
    "over_budget",
)


def pareto(rows: list[dict]) -> list[dict]:
    """The rows no other row beats on every objective (and is better on one), among those
    whose jobs never passed the memory budget."""
    fit = [r for r in rows if not r.get("over_budget")]

    def beats(a, b):
        return all(a[k] <= b[k] for k in OBJECTIVES) and any(a[k] < b[k] for k in OBJECTIVES)

    return [r for r in fit if not any(beats(o, r) for o in fit if o is not r)]


def study(reps: int, points: int, params: Params, slots_list=(6, 8, 16)):
    rows, runs = [], []
    with ProcessPoolExecutor() as pool:
        for slots in slots_list:
            cpus = MACHINES[slots]
            for workload in WORKLOADS.values():
                configs = named_configs() + screen_configs(slots, points)
                got, per_seed = evaluate(configs, workload, cpus, reps, params, pool)
                rows += got
                runs += per_seed
    return rows, runs


def _fmt(value, ci=None, nd=0, sign=False) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = f"{value:+.{nd}f}" if sign else f"{value:.{nd}f}"
    return text if ci is None else f"{text} ± {ci:.{nd}f}"


def _diff(r: dict, key: str, scale: float = 1.0, nd: int = 0) -> str:
    if (r["weights"], r["headroom"], r["kept"]) == CONTROL:
        return "control"
    mean, half = r.get(f"{key}_diff"), r.get(f"{key}_diff_ci")
    if mean is None:
        return ""
    text = _fmt(scale * mean, None if half is None else scale * half, nd, sign=True)
    if half is not None and abs(mean) <= half:
        text += " (none detected)"
    return text


def table(rows: list[dict], *, mark: list[dict] | None = None) -> str:
    """Rows as a Markdown table of the measures the study compares, each with its paired
    difference from the control."""
    marked = {id(r) for r in (mark or [])}
    lines = [
        "| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle "
        "while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | "
        "memory used peak GiB | over budget | untrusted |"
        + (" non-dominated |" if mark is not None else ""),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
        + ("---|" if mark is not None else ""),
    ]
    for r in rows:
        cells = [
            r["weights"],
            str(r["headroom"]),
            str(r["kept"]),
            _fmt(r["makespan_s"]),
            _diff(r, "makespan_s"),
            _fmt(r["wait_p95_s"], None, 1),
            _diff(r, "wait_p95_s", nd=1),
            _fmt(100 * r["idle_while_waiting"], None, 1),
            _diff(r, "idle_while_waiting", 100, 1),
            _fmt(100 * r["saturated_share"], None, 1),
            _diff(r, "saturated_share", 100, 1),
            _fmt(100 * r["full_share"]),
            _fmt(r["actual_peak_gib"], None, 1),
            f"{100 * r['over_budget']:.0f}% of runs" if r["over_budget"] else "no",
            "yes" if r["untrusted"] else "",
        ]
        if mark is not None:
            cells.append("yes" if id(r) in marked else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def write(out: Path, rows: list[dict], runs: list[dict], params: Params, reps: int,
          points: int) -> str:  # fmt: skip
    """`study.csv` (every configuration's means and paired differences), `runs.csv` (the named
    configurations seed by seed), `study.json` (the parameters and the non-dominated sets) and
    `study.md` (the tables)."""
    out.mkdir(parents=True, exist_ok=True)
    _csv(out / "study.csv", rows)
    _csv(out / "runs.csv", runs)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["machine_cpus"], r["workload"]), []).append(r)
    swap = f"swap term {params.swap_slowdown:.2f} a GiB" if params.swap_slowdown else "no swap term"
    parts = [
        f"# Policy study: {reps} replications a configuration, {points} screening points",
        "",
        f"Background {BACKGROUND:g} busy thread; memory 2 GiB a CPU, {OTHER_GIB:g} GiB of it "
        f"used by other work; {swap}. Means over the replications. A difference from the "
        "control (weight 4, headroom auto, 2 kept) is the mean over seeds of the configuration "
        "less the control on the same seed, ± its 95% interval; where the interval holds 0, no "
        "difference was detected.",
    ]
    front = {}
    for (cpus, name), group in sorted(groups.items()):
        best = pareto(group)
        front[f"{cpus}cpu-{name}"] = best
        named = [r for r in group if r["design"] == "named"]
        parts += [
            "",
            f"## {cpus - 2} slots ({cpus} CPUs), {name} workload",
            "",
            f"Jobs using more than {swap_limit_gib(cpus):.0f} GiB at once leave less than "
            f"{SWAP_AVAILABLE_GIB:g} GiB available, where the measured machine went into swap.",
            "",
            table(named, mark=best),
            "",
            "Non-dominated (named and screened):",
            "",
            table(sorted(best, key=lambda r: r["makespan_s"])),
        ]
    text = "\n".join(parts) + "\n"
    (out / "study.md").write_text(text)
    (out / "study.json").write_text(
        json.dumps(
            {
                "params": params.to_dict(),
                "reps": reps,
                "points": points,
                "background": BACKGROUND,
                "other_gib": OTHER_GIB,
                "full_share": FULL_SHARE,
                "pareto": {
                    k: [(r["weights"], r["headroom"], r["kept"]) for r in v]
                    for k, v in front.items()
                },
            },
            indent=1,
        )
        + "\n"
    )
    return text
