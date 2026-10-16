"""The policy simulator's command line, run from the repository's root after
`uv sync --group bench`, which installs SimPy.

    uv run python -m bench.sim run --condition lh-enforced --weights 2-4 --reps 30
    uv run python -m bench.sim run --from-run bench/results/8cpu-16g/raw-k8-d240 \\
        --condition lh-enforced --reps 10
    uv run python -m bench.sim calibrate [--fit]     # against bench/results/8cpu-16g
    uv run python -m bench.sim study --reps 30        # the policy study (a few minutes)

`--params` takes the parameters from a calibration's JSON; `--no-swap` leaves the swap term out.

`run` writes `runs.csv` (one row per replication), `summary.json` (means and 95% intervals) and
`timeline.csv` (the first replication's slots, line and memory over time) to `--out`, and prints
a short table. docs/sim.md describes the model, the calibration and what it leaves out.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from bench.sim import calibrate, study
from bench.sim.engine import Simulation, Unsupported
from bench.sim.metrics import aggregate, summarize, timeline
from bench.sim.workload import (
    CONDITIONS,
    GIB,
    WORKLOADS,
    Params,
    Weights,
    headroom_value,
    measured_steps,
    plan_steps,
    replay_steps,
    setup_for,
)

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results" / "8cpu-16g"


def _params(path: Path | None, no_swap: bool = False) -> Params:
    params = Params()
    if path is not None:
        raw = json.loads(path.read_text())
        params = Params.from_dict(raw.get("params", raw))
    return replace(params, swap_slowdown=0.0) if no_swap else params


def _one(args) -> tuple[dict, list[dict]]:
    a, params, seed = args
    cpus, memory = a.cpus, int(a.memory_gib * GIB)
    slots = cpus - a.kept
    weights = Weights.parse(a.weights)
    machine = params.machine(cpus, memory, a.background)
    if a.from_run is not None:
        spec = json.loads((a.from_run / "spec.json").read_text())
        memory = int(spec["machine"]["memory_bytes"])
        cpus = int(spec["machine"]["cpus"])
        machine = params.machine(cpus, memory, a.background)
        by_kind = {k: weights for k in ("cpu", "mem")}
        if a.replay is not None:
            run = json.loads((a.replay.parent / "run.json").read_text())
            steps = replay_steps(a.replay, spec, params, by_kind, run["start"])
            mem_bytes = int(spec["mem_bytes"])
        else:
            steps, mem_bytes = measured_steps(
                a.from_run, params, by_kind, declared=a.condition == "lh-enforced-declared"
            )
    else:
        workload = replace(WORKLOADS[a.workload], agents=a.agents, duration_s=a.duration)
        steps, mem_bytes = plan_steps(workload, seed, params, machine, weights)
    setup = setup_for(
        a.condition,
        cpus=cpus,
        memory_bytes=memory,
        kept=a.kept,
        headroom=headroom_value(a.headroom, slots),
        budget=a.memory_budget,
        guard=not a.no_guard,
    )
    sim = Simulation(setup, machine, params.kinds(mem_bytes), steps, seed=seed,
                     noise_cv=params.noise_cv, other_memory=int(a.other_gib * GIB),
                     pressure=params.pressure())  # fmt: skip
    result = sim.run()
    return summarize(result, a.background), timeline(result) if seed == a.seed else []


def cmd_run(a) -> int:
    params = _params(a.params, a.no_swap)
    seeds = range(a.seed, a.seed + a.reps)
    try:
        with ProcessPoolExecutor() as pool:
            done = list(pool.map(_one, [(a, params, s) for s in seeds]))
    except Unsupported as exc:
        print(f"skipped: {exc}")
        return 0
    rows = [r for r, _ in done]
    agg = aggregate(rows)
    a.out.mkdir(parents=True, exist_ok=True)
    _write_csv(a.out / "runs.csv", rows)
    _write_csv(a.out / "timeline.csv", done[0][1])
    given = {k: str(v) for k, v in vars(a).items() if k != "func"}
    record = {"args": given, "params": params.to_dict(), "summary": agg}
    (a.out / "summary.json").write_text(json.dumps(record, indent=1))
    print(f"{a.condition}, weights {a.weights}, {a.reps} replications (mean ± 95% interval)")
    for key in (
        "makespan_s", "job_mean_s", "job_p95_s", "wait_mean_s", "wait_p95_s", "slot_util",
        "cpu_util", "idle_while_waiting", "saturated_share", "jain_slot_s", "wait_ratio",
        "granted_mean", "counted_peak_gib", "actual_peak_gib", "over_budget",
    ):  # fmt: skip
        value, ci = agg.get(key), agg.get(f"{key}_ci")
        if value is None:
            continue
        print(f"  {key:20s} {value:10.3f}" + (f" ± {ci:.3f}" if ci is not None else ""))
    print(f"results in {a.out}")
    return 0


def cmd_calibrate(a) -> int:
    measured = calibrate.discover(a.results)
    if not measured:
        print(f"no measured runs under {a.results}", file=sys.stderr)
        return 1
    if a.fit_on:
        runs = set(a.fit_on.split(","))
        for m in measured:
            m.held_out = m.run not in runs
    hold = calibrate.overheads(measured)
    params = _params(a.params, a.no_swap)
    found = None
    if a.fit:
        found = calibrate.fit(measured, hold, params, a.iterations)
        params = found.params
        print(json.dumps(found.to_dict(), indent=1))
    rows = calibrate.compare(measured, replace(params, swap_slowdown=0.0), hold, a.reps)
    if params.swap_slowdown > 0:
        rows += calibrate.compare(measured, params, hold, a.reps)
    calibrate.write(a.out, rows, params, hold, found)
    print(calibrate.table(rows))
    print()
    print(calibrate.reversal(rows))
    lh = [r for r in rows if r["leasehold"] and not r["swap_term"]]
    bad = [r for r in lh if abs(r["makespan_s_error"]) > calibrate.TOLERANCE]
    print(f"\n{len(bad)} of {len(lh)} leasehold conditions are more than "
          f"{100 * calibrate.TOLERANCE:.0f}% off on makespan; results in {a.out}")  # fmt: skip
    return 0


def cmd_study(a) -> int:
    params = _params(a.params, a.no_swap)
    slots = [int(s) for s in a.slots.split(",")]
    rows, runs = study.study(a.reps, a.points, params, slots)
    print(study.write(a.out, rows, runs, params, a.reps, a.points))
    return 0


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.sim", description=__doc__.split("\n")[0])
    parser.add_argument("--params", type=Path, help="parameters from a calibration's JSON")
    parser.add_argument("--no-swap", action="store_true", help="leave the swap term out")
    sub = parser.add_subparsers(required=True)

    run = sub.add_parser("run", help="simulate one condition, several replications")
    run.add_argument("--condition", default="lh-enforced", choices=list(CONDITIONS))
    run.add_argument("--weights", default="4", help="what each job asks: a count or MIN-MAX")
    run.add_argument("--workload", default="light", choices=list(WORKLOADS))
    run.add_argument("--agents", type=int, default=8)
    run.add_argument("--duration", type=float, default=240.0, help="planned seconds per agent")
    run.add_argument("--cpus", type=int, default=8)
    run.add_argument("--memory-gib", type=float, default=16.0)
    run.add_argument("--kept", type=int, default=2, help="CPUs left out of the slots")
    run.add_argument("--headroom", default="auto", help="auto, a count, or auto+N")
    run.add_argument("--memory-budget", help="the [memory] budget, a size such as 7g")
    run.add_argument("--background", type=float, default=1.0, help="busy threads of other work")
    run.add_argument("--other-gib", type=float, default=study.OTHER_GIB,
                     help="memory other work uses")  # fmt: skip
    run.add_argument("--no-guard", action="store_true", help="leave the memory guard out")
    run.add_argument("--from-run", type=Path, help="a measured run's directory: its plan")
    run.add_argument("--replay", type=Path, help="a measured condition's jobs.csv to replay")
    run.add_argument("--reps", type=int, default=30)
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--out", type=Path, default=Path("sim-results"))
    run.set_defaults(func=cmd_run)

    cal = sub.add_parser("calibrate", help="compare with the measured runs")
    cal.add_argument("--results", type=Path, default=RESULTS)
    cal.add_argument("--fit", action="store_true", help="fit the parameters first")
    cal.add_argument("--iterations", type=int, default=800)
    cal.add_argument("--fit-on", help="fit only on these runs (comma-separated, e.g. k8-d240)")
    cal.add_argument("--reps", type=int, default=10)
    cal.add_argument("--out", type=Path, default=Path("sim-results/calibration"))
    cal.set_defaults(func=cmd_calibrate)

    st = sub.add_parser("study", help="the policy study")
    st.add_argument("--reps", type=int, default=30)
    st.add_argument("--points", type=int, default=32, help="Latin hypercube points")
    st.add_argument("--slots", default="6,8,16")
    st.add_argument("--out", type=Path, default=Path("sim-results/study"))
    st.set_defaults(func=cmd_study)

    args = parser.parse_args(argv)
    if args.func is cmd_run and args.replay is not None and args.from_run is None:
        args.from_run = args.replay.parent.parent
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
