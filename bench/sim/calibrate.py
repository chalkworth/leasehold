"""Calibration: the simulator set against the benchmark's measured runs.

Each measured condition under `bench/results/<class>/raw-*/` is replayed: the plan its
`spec.json` recorded, the bytes each job hashed and touched, the weights and memory budget it
ran with, the machine's load before it started (`outer.json`) as background threads, and the
memory others were using (the machine's memory less the benchmark's limit and 2 GiB). `fit`
chooses the parameters (`Params`) that bring the simulated median run time of each job kind
closest to the measured one, over the leasehold conditions only; makespan, completion time and
wait are not fitted, and `compare` reports how far the simulated ones are from the measured.

Conditions without leasehold are compared too but not fitted: at 8 agents they swapped (one
`mem` job too many overlapped), and the simulator does not model swap.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from bench import run as bench_run
from bench.sim.engine import Overheads, Simulation
from bench.sim.metrics import aggregate, summarize
from bench.sim.model import Speedup
from bench.sim.workload import CONDITIONS, GIB, Params, Weights, measured_steps, setup_for

#: Above this makespan error a condition is not reproduced well enough to predict from.
TOLERANCE = 0.15


@dataclass
class Measured:
    """One measured condition: where it is, what it ran with, and what it measured."""

    run: str
    label: str
    condition: str
    directory: Path
    spec: dict
    summary: dict
    medians: dict[str, float]
    background: float
    overheads: tuple[float, float]
    #: Left out of the fit, to test it on runs it did not see.
    held_out: bool = False

    @property
    def leasehold(self) -> bool:
        return self.condition.startswith("lh-")

    @property
    def fitted(self) -> bool:
        return self.leasehold and not self.held_out

    @property
    def name(self) -> str:
        return f"{self.run}/{self.label}"


def discover(results: Path) -> list[Measured]:
    """Every measured condition under `results` (a machine class's directory) that the
    simulator can reproduce."""
    found = []
    for run_dir in sorted(results.glob("raw-*")):
        spec_path = run_dir / "spec.json"
        if not spec_path.exists():
            continue
        spec = json.loads(spec_path.read_text())
        for cond_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            label = cond_dir.name
            condition = label.rsplit("-r", 1)[0] if label[-3:-1] == "-r" else label
            if condition not in CONDITIONS or not (cond_dir / "outer.json").exists():
                continue
            summary = bench_run.summarize_condition(cond_dir, label)
            rows = bench_run._read_rows(cond_dir / "jobs.csv")
            ok = [r for r in rows if r["rc"] == "0" and r["ended"] not in ("", "None")]
            medians = {
                k: statistics.median(
                    [float(r["ended"]) - float(r["started"]) for r in ok if r["kind"] == k]
                    or [math.nan]
                )
                for k in ("cpu", "mem")
            }
            starts = [float(r["started"]) - float(r["issued"]) for r in ok]
            ends = [float(r["done"]) - float(r["ended"]) for r in ok]
            short = [s for s in starts if s < 1.0] or starts
            outer = json.loads((cond_dir / "outer.json").read_text())
            found.append(
                Measured(
                    run=run_dir.name.removeprefix("raw-"),
                    label=label,
                    condition=condition,
                    directory=cond_dir,
                    spec=spec,
                    summary=summary,
                    medians=medians,
                    background=float((outer.get("before") or {}).get("load1") or 1.0),
                    overheads=(statistics.median(short), statistics.median(ends)),
                )
            )
    return found


def overheads(measured: list[Measured]) -> Overheads:
    """`hold`'s start and end times, and the bare ones, as the measured runs had them."""
    lh = [m.overheads for m in measured if m.condition.startswith("lh-")]
    bare = [m.overheads for m in measured if not m.condition.startswith("lh-")]
    out = Overheads()
    if lh:
        out = replace(
            out,
            start=statistics.median(s for s, _ in lh),
            end=statistics.median(e for _, e in lh),
        )
    if bare:
        out = replace(
            out,
            start_unmanaged=statistics.median(s for s, _ in bare),
            end_unmanaged=statistics.median(e for _, e in bare),
        )
    return out


def simulate(
    m: Measured, params: Params, hold: Overheads, *, seed: int = 0, noise: bool = True
) -> dict:
    """One simulated run of the measured condition `m`."""
    spec = m.spec
    memory = int(spec["machine"]["memory_bytes"])
    cpus = int(spec["machine"]["cpus"])
    v020 = m.condition == "lh-enforced-v020"
    weights = {k: Weights.parse(str(4 if v020 else w)) for k, w in spec["weights"].items()}
    steps, mem_bytes = measured_steps(
        m.directory.parent, params, weights, declared=m.condition == "lh-enforced-declared"
    )
    setup = setup_for(m.condition, cpus=cpus, memory_bytes=memory,
                      budget=spec.get("memory_budget"))  # fmt: skip
    machine = params.machine(cpus, memory, m.background)
    other = max(0, memory - int(spec["cap_bytes"]) - 2 * GIB)
    sim = Simulation(
        setup,
        machine,
        params.kinds(mem_bytes),
        steps,
        overheads=hold,
        other_memory=other,
        seed=seed,
        noise_cv=params.noise_cv if noise else 0.0,
    )
    return summarize(sim.run(), m.background)


# --- fitting ------------------------------------------------------------------------------------

#: The fitted parameters, in the order `fit` searches them, with their bounds.
FREE = [
    ("smt_yield", 0.0, 1.0),
    ("rate_cpu", 1e8, 1e10),
    ("rate_mem", 1e8, 1e10),
    ("touch_s_per_gib", 0.05, 5.0),
    ("cpu_A", 1.0, 64.0),
    ("cpu_sigma", 0.0, 2.0),
    ("mem_A", 1.0, 64.0),
    ("mem_sigma", 0.0, 2.0),
]


def _params(vector, base: Params) -> Params:
    values = {}
    for (name, lo, hi), x in zip(FREE, vector, strict=True):
        # Search on an unbounded scale: x maps into (lo, hi) by a logistic curve.
        values[name] = lo + (hi - lo) / (1 + math.exp(-x))
    return replace(
        base,
        smt_yield=values["smt_yield"],
        rate_cpu=values["rate_cpu"],
        rate_mem=values["rate_mem"],
        touch_s_per_gib=values["touch_s_per_gib"],
        cpu=Speedup(values["cpu_A"], values["cpu_sigma"]),
        mem=Speedup(values["mem_A"], values["mem_sigma"]),
    )


def _vector(p: Params) -> list[float]:
    current = {
        "smt_yield": p.smt_yield,
        "rate_cpu": p.rate_cpu,
        "rate_mem": p.rate_mem,
        "touch_s_per_gib": p.touch_s_per_gib,
        "cpu_A": p.cpu.A,
        "cpu_sigma": p.cpu.sigma,
        "mem_A": p.mem.A,
        "mem_sigma": p.mem.sigma,
    }
    out = []
    for name, lo, hi in FREE:
        share = min(max((current[name] - lo) / (hi - lo), 1e-4), 1 - 1e-4)
        out.append(math.log(share / (1 - share)))
    return out


def _residuals(args) -> list[float]:
    m, params, hold = args
    got = simulate(m, params, hold, noise=False)
    out = []
    for kind in ("cpu", "mem"):
        want = m.medians[kind]
        if want == want and want > 0:  # not NaN
            out.append(math.log(got[f"run_median_{kind}_s"] / want))
    return out


def loss(params: Params, measured: list[Measured], hold: Overheads, pool=None) -> float:
    """The mean squared log error of the simulated median run times against the measured, over
    the fitted conditions."""
    work = [(m, params, hold) for m in measured if m.fitted]
    results = pool.map(_residuals, work) if pool is not None else map(_residuals, work)
    errors = [e for r in results for e in r]
    return sum(e * e for e in errors) / max(1, len(errors))


def fit(measured: list[Measured], hold: Overheads, start: Params | None = None,
        iterations: int = 600) -> tuple[Params, float]:  # fmt: skip
    """The parameters that bring the simulated median run times closest to the measured
    (Nelder-Mead on `loss`), and the loss they reach; then the spread of the measured run
    times as `noise_cv`."""
    from scipy.optimize import minimize

    base = start or Params()
    with ProcessPoolExecutor() as pool:
        found = minimize(
            lambda x: loss(_params(x, base), measured, hold, pool),
            _vector(base),
            method="Nelder-Mead",
            options={"maxfev": iterations, "xatol": 1e-3, "fatol": 1e-6, "adaptive": True},
        )
    best = _params(found.x, base)
    return replace(best, noise_cv=noise_cv(measured)), float(found.fun)


def noise_cv(measured: list[Measured]) -> float:
    """The median coefficient of variation of a kind's run times in conditions where one job
    runs at a time (weight 4 on 6 slots): how much jobs of one kind differ on their own."""
    cvs = []
    for m in measured:
        if not m.leasehold or "greedy" in m.condition:
            continue
        if any(int(w) < 4 for w in m.spec["weights"].values()):
            continue
        rows = bench_run._read_rows(m.directory / "jobs.csv")
        for kind in ("cpu", "mem"):
            xs = [
                float(r["ended"]) - float(r["started"])
                for r in rows
                if r["kind"] == kind and r["rc"] == "0" and r["ended"] not in ("", "None")
            ]
            if len(xs) > 3:
                cvs.append(statistics.stdev(xs) / statistics.fmean(xs))
    return statistics.median(cvs) if cvs else 0.04


# --- comparing ----------------------------------------------------------------------------------

METRICS = ["makespan_s", "job_mean_s", "job_p95_s", "wait_mean_s", "wait_p95_s"]


def _replicate(args) -> dict:
    m, params, hold, seed = args
    return simulate(m, params, hold, seed=seed)


def compare(measured: list[Measured], params: Params, hold: Overheads, reps: int = 10) -> list:
    """Per measured condition, each metric measured and simulated (mean of `reps` runs with
    the job-to-job spread, and its 95% interval), and the relative error."""
    work = [(m, params, hold, seed) for m in measured for seed in range(reps)]
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_replicate, work, chunksize=4))
    rows = []
    for i, m in enumerate(measured):
        agg = aggregate(results[i * reps : (i + 1) * reps])
        row = {
            "run": m.run,
            "condition": m.label,
            "leasehold": m.leasehold,
            "fitted": m.fitted,
            "background": m.background,
            "failed": m.summary.get("failed"),
            "swapin_mib": m.summary.get("swapin_mib"),
        }
        for kind in ("cpu", "mem"):
            row[f"run_{kind}_measured"] = m.medians[kind]
            row[f"run_{kind}_sim"] = agg[f"run_median_{kind}_s"]
        for key in METRICS:
            want, got = m.summary.get(key), agg.get(key)
            row[f"{key}_measured"] = want
            row[f"{key}_sim"] = got
            row[f"{key}_ci"] = agg.get(f"{key}_ci")
            row[f"{key}_error"] = (got - want) / want if want and got is not None else None
        rows.append(row)
    return rows


def table(rows: list[dict]) -> str:
    """The calibration as a Markdown table: makespan, mean completion and mean wait, measured
    and simulated, with the makespan error."""
    head = (
        "| run | condition | fitted | makespan s (measured / sim) | error | job mean s "
        "(measured / sim) | wait mean s (measured / sim) | swap-in MiB |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for r in rows:
        err = r["makespan_s_error"]
        flag = "" if err is None or abs(err) <= TOLERANCE else " (over 15%)"
        lines.append(
            f"| {r['run']} | {r['condition']} | {_role(r)} | "
            f"{r['makespan_s_measured']:.0f} / {r['makespan_s_sim']:.0f} | "
            f"{100 * err:+.0f}%{flag} | "
            f"{r['job_mean_s_measured']:.1f} / {r['job_mean_s_sim']:.1f} | "
            f"{r['wait_mean_s_measured']:.1f} / {r['wait_mean_s_sim']:.1f} | "
            f"{r['swapin_mib']:.0f} |"
        )
    return "\n".join(lines)


def _role(row: dict) -> str:
    if row["fitted"]:
        return "yes"
    return "held out" if row["leasehold"] else "no"


def write(out: Path, rows: list[dict], params: Params, hold: Overheads, fit_loss: float | None):
    out.mkdir(parents=True, exist_ok=True)
    with (out / "calibration.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "calibration.json").write_text(
        json.dumps(
            {
                "params": params.to_dict(),
                "overheads": hold.__dict__,
                "fit_loss": fit_loss,
                "tolerance": TOLERANCE,
                "rows": rows,
            },
            indent=1,
        )
    )
    (out / "calibration.md").write_text(table(rows) + "\n")
