"""The memory policy study: check `bench.sim.memory` against the bench's memory runs, then
compare memory policies over a space of workloads.

    uv run python -m bench.sim.memstudy targets DIR...   # measured arms: memory_targets.json
    uv run python -m bench.sim.memstudy validate          # the model against those targets
    uv run python -m bench.sim.memstudy fit               # refit the swap terms
    uv run python -m bench.sim.memstudy sweep --points 48 --reps 10 --out OUT
    uv run python -m bench.sim.memstudy report OUT        # Pareto sets and regret
    uv run python -m bench.sim.memstudy sensitivity --policy NAME --out OUT2

`targets` reads result directories `bench/run.py` wrote (each with `spec.json` and
`summary.csv`) and keeps, per arm and round, the makespan, the OOM kills and the swap written.
`validate` replays each measured round's plan seed under the arm's policy (common random
numbers with the measurement) and prints measured beside simulated figures. The `fit` set is the
plain weight-3 arms of `net-memheavy` and `overcommit`; every other arm is held out.

`sweep` draws a maximin Latin hypercube over the workload factors (`FACTORS`), runs every policy
in `POLICIES` on each point with the same `reps` plan seeds, and writes one row per run to
`runs.csv`. Killed jobs are run again (`Workload.retry`), so a kill costs time. `report` reads
it back: per point and policy, the mean makespan, the share of runs with any OOM kill, swap
written, and seconds under memory pressure; the non-dominated policies per point; and each
policy's regret against the fastest policy that had no OOM kill at that point. Policies are
ranked by OOM kills per heavy job against `--threshold` (default 2e-4, with a 95% upper bound),
then by makespan, then by swap written, priced per GiB for an SSD and for a spinning disk.
`sensitivity` reruns one policy and its rivals with one policy, workload or physics term changed
at a time.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, fields, replace
from pathlib import Path

from bench.sim.memory import Physics, Policy, Result, Workload, run

GIB = 1 << 30
HERE = Path(__file__).resolve().parent
TARGETS = HERE / "memory_targets.json"

#: Each measured arm's policy, by the name `bench/run.py` gives its condition.
ARMS = {
    "lh-enforced_w4": Policy("w4", weight=4),
    "lh-enforced_w3": Policy("w3"),
    "lh-enforced_w3_memdeclared": Policy("w3-declared", estimator="declared"),
    "lh-enforced_w3_overflow1g": Policy("w3-swaplimit", swap_limit=True),
    "lh-enforced_w3_parkfreeze": Policy("w3-park", park=True),
    "lh-enforced_w3_parkfreeze_holdparked": Policy(
        "w3-park-hold-outside", park=True, park_hold=True, hold_outside=True
    ),
}
#: The arms the swap terms are fitted on; every other arm is held out.
FIT_SET = {("net-memheavy", "lh-enforced_w3"), ("overcommit", "lh-enforced_w3")}

LEARNED = Policy("learned", estimator="learned", cold="alone")
SETTLED = {"plateau": True, "count_settled": True}
NET = {"park": True, "park_hold": True}


def _learned(name: str, **changes) -> Policy:
    return replace(LEARNED, name=name, **changes)


POLICIES = [
    Policy("w4", weight=4),
    Policy("w3"),
    Policy("w3-declared", estimator="declared"),
    Policy("w3-park", park=True),
    Policy("w3-park-hold", **NET),
    _learned("learned-cold", cold="assumed"),
    _learned("learned"),
    _learned("learned-b90", budget_share=0.9),
    _learned("learned-b100", budget_share=1.0),
    _learned("learned-b100-m100", budget_share=1.0, margin=1.0),
    _learned("learned-b100-m105", budget_share=1.0, margin=1.05),
    _learned("learned-b100-m130", budget_share=1.0, margin=1.3),
    _learned("learned-b100-warm", budget_share=1.0, prior_runs=10),
    _learned("learned-b100-slack1", budget_share=1.0, slack_gib=1.0),
    _learned("learned-b100-settled", budget_share=1.0, **SETTLED),
    _learned("learned-b100-park-hold", budget_share=1.0, **NET),
    _learned("learned-b100-settled-park-hold", budget_share=1.0, **SETTLED, **NET),
    _learned(
        "learned-b100-settled-brake-park-hold", budget_share=1.0, brake=True, **SETTLED, **NET
    ),
    _learned(
        "learned-b100-m105-settled-park-hold", budget_share=1.0, margin=1.05, **SETTLED, **NET
    ),
    Policy("oracle-b100", estimator="oracle", budget_share=1.0),
    # Round two: a learned figure nearer the typical size, with the net catching what it misses.
    _learned("learned-b100-warm-net", budget_share=1.0, prior_runs=10, **NET),
    _learned("learned-b100-warm-q80", budget_share=1.0, prior_runs=10, quantile=0.8),
    _learned("learned-b100-warm-q80-net", budget_share=1.0, prior_runs=10, quantile=0.8, **NET),
    _learned("learned-b100-warm-q50-net", budget_share=1.0, prior_runs=10, quantile=0.5, **NET),
    _learned("learned-b100-coldnet", budget_share=1.0, cold="assumed", **NET),
    _learned("learned-b100-coldnet-q80", budget_share=1.0, cold="assumed", quantile=0.8, **NET),
    _learned("learned-b100-q80-net", budget_share=1.0, quantile=0.8, **NET),
    _learned("learned-b100-coldnet-q50", budget_share=1.0, cold="assumed", quantile=0.5, **NET),
]

#: The workload factors the sweep varies: (low, high), and whether to draw on a log scale.
FACTORS = {
    "mem_share": (0.3, 0.8, False),
    "p_mem": (0.1, 0.7, False),
    "think_s": (10.0, 60.0, True),
    "size_cv": (0.0, 0.4, False),
    "ramp": (0.03, 0.6, True),
    "swap_share": (0.3, 0.9, False),
    "late_share": (0.0, 0.5, False),
}
CAP_GIB = 7.0


# --- measured targets ---------------------------------------------------------------------------


def read_targets(dirs: list[Path]) -> dict:
    """Per experiment: its workload and plan seeds, and per arm and round the makespan, OOM
    kills and swap written, from each directory's `spec.json` and `summary.csv` (and, where the
    summary has no swap-out column, the round's `samples.csv`)."""
    out = {}
    for d in dirs:
        spec = json.loads((d / "spec.json").read_text())
        arms: dict[str, dict] = defaultdict(dict)
        with (d / "summary.csv").open() as f:
            for row in csv.DictReader(f):
                arm, rnd = row["condition"].rsplit("-r", 1)
                swap = row.get("swapout_gib")
                if swap in (None, ""):
                    swap = _swap_out(d / row["condition"] / "samples.csv")
                arms[arm][int(rnd)] = {
                    "makespan": float(row["makespan_s"]),
                    "kills": int(row["oom_kills"] or 0),
                    "swap_gib": float(swap),
                }
        out[d.name] = {
            "seed_base": spec["seed_base"],
            "mem_gib": spec["mem_bytes"] / GIB,
            "cap_gib": spec["cap_bytes"] / GIB,
            "swap_gib": spec["swap_bytes"] / GIB,
            "preset": spec["preset"],
            "agents": spec["agents"],
            "arms": arms,
        }
    return out


def _swap_out(samples: Path) -> float:
    with samples.open() as f:
        rows = list(csv.DictReader(f))
    return (int(rows[-1]["pswpout"]) - int(rows[0]["pswpout"])) * 4096 / GIB


def workload_of(exp: dict, **changes) -> Workload:
    p = exp["preset"]
    return Workload(
        agents=exp["agents"],
        duration_s=p["duration_s"],
        think_s=p["think_s"],
        p_mem=p["p_mem"],
        cap_gib=exp["cap_gib"],
        swap_gib=exp["swap_gib"],
        mem_share=exp["mem_gib"] / exp["cap_gib"],
        **changes,
    )


def _replay(args) -> tuple:
    name, arm, rnd, policy, workload, physics, seed = args
    return name, arm, rnd, run(policy, workload, physics, seed)


def replay(targets: dict, physics: Physics, only: set | None = None, jobs: int = 8) -> dict:
    """Simulated rounds for every measured arm (or those in `only`): {(exp, arm): {round:
    Result}}."""
    tasks = []
    for name, exp in targets.items():
        wl = workload_of(exp)
        for arm, rounds in exp["arms"].items():
            if arm not in ARMS or (only is not None and (name, arm) not in only):
                continue
            for rnd in rounds:
                seed = exp["seed_base"] + int(rnd)
                tasks.append((name, arm, int(rnd), ARMS[arm], wl, physics, seed))
    out: dict = defaultdict(dict)
    with ProcessPoolExecutor(jobs) as pool:
        for name, arm, rnd, res in pool.map(_replay, tasks, chunksize=2):
            out[(name, arm)][rnd] = res
    return out


def compare(targets: dict, sims: dict) -> list[dict]:
    """Measured beside simulated, per arm: makespan ratio to weight 4 (paired by round), OOM
    kills in all rounds, swap GiB written per round."""
    rows = []
    for name, exp in targets.items():
        base = exp["arms"].get("lh-enforced_w4", {})
        sim_base = sims.get((name, "lh-enforced_w4"), {})
        for arm, rounds in exp["arms"].items():
            sim = sims.get((name, arm))
            if not sim:
                continue
            rs = sorted(set(map(int, rounds)) & set(sim))
            m = [rounds[str(r)] if str(r) in rounds else rounds[r] for r in rs]
            ratio = [x["makespan"] / _get(base, r)["makespan"] for x, r in zip(m, rs, strict=True)]
            sim_ratio = [sim[r].makespan / sim_base[r].makespan for r in rs if r in sim_base]
            rows.append(
                {
                    "experiment": name,
                    "arm": arm,
                    "fit": (name, arm) in FIT_SET,
                    "rounds": len(rs),
                    "makespan": statistics.mean(x["makespan"] for x in m),
                    "sim_makespan": statistics.mean(sim[r].makespan for r in rs),
                    "ratio": statistics.mean(ratio),
                    "sim_ratio": statistics.mean(sim_ratio) if sim_ratio else math.nan,
                    "kills": sum(x["kills"] for x in m),
                    "sim_kills": sum(sim[r].kills for r in rs),
                    "swap_gib": statistics.mean(x["swap_gib"] for x in m),
                    "sim_swap_gib": statistics.mean(sim[r].swap_written_gib for r in rs),
                }
            )
    return rows


def _get(d: dict, r: int):
    return d[str(r)] if str(r) in d else d[r]


def loss(rows: list[dict]) -> float:
    """How far the fit set is from its measurements: kills, swap and makespan ratio, each
    relative to the measured scale."""
    total = 0.0
    for row in rows:
        if not row["fit"]:
            continue
        total += ((row["sim_kills"] - row["kills"]) / max(5.0, row["kills"])) ** 2
        total += ((row["sim_swap_gib"] - row["swap_gib"]) / max(2.0, row["swap_gib"])) ** 2
        total += ((row["sim_ratio"] - row["ratio"]) / 0.02) ** 2
    return total


def print_compare(rows: list[dict]) -> None:
    print(
        f"{'experiment':15s} {'arm':38s} fit  ratio(m/s)     kills(m/s)  swapGiB/round(m/s)"
        "  makespan(m/s)"
    )
    for r in rows:
        print(
            f"{r['experiment']:15s} {r['arm']:38s} {'*' if r['fit'] else ' '}  "
            f"{r['ratio']:.3f}/{r['sim_ratio']:.3f}   {r['kills']:4d}/{r['sim_kills']:<4d}"
            f"   {r['swap_gib']:5.1f}/{r['sim_swap_gib']:<5.1f}"
            f"        {r['makespan']:4.0f}/{r['sim_makespan']:<4.0f}"
        )


def load_targets(path: Path = TARGETS) -> dict:
    return json.loads(path.read_text())


def load_physics(path: Path | None) -> Physics:
    if path is None:
        return Physics()
    return Physics(**json.loads(path.read_text()))


def fit(targets: dict, grid: dict[str, list[float]], base: Physics, jobs: int) -> list:
    """Every combination of `grid`'s values, scored by `loss` on the fit set; best first."""
    keys = list(grid)
    combos = [{}]
    for k in keys:
        combos = [{**c, k: v} for c in combos for v in grid[k]]
    scored = []
    for c in combos:
        ph = replace(base, **c)
        rows = compare(targets, replay(targets, ph, FIT_SET | _w4(targets), jobs))
        scored.append((loss(rows), c))
        print(f"{loss(rows):9.2f} {c}", file=sys.stderr)
    return sorted(scored, key=lambda x: x[0])


def _w4(targets: dict) -> set:
    return {(name, "lh-enforced_w4") for name, _ in FIT_SET}


# --- the sweep ----------------------------------------------------------------------------------


def latin_hypercube(n: int, seed: int) -> list[dict]:
    """`n` points over `FACTORS`, one per bin of each factor, the best of 200 random
    hypercubes by their smallest distance between points (maximin)."""
    rng = random.Random(seed)
    best, best_d = None, -1.0
    k = len(FACTORS)
    for _ in range(200):
        cols = []
        for _ in range(k):
            perm = list(range(n))
            rng.shuffle(perm)
            cols.append([(p + rng.random()) / n for p in perm])
        pts = list(zip(*cols, strict=True))
        d = min(math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1 :])
        if d > best_d:
            best, best_d = pts, d
    assert best is not None
    out = []
    for i, u in enumerate(best):
        point = {"point": i}
        for (name, (lo, hi, log)), x in zip(FACTORS.items(), u, strict=True):
            point[name] = (
                math.exp(math.log(lo) + x * (math.log(hi) - math.log(lo)))
                if log
                else (lo + x * (hi - lo))
            )
        out.append(point)
    return out


def point_workload(point: dict) -> Workload:
    return Workload(
        cap_gib=CAP_GIB,
        swap_gib=point["swap_share"] * CAP_GIB,
        mem_share=point["mem_share"],
        p_mem=point["p_mem"],
        think_s=point["think_s"],
        size_cv=point["size_cv"],
        ramp=point["ramp"],
        late_share=point.get("late_share", 0.0),
        outlier_share=point.get("outlier_share", 0.0),
        retry=True,
    )


def _sweep_one(args) -> dict:
    point, policy, seed, physics = args
    res: Result = run(policy, point_workload(point), physics, seed)
    row = dict(point)
    row.update(policy=policy.name, seed=seed)
    row.update({f.name: getattr(res, f.name) for f in fields(res) if f.name != "notes"})
    row["cut"] = bool(res.notes)
    return row


def sweep(
    points: list[dict],
    policies: list[Policy],
    reps: int,
    physics: Physics,
    jobs: int,
    out: Path,
    first_seed: int = 9000,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "points.json").write_text(json.dumps(points, indent=1))
    (out / "physics.json").write_text(json.dumps(asdict(physics), indent=1))
    (out / "policies.json").write_text(json.dumps([asdict(p) for p in policies], indent=1))
    seeds = range(first_seed, first_seed + reps)
    tasks = [(pt, pol, seed, physics) for pt in points for pol in policies for seed in seeds]
    with (out / "runs.csv").open("w", newline="") as f, ProcessPoolExecutor(jobs) as pool:
        writer = None
        for i, row in enumerate(pool.map(_sweep_one, tasks, chunksize=4)):
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)
            if i % 500 == 0:
                print(f"{i}/{len(tasks)}", file=sys.stderr)


# --- the report ---------------------------------------------------------------------------------

METRICS = ("makespan", "kill_rate", "swap_gib", "pressure_s")
#: Risk thresholds for the curve: OOM kills per `mem` job.
THRESHOLDS = (1e-3, 2e-4, 5e-5)
#: What one GiB of swap written per run costs, in the same units as regret (a share of
#: makespan), on each kind of disk: an assumption, stated so that it can be changed. On an SSD
#: a GiB a run costs as much as 2% more time; on a spinning disk, a tenth of that.
SWAP_COST = {"ssd": 0.02, "hdd": 0.002}


def cells(rows: list[dict]) -> dict:
    """Per (point, policy): means over its runs, kills per `mem` job, the share of runs with
    any kill, and net triggers per run."""
    by: dict = defaultdict(list)
    for r in rows:
        by[(int(r["point"]), r["policy"])].append(r)
    out = {}
    for key, rs in by.items():
        mem = sum(int(r["mem_jobs"]) for r in rs)
        kills = sum(int(r["kills"]) for r in rs)
        out[key] = {
            "n": len(rs),
            "makespan": statistics.mean(float(r["makespan"]) for r in rs),
            "kills": kills,
            "mem_jobs": mem,
            "kill_rate": kills / mem if mem else 0.0,
            "p_oom": sum(int(r["kills"]) > 0 for r in rs) / len(rs),
            "important_kills": sum(int(r["important_kills"]) for r in rs),
            "swap_gib": statistics.mean(float(r["swap_written_gib"]) for r in rs),
            "pressure_s": statistics.mean(float(r["thrash_s"]) + float(r["parked_s"]) for r in rs),
            "net": statistics.mean(int(r["parks"]) + int(r["brakes"]) for r in rs),
        }
    return out


def pareto(cands: dict[str, dict]) -> list[str]:
    """The policies no other policy beats on every metric in `METRICS` (lower is better) and
    strictly on one."""
    keep = []
    for a, x in cands.items():
        beaten = any(
            all(y[m] <= x[m] for m in METRICS) and any(y[m] < x[m] for m in METRICS)
            for b, y in cands.items()
            if b != a
        )
        if not beaten:
            keep.append(a)
    return keep


def upper95(kills: int, n: int) -> float:
    """A one-sided 95% upper bound on a rate seen as `kills` in `n` (the rule of three at 0,
    else the Wilson bound)."""
    if n == 0:
        return 1.0
    if kills == 0:
        return 3.0 / n
    z, p = 1.645, kills / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + spread) / (1 + z * z / n)


def pooled(table: dict, points: list[int], policy: str) -> dict:
    kills = sum(table[(pt, policy)]["kills"] for pt in points)
    mem = sum(table[(pt, policy)]["mem_jobs"] for pt in points)
    worst = max(table[(pt, policy)]["kill_rate"] for pt in points)
    return {
        "kills": kills,
        "mem_jobs": mem,
        "rate": kills / mem if mem else 0.0,
        "upper95": upper95(kills, mem),
        "worst_point_rate": worst,
    }


def regret(table: dict, points: list[int], policies: list[str], feasible: list[str]) -> dict:
    """Per policy, per point: its makespan over the fastest `feasible` policy's there."""
    out: dict = {p: [] for p in policies}
    for pt in points:
        best = min(table[(pt, p)]["makespan"] for p in feasible)
        for p in policies:
            out[p].append(table[(pt, p)]["makespan"] / best - 1)
    return out


def report(out: Path, threshold: float) -> None:
    """Policies ranked under the owner's objective: kills per `mem` job within `threshold`
    first (pooled over every point), then throughput (regret against the fastest such policy
    at each point), then swap written, at its SSD and HDD prices."""
    with (out / "runs.csv").open() as f:
        rows = list(csv.DictReader(f))
    points = json.loads((out / "points.json").read_text())
    table = cells(rows)
    pols = list(dict.fromkeys(r["policy"] for r in rows))
    pts = [p["point"] for p in points]
    risk = {p: pooled(table, pts, p) for p in pols}
    fronts = {pt: pareto({p: table[(pt, p)] for p in pols}) for pt in pts}

    def mean_of(p: str, key: str) -> float:
        return statistics.mean(table[(pt, p)][key] for pt in pts)

    curve = {}
    for t in THRESHOLDS:
        feasible = [p for p in pols if risk[p]["rate"] <= t] or pols
        reg = regret(table, pts, pols, feasible)
        curve[t] = {
            p: {"mean_regret": statistics.mean(reg[p]), "worst_regret": max(reg[p])}
            for p in feasible
        }
    feasible = [p for p in pols if risk[p]["rate"] <= threshold]
    reg = regret(table, pts, pols, feasible or pols)
    print(
        f"{len(pts)} points, {len(pols)} policies, {len(rows)} runs; constraint: kills per mem "
        f"job <= {threshold:g} (pooled)"
    )
    head = (
        f"{'policy':38s} {'kills':>5s} {'mem jobs':>8s} {'rate':>8s} {'up95':>8s} "
        f"{'P(run)':>6s} {'mean reg':>8s} {'worst':>6s} {'swap':>5s} {'SSD':>6s} {'HDD':>6s} "
        f"{'net':>5s} {'front':>5s}"
    )
    print(head)
    scores = {}
    for p in pols:
        swap = mean_of(p, "swap_gib")
        mreg = statistics.mean(reg[p])
        scores[p] = {k: mreg + c * swap for k, c in SWAP_COST.items()}
    order = sorted(pols, key=lambda p: (risk[p]["rate"] > threshold, scores[p]["ssd"]))
    for p in order:
        r = risk[p]
        print(
            f"{p:38s} {r['kills']:5d} {r['mem_jobs']:8d} {r['rate']:8.1e} {r['upper95']:8.1e} "
            f"{mean_of(p, 'p_oom'):6.1%} {statistics.mean(reg[p]):8.1%} {max(reg[p]):6.1%} "
            f"{mean_of(p, 'swap_gib'):5.1f} {scores[p]['ssd']:6.1%} {scores[p]['hdd']:6.1%} "
            f"{mean_of(p, 'net'):5.2f} {sum(p in fronts[pt] for pt in pts):5d}"
        )
    print("\nThe risk/throughput curve: the fastest policies within each threshold")
    for t, c in curve.items():
        best = sorted(c.items(), key=lambda kv: kv[1]["mean_regret"])[:3]
        text = ", ".join(
            f"{p} ({v['mean_regret']:.1%} mean, {v['worst_regret']:.1%} worst)" for p, v in best
        )
        print(f"  <= {t:g}: {text}")
    summary = {
        "threshold": threshold,
        "swap_cost": SWAP_COST,
        "risk": risk,
        "scores": scores,
        "regret": {p: {"mean": statistics.mean(reg[p]), "worst": max(reg[p])} for p in pols},
        "curve": {str(t): c for t, c in curve.items()},
        "cells": {f"{pt}|{p}": table[(pt, p)] for pt in pts for p in pols},
        "fronts": fronts,
    }
    (out / "report.json").write_text(json.dumps(summary, indent=1))


# --- sensitivity --------------------------------------------------------------------------------

#: One-at-a-time changes to the chosen policy's own parameters.
POLICY_VARIANTS = {
    "quantile": [0.8, 0.95],
    "margin": [1.0, 1.3],
    "budget_share": [0.85, 0.95],
    "trust_after": [1, 5],
    "cold": ["alone"],
    "park_hold": [False],
    "hold_outside": [True],
}
#: Workloads harder than the sweep's, applied to every point: wider spread, surprises, a late
#: link step.
WORKLOAD_VARIANTS = {
    "size_cv": [0.6],
    "outlier_share": [0.02, 0.1],
    "late_share": [0.5],
}
#: One-at-a-time changes to the model's least certain terms.
PHYSICS_VARIANTS = {
    "slot_dup_gib": [1.0, 3.5],
    "thrash_per_gib": [0.4, 2.0],
    "swap_out_gib_s": [0.25, 1.5],
    "page_in_gib_s": [0.15, 0.6],
    "detect_s": [1.0],
    "overflow_gib": [0.5, 2.0],
    "settle_cooldown_s": [2.0, 10.0],
    "settle_floor_gib_s": [4 / 1024, 64 / 1024],
}


def sensitivity(
    points: list[dict],
    chosen: Policy,
    rivals: list[Policy],
    reps: int,
    physics: Physics,
    jobs: int,
    out: Path,
) -> None:
    """Runs of `chosen` with each of its parameters changed alone (`POLICY_VARIANTS`), and of
    `chosen` and `rivals` under each change to the model (`PHYSICS_VARIANTS`), to `runs.csv`
    with a `variant` column."""
    tasks = []
    for r in range(reps):
        seed = 9000 + r
        for pt in points:
            for pol in [chosen, *rivals]:
                tasks.append(("base", pt, pol, seed, physics))
            for key, values in POLICY_VARIANTS.items():
                for v in values:
                    tasks.append(
                        (f"policy:{key}={v}", pt, replace(chosen, **{key: v}), seed, physics)
                    )
            for key, values in WORKLOAD_VARIANTS.items():
                for v in values:
                    for pol in [chosen, *rivals]:
                        tasks.append((f"workload:{key}={v}", {**pt, key: v}, pol, seed, physics))
            for key, values in PHYSICS_VARIANTS.items():
                for v in values:
                    for pol in [chosen, *rivals]:
                        tasks.append(
                            (f"physics:{key}={v}", pt, pol, seed, replace(physics, **{key: v}))
                        )
    out.mkdir(parents=True, exist_ok=True)
    (out / "points.json").write_text(json.dumps(points, indent=1))
    with (out / "runs.csv").open("w", newline="") as f, ProcessPoolExecutor(jobs) as pool:
        writer = None
        for row in pool.map(_variant_one, tasks, chunksize=4):
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row))
                writer.writeheader()
            writer.writerow(row)


def _variant_one(args) -> dict:
    variant, point, policy, seed, physics = args
    row = _sweep_one((point, policy, seed, physics))
    for key in WORKLOAD_VARIANTS:
        row.setdefault(key, 0.0)
    row["variant"] = variant
    return row


def print_sensitivity(out: Path, chosen: str) -> None:
    """Per variant and policy: kills per `mem` job, mean makespan against the chosen policy's
    base runs, swap written and net triggers per run."""
    with (out / "runs.csv").open() as f:
        rows = list(csv.DictReader(f))
    by: dict = defaultdict(list)
    for r in rows:
        by[(r["variant"], r["policy"])].append(r)
    base = {(r["point"], r["seed"]): float(r["makespan"]) for r in by[("base", chosen)]}
    print(
        f"{'variant':30s} {'policy':38s} {'kills':>5s} {'mem jobs':>8s} {'rate':>8s} "
        f"{'vs base':>7s} {'swap':>5s} {'net':>5s}"
    )
    for (variant, pol), rs in by.items():
        kills = sum(int(r["kills"]) for r in rs)
        mem = sum(int(r["mem_jobs"]) for r in rs)
        ratio = statistics.mean(float(r["makespan"]) / base[(r["point"], r["seed"])] for r in rs)
        swap = statistics.mean(float(r["swap_written_gib"]) for r in rs)
        net = statistics.mean(int(r["parks"]) + int(r["brakes"]) for r in rs)
        print(
            f"{variant:30s} {pol:38s} {kills:5d} {mem:8d} {kills / mem:8.1e} {ratio:7.3f} "
            f"{swap:5.1f} {net:5.2f}"
        )


# --- the command line ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--physics", type=Path, help="a Physics JSON (default: the fit)")
    parser.add_argument("--jobs", type=int, default=8)
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("targets", help="measured arms from result directories")
    t.add_argument("dirs", nargs="+", type=Path)
    t.add_argument("--out", type=Path, default=TARGETS)
    sub.add_parser("validate", help="the model against the measured arms")
    f = sub.add_parser("fit", help="refit the swap terms on the fit set")
    f.add_argument("--grid", type=json.loads, help='JSON: {"thrash_per_gib": [0.5, 1.0], ...}')
    s = sub.add_parser("sweep", help="every policy over a Latin hypercube of workloads")
    s.add_argument("--points", type=int, default=56)
    s.add_argument("--reps", type=int, default=10)
    s.add_argument("--seed", type=int, default=20261024)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--policies", default="", help="names in POLICIES, comma-separated (all)")
    s.add_argument("--first-seed", type=int, default=9000)
    v = sub.add_parser("sensitivity", help="the chosen policy's parameters and the model's")
    v.add_argument("--policy", required=True, help="a name in POLICIES")
    v.add_argument("--rivals", default="", help="names in POLICIES, comma-separated")
    v.add_argument("--points", type=int, default=16)
    v.add_argument("--reps", type=int, default=6)
    v.add_argument("--seed", type=int, default=20261025)
    v.add_argument("--out", type=Path, required=True)
    v.add_argument("--show", action="store_true", help="print a finished run's table only")
    r = sub.add_parser("report", help="Pareto sets and regret from a sweep")
    r.add_argument("out", type=Path)
    r.add_argument("--threshold", type=float, default=2e-4, help="kills per mem job")
    args = parser.parse_args(argv)
    physics = load_physics(args.physics)
    if args.cmd == "targets":
        args.out.write_text(json.dumps(read_targets(args.dirs), indent=1) + "\n")
    elif args.cmd == "validate":
        targets = load_targets()
        print_compare(compare(targets, replay(targets, physics, jobs=args.jobs)))
    elif args.cmd == "fit":
        grid = args.grid or {
            "thrash_per_gib": [0.6, 0.9, 1.2, 1.6],
            "churn_gib_s": [0.05, 0.1, 0.15],
            "slot_growth_gib_s": [0.25, 0.3, 0.4],
            "slot_dup_gib": [2.3, 2.6],
        }
        for score, c in fit(load_targets(), grid, physics, args.jobs)[:5]:
            print(f"{score:9.2f} {c}")
    elif args.cmd == "sweep":
        wanted = set(args.policies.split(",")) - {""}
        sweep(
            latin_hypercube(args.points, args.seed),
            [p for p in POLICIES if not wanted or p.name in wanted],
            args.reps,
            physics,
            args.jobs,
            args.out,
            args.first_seed,
        )
    elif args.cmd == "sensitivity":
        named = {p.name: p for p in POLICIES}
        if not args.show:
            rivals = [named[n] for n in args.rivals.split(",") if n]
            sensitivity(
                latin_hypercube(args.points, args.seed),
                named[args.policy],
                rivals,
                args.reps,
                physics,
                args.jobs,
                args.out,
            )
        print_sensitivity(args.out, args.policy)
    elif args.cmd == "report":
        report(args.out, args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
