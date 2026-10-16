"""Calibration: the simulator set against the benchmark's measured runs.

Each measured condition under `bench/results/<class>/raw-*/` is replayed: the plan its
`spec.json` recorded, the bytes each job hashed and touched, the weights and memory budget it
ran with, the machine's load before it started (`outer.json`) as background threads, and the
memory other work used (the median over the run's samples of the machine's memory less what was
available and what the benchmark used).
`fit` chooses the parameters (`Params`) that bring the simulated median run time of each job
kind closest to the measured one, over the leasehold conditions only; makespan, completion time
and wait are not fitted, and `compare` reports how far the simulated ones are from the measured.

Conditions without leasehold are compared too but not fitted: at 8 agents they swapped (one
`mem` job too many overlapped). `fit_swap` then fits the swap term (`Params.swap_slowdown` and
`swap_reserve_gib`) to the runs that swapped, with the rest held, so each comparison can be made
with swap left out and with it in.

With some runs held out (`Measured.held_out`), everything a fit uses comes from the other runs
alone: the parameters, the swap term, `hold`'s start and end times (`overheads`) and the spread
of run times (`noise_cv`).
"""

from __future__ import annotations

import csv
import gzip
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
from bench.sim.search import Found, nelder_mead
from bench.sim.workload import (
    CONDITIONS,
    GIB,
    Params,
    Weights,
    measured_steps,
    setup_for,
)

#: Above this makespan error a condition is not reproduced well enough to predict from.
TOLERANCE = 0.15
#: A run that read more than this back from swap counts as one that swapped.
SWAPPED_MIB = 1024


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
    #: The memory other work used during the run, in bytes.
    other: int
    #: Left out of the fit, to test it on runs it did not see.
    held_out: bool = False

    @property
    def leasehold(self) -> bool:
        return self.condition.startswith("lh-")

    @property
    def fitted(self) -> bool:
        return self.leasehold and not self.held_out

    @property
    def swapped(self) -> bool:
        return (self.summary.get("swapin_mib") or 0) > SWAPPED_MIB

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
            memory = int(spec["machine"]["memory_bytes"])
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
                    other=_other_memory(cond_dir, memory, int(spec["cap_bytes"])),
                )
            )
    return found


def _other_memory(cond_dir: Path, memory: int, cap: int) -> int:
    """The median, over the run's samples, of the memory neither available nor the
    benchmark's; without samples, the machine's memory less the benchmark's limit and 2 GiB."""
    path = cond_dir / "samples.csv.gz"
    if not path.exists():
        path = cond_dir / "samples.csv"
    if not path.exists():
        return max(0, memory - cap - 2 * GIB)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as f:
        used = [
            memory - int(r["mem_available"]) - int(r["bench_mem"] or 0)
            for r in csv.DictReader(f)
            if r.get("mem_available") not in (None, "", "None")
        ]
    return int(statistics.median(used)) if used else max(0, memory - cap - 2 * GIB)


def overheads(measured: list[Measured]) -> Overheads:
    """`hold`'s start and end times, and the bare ones, as the runs not held out had them."""
    seen = [m for m in measured if not m.held_out]
    lh = [m.overheads for m in seen if m.leasehold]
    bare = [m.overheads for m in seen if not m.leasehold]
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
    sim = Simulation(
        setup,
        machine,
        params.kinds(mem_bytes),
        steps,
        overheads=hold,
        other_memory=m.other,
        pressure=params.pressure(cap=int(spec["cap_bytes"])),
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


def loss(params: Params, runs: list[Measured], hold: Overheads, pool=None) -> float:
    """The mean squared log error of the simulated median run times against the measured, over
    `runs`."""
    work = [(m, params, hold) for m in runs]
    results = pool.map(_residuals, work) if pool is not None else map(_residuals, work)
    errors = [e for r in results for e in r]
    return sum(e * e for e in errors) / max(1, len(errors))


@dataclass
class Fit:
    """What `fit` found and how the search went."""

    params: Params
    #: The loss the search ended at, and the swap term's.
    loss: float
    swap_loss: float | None
    evaluations: int
    iterations: int
    converged: bool
    #: The parameters that ended within 1% of their range's end.
    at_bounds: list[str]

    def summary(self) -> dict:
        """How the search went, without the parameters."""
        return {k: v for k, v in self.__dict__.items() if k != "params"}

    def to_dict(self) -> dict:
        return {"params": self.params.to_dict(), **self.summary()}


def fit(measured: list[Measured], hold: Overheads, start: Params | None = None,
        iterations: int = 800) -> Fit:  # fmt: skip
    """The parameters that bring the simulated median run times closest to the measured, over
    the fitted conditions (Nelder-Mead on `loss`, at most `iterations` evaluations); then the
    spread of the measured run times as `noise_cv`, and the swap term (`fit_swap`)."""
    base = replace(start or Params(), swap_slowdown=0.0)
    runs = [m for m in measured if m.fitted]
    with ProcessPoolExecutor() as pool:
        found = nelder_mead(
            lambda x: loss(_params(x, base), runs, hold, pool),
            _vector(base),
            max_evaluations=iterations,
        )
    best = replace(_params(found.x, base), noise_cv=noise_cv(measured))
    swap = fit_swap(measured, replace(best, swap_slowdown=1.0), hold)
    at_bounds = []
    for (name, _, _), x in zip(FREE, found.x, strict=True):
        share = 1 / (1 + math.exp(-x))
        if share < 0.01 or share > 0.99:
            at_bounds.append(name)
    return Fit(
        params=replace(best, swap_slowdown=swap.x[0], swap_reserve_gib=swap.x[1]),
        loss=found.fun,
        swap_loss=swap.fun,
        evaluations=found.evaluations,
        iterations=found.iterations,
        converged=found.converged,
        at_bounds=at_bounds,
    )


#: The ranges `fit_swap` searches: the slowdown per GiB past the limit, and the reserve in GiB.
SWAP_BOUNDS = (("swap_slowdown", 0.0, 4.0), ("swap_reserve_gib", 0.0, 8.0))


def _swap_params(x, base: Params) -> Params:
    values = {name: lo + (hi - lo) / (1 + math.exp(-v)) for (name, lo, hi), v in
              zip(SWAP_BOUNDS, x, strict=True)}  # fmt: skip
    return replace(base, **values)


def fit_swap(measured: list[Measured], params: Params, hold: Overheads) -> Found:
    """The swap term that brings the simulated median run times closest to the measured over
    the runs not held out that swapped, every other parameter held. `Found.x` is the slowdown
    and the reserve."""
    runs = [m for m in measured if m.swapped and not m.held_out]
    with ProcessPoolExecutor() as pool:
        found = nelder_mead(
            lambda x: loss(_swap_params(x, params), runs, hold, pool),
            [-1.0, 0.0],
            step=1.0,
            max_evaluations=120,
        )
    best = _swap_params(found.x, params)
    found.x = [best.swap_slowdown, best.swap_reserve_gib]
    return found


def noise_cv(measured: list[Measured]) -> float:
    """The median coefficient of variation of a kind's run times in the runs not held out where
    one job runs at a time (weight 4 on 6 slots): how much jobs of one kind differ on their
    own."""
    cvs = []
    for m in measured:
        if m.held_out or not m.leasehold or "greedy" in m.condition:
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
            "swap_term": params.swap_slowdown > 0,
            "leasehold": m.leasehold,
            "fitted": m.fitted,
            "background": m.background,
            "failed": m.summary.get("failed"),
            "swapin_mib": m.summary.get("swapin_mib"),
            "swap_share_sim": agg["swap_share"],
            "actual_peak_gib_sim": agg["actual_peak_gib"],
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


def _pct(error: float | None) -> str:
    if error is None:
        return ""
    flag = "" if abs(error) <= TOLERANCE else " (over 15%)"
    return f"{100 * error:+.0f}%{flag}"


def table(rows: list[dict]) -> str:
    """The calibration as a Markdown table: makespan measured, simulated without the swap term
    and with it, with each error, and the mean wait measured and simulated without it."""
    pairs: dict[tuple, dict] = {}
    for r in rows:
        pairs.setdefault((r["run"], r["condition"]), {})["on" if r["swap_term"] else "off"] = r
    head = (
        "| run | condition | fitted | swap-in MiB | makespan s measured | sim, no swap term | "
        "error | sim, swap term | error | wait mean s (measured / sim, no swap term) |\n"
        "|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for (run, condition), pair in pairs.items():
        off, on = pair.get("off"), pair.get("on")
        r = off or on
        cells = [
            run,
            condition,
            _role(r),
            f"{r['swapin_mib']:.0f}",
            f"{r['makespan_s_measured']:.0f}",
            f"{off['makespan_s_sim']:.0f}" if off else "",
            _pct(off["makespan_s_error"]) if off else "",
            f"{on['makespan_s_sim']:.0f}" if on else "",
            _pct(on["makespan_s_error"]) if on else "",
            f"{r['wait_mean_s_measured']:.1f} / {r['wait_mean_s_sim']:.1f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def reversal(rows: list[dict]) -> str:
    """The one measured comparison of two policies on the same plan and machine (the 0.2
    config against two jobs at once, `k8-full-d240-two-at-once`), measured and simulated."""
    pick = {
        (r["condition"], r["swap_term"]): r
        for r in rows
        if r["run"] == "k8-full-d240-two-at-once"
        and r["condition"] in ("lh-enforced-v020", "lh-enforced")
    }
    lines = [
        "| | 0.2 config (weight 4) s | two at once s | two at once against 0.2 |",
        "|---|---|---|---|",
    ]
    old = pick.get(("lh-enforced-v020", False))
    new = pick.get(("lh-enforced", False))
    if old is None or new is None:
        return ""

    def line(name, a, b):
        return f"| {name} | {a:.0f} | {b:.0f} | {100 * (b / a - 1):+.0f}% |"

    lines.append(line("measured", old["makespan_s_measured"], new["makespan_s_measured"]))
    lines.append(line("sim, no swap term", old["makespan_s_sim"], new["makespan_s_sim"]))
    old, new = pick.get(("lh-enforced-v020", True)), pick.get(("lh-enforced", True))
    if old is not None and new is not None:
        lines.append(line("sim, swap term", old["makespan_s_sim"], new["makespan_s_sim"]))
    return "\n".join(lines)


def _role(row: dict) -> str:
    if row["fitted"]:
        return "yes"
    return "held out" if row["leasehold"] else "no"


def write(out: Path, rows: list[dict], params: Params, hold: Overheads, found: Fit | None):
    """`calibration.csv` (every comparison), `calibration.json` (the parameters, `hold`'s
    times and the fit) and `calibration.md` (the tables the docs quote)."""
    out.mkdir(parents=True, exist_ok=True)
    with (out / "calibration.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (out / "calibration.json").write_text(
        json.dumps(
            {
                "params": params.to_dict(),
                "overheads": hold.__dict__,
                "fit": None if found is None else found.summary(),
                "bounds": {name: [lo, hi] for name, lo, hi in FREE},
                "swap_bounds": {name: [lo, hi] for name, lo, hi in SWAP_BOUNDS},
                "tolerance": TOLERANCE,
            },
            indent=1,
        )
        + "\n"
    )
    (out / "calibration.md").write_text(table(rows) + "\n\n" + reversal(rows) + "\n")
