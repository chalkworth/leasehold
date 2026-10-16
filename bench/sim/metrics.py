"""What a run measured, and the mean and 95% confidence interval of each measure over
replications.

Per run: makespan (from the first agent's start to the last call's end); each job's completion
time (its agent's call to its end) and its wait (the call to the job's start), mean and p95;
slot and CPU utilisation; the share of the slots left idle while something waited; the share
of the time the machine had at most one CPU idle (a stand-in for the wakeup latency the real
benchmark measures, which the simulator does not model); fairness between sessions (Jain's
index of their slot-seconds, and the ratio of the longest mean wait to the shortest); and the
memory the leases counted and the jobs used at their peaks.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable

from bench.sim.engine import Result

GIB = 1 << 30
#: How far short of all but one CPU busy still counts as busy: a job on n workers keeps a
#: little under n busy by its speedup curve.
SATURATED_SLACK = 0.1


def pct(values: list[float], p: float) -> float | None:
    """The nearest-rank `p`th percentile (as `bench/run.py` takes it), or None for none."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def jain(values: Iterable[float]) -> float | None:
    """Jain's fairness index: 1 when every value is the same, 1/n when one has everything."""
    xs = list(values)
    if not xs or not any(xs):
        return None
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs))


def summarize(result: Result, background: float = 0.0) -> dict:
    """One run's measures."""
    done = [j for j in result.jobs if j.done is not None]
    makespan = max((j.done for j in done), default=0.0)
    completion = [j.done - j.issued for j in done]
    wait = [j.started - j.issued for j in done]
    run = [j.ended - j.started for j in done]
    by_kind = {
        k: statistics.median([j.ended - j.started for j in done if j.kind == k] or [0.0])
        for k in ("cpu", "mem")
    }
    slot_s, cpu_s, idle_waiting, saturated = 0.0, 0.0, 0.0, 0.0
    samples = result.samples
    for a, b in zip(samples, [*samples[1:], None], strict=True):
        end = makespan if b is None else min(b.t, makespan)
        dt = end - a.t
        if dt <= 0:
            continue
        slot_s += a.held * dt
        cpu_s += min(a.busy, result.cpus) * dt
        if a.waiting:
            idle_waiting += a.free * dt
        if a.busy + background >= result.cpus - 1 - SATURATED_SLACK:
            saturated += dt
    sessions: dict[str, list] = {}
    for j in done:
        sessions.setdefault(j.session, []).append(j)
    shares = [sum((j.granted or 0) * (j.ended - j.started) for j in js) for js in sessions.values()]
    waits = [statistics.fmean([j.started - j.issued for j in js]) for js in sessions.values()]
    span = max(makespan, 1e-9)
    budget = result.budget
    return {
        "jobs": len(result.jobs),
        "unfinished": len(result.jobs) - len(done),
        "makespan_s": makespan,
        "job_mean_s": statistics.fmean(completion) if completion else None,
        "job_p95_s": pct(completion, 95),
        "wait_mean_s": statistics.fmean(wait) if wait else None,
        "wait_p95_s": pct(wait, 95),
        "run_mean_s": statistics.fmean(run) if run else None,
        "run_median_cpu_s": by_kind["cpu"],
        "run_median_mem_s": by_kind["mem"],
        "granted_mean": statistics.fmean([j.granted or 0 for j in done]) if done else None,
        "slot_util": slot_s / (result.slots * span),
        "cpu_util": cpu_s / (result.cpus * span),
        "idle_while_waiting": idle_waiting / (result.slots * span),
        "saturated_share": saturated / span,
        "jain_slot_s": jain(shares),
        "wait_ratio": max(waits) / max(min(waits), 1e-3) if waits else None,
        "counted_peak_gib": result.counted_peak / GIB,
        "actual_peak_gib": result.actual_peak / GIB,
        "budget_gib": None if budget is None else budget / GIB,
        "over_budget": budget is not None and result.actual_peak > budget,
        "guard_on_s": result.guard_on_s,
    }


def t975(df: int) -> float:
    """The 97.5th percentile of Student's t with `df` degrees of freedom (a Cornish-Fisher
    expansion, within 0.002 of the exact value from 3 degrees up)."""
    z = 1.959963984540054
    if df <= 0:
        return math.inf
    if df == 1:
        return 12.706
    if df == 2:
        return 4.303
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


def aggregate(rows: list[dict]) -> dict:
    """The mean of each numeric measure over replications, with the half-width of its 95%
    confidence interval as `<name>_ci` (Student's t over the replications' own values)."""
    out: dict = {"reps": len(rows)}
    keys = [k for k in rows[0] if isinstance(rows[0][k], int | float) and k != "jobs"]
    for key in keys:
        values = [float(r[key]) for r in rows if r.get(key) is not None]
        if not values:
            out[key], out[f"{key}_ci"] = None, None
            continue
        mean = statistics.fmean(values)
        half = (
            t975(len(values) - 1) * statistics.stdev(values) / math.sqrt(len(values))
            if len(values) > 1
            else None
        )
        out[key], out[f"{key}_ci"] = mean, half
    out["jobs"] = statistics.fmean(r["jobs"] for r in rows)
    return out


def timeline(result: Result) -> list[dict]:
    """The run's samples as rows, for a CSV of utilisation over time."""
    return [
        {
            "t": round(s.t, 3),
            "held": s.held,
            "free": s.free,
            "waiting": s.waiting,
            "busy_threads": round(s.busy, 3),
            "counted_gib": round(s.counted / GIB, 3),
            "actual_gib": round(s.actual / GIB, 3),
        }
        for s in result.samples
    ]
