"""A wide screen of memory policies on `bench.sim.memory`: admission rules, responses to an
overflow, and budgets, each run over the study's workloads under the recalibrated physics and
under changes to the model's weakest terms.

    uv run python -m bench.sim.memexplore calibrate               # the newer measured cases
    uv run python -m bench.sim.memexplore run --out OUT [--policies A,B] [--conditions C,D]
    uv run python -m bench.sim.memexplore report OUT [--condition C]

`run` writes one row per run to `OUT/runs.csv`. A condition is a change to the workload or the
physics applied at every point (`CONDITIONS`). `report` ranks policies per condition by the
owner's objective: OOM kills per heavy job within 2e-4 (pooled, with a 95% upper bound), then
makespan against weight 4 on the same point and seed, then swap written at 2% of throughput per
GiB (an SSD). A requeue (the net killing a job to run it again) is not an OOM kill. It is
counted, and the time it loses is in the makespan.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import fields, replace
from pathlib import Path

from bench.sim.memory import Physics, Policy, Result, Workload, run
from bench.sim.memstudy import latin_hypercube, point_workload, upper95

#: The physics the newer measured cases call for: pages read back from swap keep their slot
#: until the job ends, and a resumed job reads its pages back at 0.5 GiB/s (`calibrate`).
CAL = Physics(sticky_slots=True, page_in_gib_s=0.5)
SSD = 0.02
THRESHOLD = 2e-4

NET = {"park": True, "park_hold": True}
REC = Policy(
    "rec",
    estimator="learned",
    budget_share=1.0,
    cold="assumed",
    quantile=0.5,
    **NET,
)


def _rec(name: str, **changes) -> Policy:
    return replace(REC, name=name, **changes)


def _w3(name: str, **changes) -> Policy:
    return Policy(name, **changes)


#: Every policy screened. `rec` is the study's recommendation (learned weighted median x 1.15,
#: budget = cap, cold kinds count weight x budget / slots, park and hold as the net).
POLICIES = [
    Policy("w4", weight=4),
    Policy("w3"),
    # Estimators, with the net and without.
    REC,
    _rec("rec-nonet", park=False, park_hold=False),
    _rec("q95", quantile=0.95, park=False, park_hold=False),
    _rec("q95-net", quantile=0.95),
    _rec("q90-bump-net", quantile=0.9, bump_on_net=True),
    _rec("q80-bump-net", quantile=0.8, bump_on_net=True),
    _rec("q50-bump-net", bump_on_net=True),
    _rec("gauss2-net", estimator="gauss", k_sd=2.0, margin=1.0),
    _rec("gauss3", estimator="gauss", k_sd=3.0, margin=1.0, park=False, park_hold=False),
    _rec("gauss1-net", estimator="gauss", k_sd=1.0, margin=1.0),
    # Chance-constrained packing (item counts are the mean; the veto decides pairs).
    _rec("cc-gauss35", estimator="mean", margin=1.0, chance="gauss", park=False, park_hold=False),
    _rec("cc-gauss35-net", estimator="mean", margin=1.0, chance="gauss"),
    _rec("cc-gauss2-net", estimator="mean", margin=1.0, chance="gauss", chance_z=2.0),
    _rec("cc-mc", estimator="mean", margin=1.0, chance="mc", park=False, park_hold=False),
    _rec("cc-mc-net", estimator="mean", margin=1.0, chance="mc"),
    # Staggering.
    _rec("rec-stagger5", stagger_s=5.0),
    _rec("rec-peakwait", peak_wait=1.0),
    _rec("rec-nonet-peakwait", peak_wait=1.0, park=False, park_hold=False),
    # Holds and swap room.
    _rec("rec-swaphold1", hold_swap_gib=1.0),
    _rec("rec-swaphold-room", hold_swap_gib=1.0, park_room=True, requeue="noroom"),
    _rec("rec-holdend", hold_resumed=True),
    _rec("rec-zswap2-swaphold", park_zswap=2.0, hold_swap_gib=1.0),
    _rec("rec-zswap3-swaphold", park_zswap=3.0, hold_swap_gib=1.0),
    _rec(
        "rec-requeue-est", park=False, requeue="always", requeue_count="estimate", bump_on_net=True
    ),
    _rec("rec-swaphold-rqyoung", hold_swap_gib=1.0, requeue="young"),
    # Responses on the recommendation's estimate.
    _rec("rec-requeue", park=False, requeue="always"),
    _rec("rec-requeue95", park=False, requeue="always", requeue_at=0.95),
    _rec("rec-park-rqnoroom", requeue="noroom"),
    _rec("rec-park-rqyoung", requeue="young"),
    _rec("rec-throttle-park", throttle=True),
    _rec("rec-brake-park", brake=True, brake_at=0.95, brake_share=0.05),
    _rec("rec-zswap2", park_zswap=2.0),
    _rec("rec-zswap3", park_zswap=3.0),
    _rec("rec-b90", budget_share=0.9),
    # The same responses behind a count that is often wrong (weight 3), where the net acts.
    _w3("w3-net", **NET),
    _w3("w3-net-swaphold1", hold_swap_gib=1.0, **NET),
    _w3("w3-net-holdend", hold_resumed=True, **NET),
    _w3("w3-zswap2-swaphold", park_zswap=2.0, hold_swap_gib=1.0, **NET),
    _w3("w3-zswap3-swaphold", park_zswap=3.0, hold_swap_gib=1.0, **NET),
    _w3("w3-requeue", park_hold=True, requeue="always"),
    _w3("w3-requeue95", park_hold=True, requeue="always", requeue_at=0.95),
    _w3("w3-net-rqnoroom", requeue="noroom", **NET),
    _w3("w3-net-rqyoung", requeue="young", **NET),
    _w3("w3-net-swaphold-rqyoung", hold_swap_gib=1.0, requeue="young", **NET),
    _w3("w3-throttle-net", throttle=True, **NET),
    _w3("w3-throttle", throttle=True, park_hold=True),
    _w3("w3-brake-net", brake=True, brake_at=0.95, brake_share=0.05, **NET),
    _w3("w3-zswap1", park_zswap=1.0, **NET),
    _w3("w3-zswap2", park_zswap=2.0, **NET),
    _w3("w3-zswap3", park_zswap=3.0, **NET),
]

#: Changes applied at every point: ("workload" | "physics", changes).
CONDITIONS = {
    "cal": ("physics", {}),
    "old-physics": ("physics", {"sticky_slots": False, "page_in_gib_s": 0.3}),
    "weak-park": ("physics", {"reclaim_share": 0.6, "reclaim_gib_s": 0.7, "detect_s": 1.0}),
    "fast-growth": ("physics", {"swap_out_gib_s": 1.5, "slot_growth_gib_s": 0.6}),
    "tail": ("workload", {"tail_share": 0.05, "tail_alpha": 2.0}),
    "drift": ("workload", {"drift": 0.3, "drift_at": 0.4}),
    "wide": ("workload", {"size_cv": 0.6}),
}


def _one(args) -> dict:
    condition, point, policy, seed = args
    kind, changes = CONDITIONS[condition]
    wl: Workload = point_workload(point)
    ph = CAL
    if kind == "workload":
        wl = replace(wl, **changes)
    else:
        ph = replace(CAL, **changes)
    res: Result = run(policy, wl, ph, seed)
    row = {"condition": condition, "point": point["point"], "policy": policy.name, "seed": seed}
    row.update({f.name: getattr(res, f.name) for f in fields(res) if f.name != "notes"})
    return row


def sweep(conditions, policies, points, reps, jobs, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "points.json").write_text(json.dumps(points, indent=1))
    tasks = [
        (c, pt, pol, 9000 + r)
        for c in conditions
        for pt in points
        for pol in policies
        for r in range(reps)
    ]
    path = out / "runs.csv"
    new = not path.exists()
    with path.open("a", newline="") as f, ProcessPoolExecutor(jobs) as pool:
        writer = None
        for i, row in enumerate(pool.map(_one, tasks, chunksize=8)):
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=list(row))
                if new:
                    writer.writeheader()
            writer.writerow(row)
            if i % 2000 == 0:
                print(f"{i}/{len(tasks)}", file=sys.stderr)


def summarise(rows: list[dict]) -> dict:
    """Per (condition, policy): pooled kills per heavy job and its bound, makespan against w4 on
    the same (point, seed), swap GiB per run, nets per run, requeues per run."""
    base = {
        (r["condition"], r["point"], r["seed"]): float(r["makespan"])
        for r in rows
        if r["policy"] == "w4"
    }
    by: dict = defaultdict(list)
    for r in rows:
        by[(r["condition"], r["policy"])].append(r)
    out = {}
    for key, rs in by.items():
        kills = sum(int(r["kills"]) for r in rs)
        mem = sum(int(r["mem_jobs"]) - int(r["requeues"]) for r in rs)
        ratios = [
            float(r["makespan"]) / base[(r["condition"], r["point"], r["seed"])]
            for r in rs
            if (r["condition"], r["point"], r["seed"]) in base
        ]
        ratio = statistics.mean(ratios) if ratios else float("nan")
        swap = statistics.mean(float(r["swap_written_gib"]) for r in rs)
        out[key] = {
            "runs": len(rs),
            "kills": kills,
            "mem_jobs": mem,
            "rate": kills / mem if mem else 0.0,
            "up95": upper95(kills, mem),
            "ratio": ratio,
            "swap": swap,
            "score": ratio + SSD * swap * ratio,
            "parks": statistics.mean(int(r["parks"]) for r in rs),
            "requeues": statistics.mean(int(r["requeues"]) for r in rs),
            "throttles": statistics.mean(int(r["throttles"]) + int(r["brakes"]) for r in rs),
        }
    return out


def report(out: Path, only: str | None) -> None:
    with (out / "runs.csv").open() as f:
        rows = list(csv.DictReader(f))
    table = summarise(rows)
    (out / "summary.json").write_text(json.dumps({f"{c}|{p}": v for (c, p), v in table.items()}))
    for cond in dict.fromkeys(c for c, _ in table):
        if only and cond != only:
            continue
        print(f"\n== {cond}")
        print(
            f"{'policy':26s} {'kills':>5s} {'mem':>6s} {'rate':>8s} {'up95':>8s} {'ratio':>6s}"
            f" {'swap':>5s} {'SSD':>6s} {'parks':>5s} {'rq':>5s} {'thr':>5s}"
        )
        cells = [(p, v) for (c, p), v in table.items() if c == cond]
        cells.sort(key=lambda pv: (pv[1]["rate"] > THRESHOLD, pv[1]["score"]))
        for p, v in cells:
            print(
                f"{p:26s} {v['kills']:5d} {v['mem_jobs']:6d} {v['rate']:8.1e} {v['up95']:8.1e} "
                f"{v['ratio']:6.3f} {v['swap']:5.1f} {v['score']:6.3f} {v['parks']:5.2f} "
                f"{v['requeues']:5.2f} {v['throttles']:5.2f}"
            )


# --- the newer measured cases -------------------------------------------------------------------

#: The newer bench cases (6 rounds each), as the coordinator reported them. Plan seeds are not
#: known, so `calibrate` averages over many seeds. The underestimate case is taken to have half
#: its jobs heavy (~150 of ~300).
CASES = {
    "underestimate": (
        Workload(cap_gib=7.0, swap_gib=2.0, mem_share=3.75 / 7.0, p_mem=0.5),
        {
            "no net": (
                Policy("u", estimator="declared", declared_pad_gib=-0.5, budget_share=1.0),
                "38 kills",
            ),
            "park net": (
                Policy(
                    "u-net",
                    estimator="declared",
                    declared_pad_gib=-0.5,
                    budget_share=1.0,
                    **NET,
                ),
                "17 kills, ~12 GiB parked per round",
            ),
        },
    ),
    "overcommit-4g": (
        Workload(cap_gib=6.0, swap_gib=4.0, mem_share=4.25 / 6.0),
        {
            "no net": (Policy("w3"), "24 kills"),
            "park net": (Policy("w3-net", **NET), "0 kills, 28 parks, 0.915 x w4"),
        },
    ),
}


def _case(args) -> tuple:
    name, arm, policy, wl, ph, seed = args
    return name, arm, seed, run(policy, wl, ph, seed), run(Policy("w4", weight=4), wl, ph, seed)


def calibrate(physics: dict[str, Physics], seeds: int, jobs: int) -> None:
    """Simulated per 6 rounds beside the measured figure, for each physics in `physics`."""
    for label, ph in physics.items():
        tasks = [
            (name, arm, pol, wl, ph, 3500 + s)
            for name, (wl, arms) in CASES.items()
            for arm, (pol, _) in arms.items()
            for s in range(seeds)
        ]
        res: dict = defaultdict(list)
        with ProcessPoolExecutor(jobs) as pool:
            for name, arm, _, r, w4 in pool.map(_case, tasks, chunksize=4):
                res[(name, arm)].append((r, w4))
        print(f"== {label}")
        for (name, arm), rs in res.items():
            k = 6 * sum(r.kills for r, _ in rs) / seeds
            parks = 6 * sum(r.parks for r, _ in rs) / seeds
            mem = 6 * sum(r.mem_jobs for r, _ in rs) / seeds
            swap = statistics.mean(r.swap_written_gib for r, _ in rs)
            ratio = statistics.mean(r.makespan / w.makespan for r, w in rs)
            print(
                f"  {name:14s} {arm:9s} kills {k:5.1f}  parks {parks:5.1f}  mem jobs {mem:4.0f}"
                f"  swap/round {swap:5.1f}  x w4 {ratio:.3f}   measured: {CASES[name][1][arm][1]}"
            )


#: The responses `cases` tries behind each case's own (wrong) count.
CASE_NETS = {
    "none": {},
    "park+hold": NET,
    "park+hold-until-end": {**NET, "hold_resumed": True},
    "park+swaphold1": {**NET, "hold_swap_gib": 1.0},
    "park+swaphold-room-rq": {
        **NET,
        "hold_swap_gib": 1.0,
        "park_room": True,
        "requeue": "noroom",
    },
    "park+rq-noroom": {**NET, "requeue": "noroom"},
    "park+rq-young": {**NET, "requeue": "young"},
    "park+rq-young+swaphold": {**NET, "requeue": "young", "hold_swap_gib": 1.0},
    "requeue": {"park_hold": True, "requeue": "always"},
    "requeue@95": {"park_hold": True, "requeue": "always", "requeue_at": 0.95},
    "throttle": {"park_hold": True, "throttle": True},
    "throttle+park": {**NET, "throttle": True},
    "throttle+park+swaphold": {**NET, "throttle": True, "hold_swap_gib": 1.0},
    "brake95+park": {**NET, "brake": True, "brake_at": 0.95, "brake_share": 0.05},
    "zswap1": {**NET, "park_zswap": 1.0},
    "zswap2": {**NET, "park_zswap": 2.0},
    "zswap3": {**NET, "park_zswap": 3.0},
    "zswap2+swaphold": {**NET, "park_zswap": 2.0, "hold_swap_gib": 1.0},
    "zswap3+swaphold": {**NET, "park_zswap": 3.0, "hold_swap_gib": 1.0},
}


def cases(physics: dict[str, Physics], nets: list[str], seeds: int, jobs: int) -> None:
    """Each net in `nets` behind each measured case's count, per 6 rounds."""
    for label, ph in physics.items():
        tasks = []
        for name, (wl, arms) in CASES.items():
            base = arms["no net"][0]
            for net in nets:
                pol = replace(base, name=net, **CASE_NETS[net])
                tasks += [(name, net, pol, wl, ph, 3500 + s) for s in range(seeds)]
        res: dict = defaultdict(list)
        with ProcessPoolExecutor(jobs) as pool:
            for name, net, _, r, w4 in pool.map(_case, tasks, chunksize=4):
                res[(name, net)].append((r, w4))
        print(f"== {label}  (per 6 rounds, {seeds} seeds)")
        for (name, net), rs in res.items():
            per = 6 / seeds
            print(
                f"  {name:14s} {net:24s} kills {per * sum(r.kills for r, _ in rs):5.1f}"
                f"  parks {per * sum(r.parks for r, _ in rs):5.1f}"
                f"  rq {per * sum(r.requeues for r, _ in rs):5.1f}"
                f"  thr {per * sum(r.throttles + r.brakes for r, _ in rs):5.1f}"
                f"  swap/round {statistics.mean(r.swap_written_gib for r, _ in rs):5.1f}"
                f"  x w4 {statistics.mean(r.makespan / w.makespan for r, w in rs):.3f}"
            )


#: The `zswap` grid: parking to disk, to zswap at each ratio, and to zswap with a requeue when
#: the compressed job would not fit, all behind weight 3 with the swap-aware hold.
ZSWAP_NETS = {
    "disk": {},
    "requeue": {"park": False, "requeue": "always"},
    **{f"z{r:g}": {"park_zswap": r} for r in (1.0, 1.5, 2.0, 3.0)},
    **{f"z{r:g}-rq": {"park_zswap": r, "requeue": "noroom"} for r in (1.5, 2.0, 3.0)},
}


def _zswap_one(args) -> tuple:
    share, swap, pool, net, seed = args
    wl = Workload(cap_gib=7.0, swap_gib=swap, mem_share=share, size_cv=0.15, p_mem=0.35, retry=True)
    ph = replace(CAL, zswap_pool_gib=pool)
    pol = Policy(net, **{**NET, "hold_swap_gib": 1.0, **ZSWAP_NETS[net]})
    w4 = run(Policy("w4", weight=4), wl, ph, seed)
    return (share, swap, pool, net), run(pol, wl, ph, seed), w4


def zswap(seeds: int, jobs: int) -> None:
    """Where parking to zswap beats parking to disk, by job size, swap and pool size."""
    tasks = [
        (share, swap, pool, net, 9000 + s)
        for share in (0.4, 0.5, 0.6, 0.7, 0.8)
        for swap in (2.0, 4.0)
        for pool in (1.6, 3.2)
        for net in ZSWAP_NETS
        for s in range(seeds)
    ]
    res: dict = defaultdict(list)
    with ProcessPoolExecutor(jobs) as pool:
        for key, r, w4 in pool.map(_zswap_one, tasks, chunksize=4):
            res[key].append((r, w4))
    print("share swap pool net        kills/heavy   x w4   swap   SSD    parks  rq")
    for (share, swap, pool, net), rs in res.items():
        mem = sum(r.mem_jobs - r.requeues for r, _ in rs)
        kills = sum(r.kills for r, _ in rs)
        ratio = statistics.mean(r.makespan / w.makespan for r, w in rs)
        gib = statistics.mean(r.swap_written_gib for r, _ in rs)
        print(
            f"{share:5.2f} {swap:4.1f} {pool:4.1f} {net:8s} {kills:4d}/{mem:<6d} {ratio:6.3f} "
            f"{gib:6.1f} {ratio * (1 + SSD * gib):6.3f} "
            f"{statistics.mean(r.parks for r, _ in rs):5.2f} "
            f"{statistics.mean(r.requeues for r, _ in rs):5.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=8)
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate", help="the newer measured cases")
    c.add_argument("--seeds", type=int, default=30)
    k = sub.add_parser("cases", help="nets behind the measured cases' counts")
    k.add_argument("--seeds", type=int, default=60)
    k.add_argument("--nets", default=",".join(CASE_NETS))
    k.add_argument("--conditions", default="cal")
    z = sub.add_parser("zswap", help="parking to zswap against parking to disk")
    z.add_argument("--seeds", type=int, default=20)
    r = sub.add_parser("run", help="policies over the workloads, per condition")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--policies", default="")
    r.add_argument("--conditions", default="cal")
    r.add_argument("--points", type=int, default=56)
    r.add_argument("--reps", type=int, default=10)
    r.add_argument("--seed", type=int, default=20261024)
    p = sub.add_parser("report", help="rank the policies of a run")
    p.add_argument("out", type=Path)
    p.add_argument("--condition")
    args = parser.parse_args(argv)
    if args.cmd == "calibrate":
        old = Physics()
        calibrate(
            {"old physics": old, "sticky slots": replace(old, sticky_slots=True), "cal": CAL},
            args.seeds,
            args.jobs,
        )
    elif args.cmd == "cases":
        phys = {}
        for cond in args.conditions.split(","):
            kind, changes = CONDITIONS[cond]
            assert kind == "physics", "cases varies the physics only"
            phys[cond] = replace(CAL, **changes)
        cases(phys, args.nets.split(","), args.seeds, args.jobs)
    elif args.cmd == "zswap":
        zswap(args.seeds, args.jobs)
    elif args.cmd == "run":
        wanted = set(args.policies.split(",")) - {""}
        pols = [p for p in POLICIES if not wanted or p.name in wanted or p.name == "w4"]
        conds = [c for c in args.conditions.split(",") if c]
        points = latin_hypercube(args.points, args.seed)
        sweep(conds, pols, points, args.reps, args.jobs, args.out)
    elif args.cmd == "report":
        report(args.out, args.condition)
    return 0


if __name__ == "__main__":
    sys.exit(main())
