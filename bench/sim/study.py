"""The policy study: weight defaults compared on simulated machines, for the real benchmark to
choose among.

Two parts, both on every one of `WORKLOADS` and on three machines (6, 8 and 16 slots: 8, 10
and 18 CPUs with 2 kept back, 2 GiB of memory a CPU):

* `named`: today's fixed weight 4 against the ranges 2-4 and 1-4, each with the pool's
  headroom at `auto`, 0, and one more than `auto`, and against 4-8 (today's weight as the
  least) with headroom `auto`;
* `screen`: a Latin hypercube over the range's least and most, the headroom and the CPUs kept
  back, `points` configurations per machine and workload.

Every configuration runs the same `reps` plans (seeds 1 to `reps`: common random numbers, so
two configurations differ by their policy, not their luck). A configuration is discarded when,
in any replication, the memory its jobs really used passed the budget (the budget counts what
they declare or are assumed to use, which can be less). The rest are compared on four
measures, all lower-better: makespan, p95 wait, the share of slots left idle while something
waited, and the share of the time the machine had at most one CPU idle (where the measured
wakeup latency went bad, docs/bench.md); `pareto` keeps those no other configuration beats on
all four.
"""

from __future__ import annotations

import csv
import json
import math
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from bench.sim.engine import Simulation
from bench.sim.metrics import aggregate, summarize
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
OBJECTIVES = ("makespan_s", "wait_p95_s", "idle_while_waiting", "saturated_share")


@dataclass(frozen=True)
class Config:
    """One policy to try: what every job asks for, the work pool's headroom and the CPUs kept
    out of the slots."""

    weights: str
    headroom: str = "auto"
    kept: int = 2
    design: str = "named"

    def label(self) -> str:
        return f"w={self.weights} headroom={self.headroom} kept={self.kept}"


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
    sim = Simulation(setup, machine, params.kinds(mem_bytes), steps, seed=seed,
                     noise_cv=params.noise_cv)  # fmt: skip
    return summarize(sim.run(), BACKGROUND)


def named_configs() -> list[Config]:
    out = [Config("4")]
    for weights in ("2-4", "1-4"):
        out += [Config(weights, headroom) for headroom in ("auto", "0", "auto+1")]
    return [*out, Config("4-8")]


def screen_configs(slots: int, points: int, seed: int = 7) -> list[Config]:
    """`points` configurations from a Latin hypercube over (least, most, headroom, kept), each
    factor's range cut into `points` strata: least 1 to 4, most from least to the slots,
    headroom 0 to 3, kept 0 to 3."""
    if points < 1:
        return []
    from scipy.stats import qmc

    sample = qmc.LatinHypercube(d=4, seed=seed).random(points)
    out = []
    for u_least, u_most, u_head, u_kept in sample:
        kept = min(3, int(u_kept * 4))
        top = slots + 2 - kept
        least = 1 + min(3, int(u_least * 4))
        most = least + min(top - least, int(u_most * (top - least + 1)))
        headroom = min(3, int(u_head * 4))
        out.append(Config(f"{least}-{most}", str(headroom), kept, "screen"))
    return list(dict.fromkeys(out))


def evaluate(configs, workload: Workload, cpus: int, reps: int, params: Params, pool) -> list:
    work = [(c, workload, cpus, seed, params) for c in configs for seed in range(1, reps + 1)]
    results = list(pool.map(run_one, work, chunksize=8))
    rows = []
    for i, c in enumerate(configs):
        agg = aggregate(results[i * reps : (i + 1) * reps])
        rows.append(
            {
                "workload": workload.name,
                "slots": cpus - c.kept,
                "machine_cpus": cpus,
                "design": c.design,
                "weights": c.weights,
                "headroom": c.headroom,
                "kept": c.kept,
                **agg,
            }
        )
    return rows


def pareto(rows: list[dict]) -> list[dict]:
    """The rows no other row beats on every objective (and is better on one), among those
    whose jobs never passed the memory budget."""
    fit = [r for r in rows if not r.get("over_budget")]

    def beats(a, b):
        return all(a[k] <= b[k] for k in OBJECTIVES) and any(a[k] < b[k] for k in OBJECTIVES)

    return [r for r in fit if not any(beats(o, r) for o in fit if o is not r)]


def study(reps: int, points: int, params: Params, slots_list=(6, 8, 16)) -> list[dict]:
    rows = []
    with ProcessPoolExecutor() as pool:
        for slots in slots_list:
            cpus = MACHINES[slots]
            for workload in WORKLOADS.values():
                configs = named_configs() + screen_configs(slots, points)
                rows += evaluate(configs, workload, cpus, reps, params, pool)
    return rows


def _fmt(value, ci=None, nd=0) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = f"{value:.{nd}f}"
    return text if ci is None else f"{text} ± {ci:.{nd}f}"


def table(rows: list[dict], *, mark: list[dict] | None = None) -> str:
    """Rows as a Markdown table of the measures the study compares."""
    marked = {id(r) for r in (mark or [])}
    lines = [
        "| weights | headroom | kept | slots | makespan s | job mean s | wait p95 s | idle "
        "while waiting % | at most 1 CPU idle % | CPU busy % | granted | Jain | memory used peak "
        "GiB | over budget |" + (" non-dominated |" if mark is not None else ""),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|" + ("---|" if mark else ""),
    ]
    for r in rows:
        cells = [
            r["weights"],
            str(r["headroom"]),
            str(r["kept"]),
            str(r["slots"]),
            _fmt(r["makespan_s"], r["makespan_s_ci"]),
            _fmt(r["job_mean_s"], r["job_mean_s_ci"], 1),
            _fmt(r["wait_p95_s"], r["wait_p95_s_ci"], 1),
            _fmt(100 * r["idle_while_waiting"], 100 * (r["idle_while_waiting_ci"] or 0), 1),
            _fmt(100 * r["saturated_share"], 100 * (r["saturated_share_ci"] or 0), 1),
            _fmt(100 * r["cpu_util"]),
            _fmt(r["granted_mean"], None, 2),
            _fmt(r["jain_slot_s"], None, 3),
            _fmt(r["actual_peak_gib"], None, 1),
            f"{100 * r['over_budget']:.0f}% of runs" if r["over_budget"] else "no",
        ]
        if mark is not None:
            cells.append("yes" if id(r) in marked else "")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write(out: Path, rows: list[dict], params: Params, reps: int, points: int) -> str:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "study.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["machine_cpus"], r["workload"]), []).append(r)
    parts = [
        f"# Policy study: {reps} replications a configuration, {points} screening points",
        "",
        f"Background {BACKGROUND:g} busy thread; memory 2 GiB a CPU; mean ± 95% interval.",
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
                "rows": rows,
                "pareto": {
                    k: [(r["weights"], r["headroom"], r["kept"]) for r in v]
                    for k, v in front.items()
                },
            },
            indent=1,
        )
    )
    return text
