"""`bench/run.py compare`: finished runs judged by a gate file written before they ran.

    python bench/run.py compare DIR [DIR ...] --gate bench/gates/X.toml
    python bench/run.py compare DIR [DIR ...] --gate bench/gates/X.toml --blind-sd
    python bench/run.py compare AA-DIR --gate bench/gates/aa.toml --plan bench/gates/defaults.toml

Provenance first. `compare` refuses results it cannot pool, and says why:

* a results directory whose seed base is not the gate's, whose workload the gate does not
  name, or whose recorded sizes (`spec.json` `args`) differ from the workload's `args`;
* two runs of one condition in one round of one workload (a directory passed twice, or a
  pilot beside a gate);
* runs from more than one commit, leasehold version or machine class;
* a control and a candidate run of one round with different seeds.

The unit is the round. A round counts for a candidate when its control run and its candidate
run are both valid and the control broke no hard rule (no failed job, no OOM kill, no abort).
A round that does not count is listed with the reason. The verdict uses exactly the first N
rounds that count, N being what the gate asks for; later rounds are reported as extra and
never enter it. In each of the N rounds, each co-primary metric gives one paired log ratio,
d = ln(candidate / control). Over the N rounds come the mean, the SD and the one-sided upper
bound mean + t(1 - alpha, N - 1) * SD / sqrt(N). A metric passes when exp(upper bound) is under
its margin.

A candidate passes only when every metric passes on every workload and it broke no hard rule.
This is an intersection-union test, so the number of metrics needs no correction. The hard
rules are checked on every candidate run up to the last of the N rounds, whether or not its
round counts: no failed job, no OOM kill, no abort other than the harness's own failure, and
memory pressure (PSI `some` max) not above `psi_max` and the control's plus `psi_over_control`
at once. A broken hard rule fails a candidate at once. Otherwise, fewer than N rounds, or a
metric missing in any of the N, makes the verdict `incomplete`.

A sign-permutation test of the same hypothesis is reported beside each t bound as a check. It
shifts d by ln(margin) and is exact over all 2^N sign flips up to 12 rounds (else 20 000 seeded
draws). It does not change the verdict.

A gate has one of three kinds:

* `confirmatory`: candidates in their listed order; with `sequential = true`, a candidate is
  tested only if the one before it passed. N is `rounds.planned`, or more after the blind look
  (below), up to `rounds.extend_to`.
* `screen`: drops arms and never promotes one. N is `rounds.planned`. An arm goes if the
  geometric mean of its wake or makespan ratio is above the gate's limit, or it breaks a hard
  rule; with fewer than N rounds it is `incomplete`. At most `max_survivors` stay, the best by
  `rank_by`.
* `aa`: two arms of one condition, over N = `rounds.planned` rounds. It reports the paired SD
  of every metric, and passes when the two-sided interval of `aa.metric`'s mean log ratio
  contains 0 (a ratio of 1) and its paired SD is at most `max_paired_sd`.

The plan. Each gate file states its rule in `[plan]`. The joint power at N rounds is the
chance that a candidate equal to the control passes every metric on every workload, taking the
metrics and workloads as independent: the product of each metric's power to pass its margin,
raised to the number of workloads. The gate runs the fewest rounds from `min_rounds` (8) to
`max_rounds` (12) whose joint power reaches `joint_power` (0.8). If none does, the margins in
`widen` (makespan and job mean, to 1.10) take their wider values and the search runs again.
The latency margins never widen. If 12 rounds still fall short, the gate runs 12 and the plan
states its joint power as a known limit. `--plan GATE` applies GATE's rule to the SDs of an
A/A run; the A/A runs one workload, and its SDs stand for all of GATE's. The blind look
applies the same rule, with no widening, to the pooled SDs of the first `rounds.look_after`
rounds, so a gate runs `planned` rounds, or more if the look asks for it, up to `extend_to`.
Power comes from the noncentral t distribution, integrated numerically.

`--blind-sd` prints only those pooled SDs and the rounds the rule asks for. It says nothing
about which arm is ahead. Otherwise the report is printed, and written as markdown and JSON
to `DIR/compare-<gate name>.md` and `.json`.

Results are matched to the gate by workload (a run's `--workload`) and by condition. A
workload's `params` are added to every arm in it, so `lh-enforced@w=4` in a workload with
`params = "mem=declared"` is `lh-enforced@w=4,mem=declared`.

Everything is computed with the standard library. The t quantile comes from the regularized
incomplete beta function (a continued fraction).
"""

from __future__ import annotations

import json
import math
import random
import sys
import tomllib
from itertools import product
from pathlib import Path

import conditions as conds
import schedule

PERMUTATION_EXACT_UP_TO = 12
PERMUTATION_DRAWS = 20_000
KINDS = ("confirmatory", "screen", "aa")


# ---------------------------------------------------------------------------------------------
# Student's t, in the standard library


def _betacf(a: float, b: float, x: float) -> float:
    """The continued fraction of the incomplete beta function (modified Lentz)."""
    tiny, eps = 1e-300, 1e-15
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d
        d = 1 / (d if abs(d) > tiny else tiny)
        c = 1 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    ln = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x)
    front = math.exp(ln + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with `df` degrees of freedom."""
    tail = 0.5 * betainc(df / 2, 0.5, df / (df + t * t))
    return 1 - tail if t >= 0 else tail


def t_quantile(p: float, df: float) -> float:
    """The `p` quantile of Student's t with `df` degrees of freedom (bisection on `t_cdf`)."""
    if not 0 < p < 1:
        raise ValueError("p is between 0 and 1")
    if df <= 0:
        raise ValueError("df is above 0")
    if p == 0.5:
        return 0.0
    lo, hi = -1.0, 1.0
    while t_cdf(lo, df) > p:
        lo *= 2
    while t_cdf(hi, df) < p:
        hi *= 2
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return (lo + hi) / 2


# ---------------------------------------------------------------------------------------------
# Paired log ratios


def mean_sd(values: list[float]) -> tuple[float, float]:
    n = len(values)
    m = sum(values) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1)) if n > 1 else math.nan
    return m, sd


def sign_permutation_p(diffs: list[float], shift: float = 0.0) -> tuple[float, bool]:
    """(one-sided p, exact) for the mean of `diffs` below `shift`: the share of sign flips of
    `diffs - shift` whose mean is at most the one observed."""
    x = [d - shift for d in diffs]
    observed = sum(x) / len(x)
    n = len(x)
    if n <= PERMUTATION_EXACT_UP_TO:
        count = sum(
            sum(s * v for s, v in zip(signs, x, strict=True)) / n <= observed + 1e-12
            for signs in product((1, -1), repeat=n)
        )
        return count / 2**n, True
    rng = random.Random(0)
    count = sum(
        sum(v if rng.random() < 0.5 else -v for v in x) / n <= observed + 1e-12
        for _ in range(PERMUTATION_DRAWS)
    )
    return (count + 1) / (PERMUTATION_DRAWS + 1), False


def upper_test(diffs: list[float], margin: float, alpha: float) -> dict:
    """The one-sided test that the geometric-mean ratio is under `margin`."""
    n = len(diffs)
    out: dict = {"n": n, "margin": margin}
    if n < 2:
        return out | {"pass": False, "why": f"{n} paired round(s); the test needs 2 or more"}
    m, sd = mean_sd(diffs)
    t = t_quantile(1 - alpha, n - 1)
    upper = m + t * sd / math.sqrt(n)
    perm, exact = sign_permutation_p(diffs, math.log(margin))
    return out | {
        "mean_log": m,
        "sd_log": sd,
        "ratio": math.exp(m),
        "upper_ratio": math.exp(upper),
        "t": t,
        "pass": math.exp(upper) < margin,
        "permutation_p": perm,
        "permutation_exact": exact,
        "permutation_pass": perm <= alpha,
        "permutation_min_p": 1 / 2**n if exact else None,
    }


# ---------------------------------------------------------------------------------------------
# Power, and the plan rule


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def pass_power(n: int, sd: float, margin: float, alpha: float, true_log: float = 0.0) -> float:
    """The chance that the one-sided test of `n` rounds passes `margin` when the paired log
    ratios are normal with SD `sd` around `true_log` (0: the candidate equals the control).
    It is P(T > t) for a noncentral t with n - 1 degrees of freedom: the expectation, over the
    chi-square distribution of the sample variance, of a normal probability, integrated with
    Simpson's rule in u = sqrt(V)."""
    if n < 2:
        return 0.0
    if sd <= 0:
        return 1.0 if true_log < math.log(margin) else 0.0
    nu = n - 1
    t = t_quantile(1 - alpha, nu)
    delta = (math.log(margin) - true_log) * math.sqrt(n) / sd
    log_norm = (nu / 2) * math.log(2) + math.lgamma(nu / 2)
    top = math.sqrt(nu + 12 * math.sqrt(2 * nu) + 40)
    steps = 2000

    def f(u: float) -> float:
        if u <= 0:
            return 2 / math.exp(log_norm) if nu == 1 else 0.0
        dens = 2 * math.exp((nu - 1) * math.log(u) - u * u / 2 - log_norm)
        return _phi(delta - t * u / math.sqrt(nu)) * dens

    h = top / steps
    total = f(0) + f(top)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * f(i * h)
    return min(1.0, max(0.0, total * h / 3))


def joint_power(n: int, sds: dict, margins: dict, alpha: float, workloads: int) -> float:
    """The chance an equal candidate passes every metric on every workload in `n` rounds,
    taking the metrics and the workloads as independent."""
    each = math.prod(pass_power(n, sds[k], margins[k], alpha) for k in margins)
    return each**workloads


def plan(
    sds: dict,
    metrics: list[dict],
    rule: dict,
    alpha: float,
    workloads: int = 1,
    widen: bool = True,
) -> dict:
    """The gate's rounds and margins for the paired SDs `sds` (metric: SD), by `rule` (a gate's
    `[plan]`): the fewest rounds from `min_rounds` to `max_rounds` whose joint power over every
    metric and all `workloads` reaches `joint_power`. If none does, the margins in `widen`
    take their wider values and the search runs again. If that falls short too, the plan is
    `max_rounds` and says the joint power it has. A metric without an SD is left out and
    named."""
    target = rule.get("joint_power", 0.8)
    least, most = rule.get("min_rounds", 8), rule.get("max_rounds", 12)
    wider = rule.get("widen", {}) if widen else {}
    measured = [m for m in metrics if sds.get(m["name"]) is not None]
    margins = {m["name"]: m["margin"] for m in measured}

    def search(margins: dict) -> int | None:
        return next(
            (
                n
                for n in range(least, most + 1)
                if joint_power(n, sds, margins, alpha, workloads) >= target
            ),
            None,
        )

    rounds, widened = search(margins), []
    if rounds is None and any(k in margins for k in wider):
        widened = [k for k in margins if k in wider]
        margins = margins | {k: wider[k] for k in widened}
        rounds = search(margins)
    reached = rounds is not None
    rounds = rounds if reached else most
    return {
        "rounds": rounds,
        "target": target,
        "reached": reached,
        "joint_power": joint_power(rounds, sds, margins, alpha, workloads) if measured else 0.0,
        "workloads": workloads,
        "metrics": [
            {
                "metric": m["name"],
                "sd": sds.get(m["name"]),
                "margin": margins.get(m["name"], m["margin"]),
                "widened": m["name"] in widened,
                "power": (
                    pass_power(rounds, sds[m["name"]], margins[m["name"]], alpha)
                    if m["name"] in margins
                    else None
                ),
            }
            for m in metrics
        ],
        "missing": [m["name"] for m in metrics if sds.get(m["name"]) is None],
    }


def _workloads(n: int) -> str:
    return "the one workload" if n == 1 else f"all {n} workloads"


def plan_text(result: dict, heading: str, note: str = "") -> str:
    lines = [heading, ""]
    if note:
        lines += [note, ""]
    lines += [
        f"Rounds: {result['rounds']}. Joint power, the chance an equal candidate passes every "
        f"metric on {_workloads(result['workloads'])}, taking them as independent: "
        f"{result['joint_power']:.2f} (target {result['target']:.2f}).",
        "",
        "| metric | paired SD | margin | power on one workload |",
        "|---|---|---|---|",
    ]
    for m in result["metrics"]:
        if m["sd"] is None:
            lines.append(f"| {m['metric']} | not measured | {m['margin']:.2f} | |")
            continue
        margin = f"{m['margin']:.2f}" + (" (widened)" if m["widened"] else "")
        lines.append(f"| {m['metric']} | {m['sd']:.3f} | {margin} | {m['power']:.2f} |")
    lines.append("")
    if not result["reached"]:
        lines.append(
            f"Known limit: even at {result['rounds']} rounds, with the throughput margins "
            f"widened, the joint power is {result['joint_power']:.2f}, under the target. An "
            "equal candidate may fail this gate."
        )
    if result["missing"]:
        lines.append(f"No SD for: {', '.join(result['missing'])}; the plan leaves them out.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Results


class Refused(Exception):
    """Results `compare` will not judge against a gate, with the reasons."""


def load_gate(path: Path) -> dict:
    """A gate file, checked for the fields every kind needs."""
    gate = tomllib.loads(path.read_text())
    about = gate.get("gate", {})
    kind = about.get("kind")
    if kind not in KINDS:
        raise SystemExit(f"{path}: [gate] kind is one of {', '.join(KINDS)}")
    for key in ("name", "intent", "objective", "stop", "decision"):
        if not about.get(key):
            raise SystemExit(f"{path}: [gate] needs {key}")
    arms = gate.get("arms", {})
    if not arms.get("control"):
        raise SystemExit(f"{path}: [arms] needs control")
    try:
        arms["control"] = conds.parse(arms["control"]).text
        arms["candidates"] = [conds.parse(c).text for c in arms.get("candidates", [])]
    except ValueError as exc:
        raise SystemExit(f"{path}: [arms]: {exc}") from exc
    if not arms["candidates"]:
        raise SystemExit(f"{path}: [arms] candidates is empty; name the arms to compare")
    gate.setdefault("test", {}).setdefault("alpha", 0.05)
    hard = gate.setdefault("hard", {})
    hard.setdefault("failed_jobs", 0)
    hard.setdefault("oom_kills", 0)
    gate.setdefault("plan", {})
    if not gate.get("rounds", {}).get("planned"):
        raise SystemExit(f"{path}: [rounds] needs planned")
    gate.setdefault("workloads", [])
    if not gate["workloads"]:
        raise SystemExit(f"{path}: needs [[workloads]], each with its args")
    import run as bench

    for w in gate["workloads"]:
        if not w.get("name") or not w.get("args"):
            raise SystemExit(f"{path}: every [[workloads]] needs a name and args")
        try:
            for arm in [arms["control"], *arms["candidates"]]:
                arm_in(gate, w["name"], arm)
        except ValueError as exc:
            raise SystemExit(f"{path}: workload {w['name']}: {exc}") from exc
        w["sizes"] = bench.workload_args(w["args"])
    if not gate.get("metrics") and kind != "screen":
        raise SystemExit(f"{path}: needs [[metrics]]")
    return gate


def collect(dirs: list[Path]) -> tuple[list[dict], list[dict]]:
    """(every run under `dirs` that has a manifest, each directory's facts). A run has its
    workload, round, seed, condition, whether it is valid and why not, the code and machine
    it ran on, and, if it has results, its summary row."""
    import run as bench

    records, infos = [], []
    for d in dirs:
        spec = json.loads((d / "spec.json").read_text())
        workload = spec.get("workload") or spec.get("preset_name")
        seed_base = spec.get("seed_base", spec.get("seed", 1) - 1)
        rounds_seen = 0
        for manifest_path in sorted(d.glob("*/manifest.json")):
            m = json.loads(manifest_path.read_text())
            workdir = manifest_path.parent
            has = (workdir / "outer.json").exists()
            row = bench.summarize_condition(workdir, m["label"]) if has else None
            rounds_seen = max(rounds_seen, m["round"])
            records.append(
                {
                    "dir": d.name,
                    "workload": workload,
                    "seed_base": seed_base,
                    "round": m["round"],
                    "seed": m.get("seed"),
                    "condition": m["condition"]["text"],
                    "valid": bool(m.get("valid")) and has,
                    "why_invalid": m.get("why_invalid") or ("no results" if not has else ""),
                    "row": row,
                    "git_sha": (m.get("git") or {}).get("sha"),
                    "leasehold": m.get("leasehold"),
                    "machine_class": m.get("machine", {}).get("class"),
                }
            )
        infos.append(
            {
                "name": d.name,
                "workload": workload,
                "seed_base": seed_base,
                "args": spec.get("args"),
                "arms": len(spec.get("conditions", [])),
                "rounds_seen": rounds_seen,
            }
        )
    return records, infos


def check_provenance(gate: dict, records: list[dict], infos: list[dict]) -> None:
    """Raise Refused with every reason these results cannot be judged together by `gate`."""
    why = []
    base = gate.get("rounds", {}).get("seed_base")
    sizes = {w["name"]: w["sizes"] for w in gate["workloads"]}
    for info in infos:
        name = info["name"]
        if info["workload"] not in sizes:
            why.append(f"{name} is workload {info['workload']!r}, which the gate does not name")
            continue
        if base is not None and info["seed_base"] != base:
            why.append(f"{name} ran with seed base {info['seed_base']}; the gate names {base}")
        if info["args"] is None:
            why.append(f"{name} has no recorded args (it predates them)")
        elif info["args"] != sizes[info["workload"]]:
            diff = sorted(
                k
                for k in sizes[info["workload"]]
                if info["args"].get(k) != sizes[info["workload"]][k]
            )
            why.append(f"{name} ran with other args than workload {info['workload']!r}: {diff}")
    seen: dict[tuple, str] = {}
    for r in records:
        key = (r["workload"], r["condition"], r["round"])
        if key in seen:
            why.append(
                f"{r['condition']} round {r['round']} of {r['workload']!r} is in both "
                f"{seen[key]} and {r['dir']}"
            )
        seen[key] = r["dir"]
    for field, label in (
        ("git_sha", "commits"),
        ("leasehold", "leasehold versions"),
        ("machine_class", "machine classes"),
    ):
        values = sorted({str(r[field]) for r in records})
        if len(values) > 1:
            why.append(f"the runs come from {len(values)} {label}: {', '.join(values)}")
    if why:
        raise Refused("; ".join(why))


def workloads_of(gate: dict) -> list[str]:
    return [w["name"] for w in gate["workloads"]]


def arm_in(gate: dict, workload: str, arm: str) -> str:
    """The condition `arm` runs as in `workload`: with the workload's `params` added, so that
    one gate can name `lh-enforced@w=4` and have it declare memory in a memory-heavy mix."""
    extra = next((w.get("params") for w in gate["workloads"] if w["name"] == workload), None)
    if not extra:
        return arm
    return conds.parse(arm + ("," if "@" in arm else "@") + extra).text


def _breaks(gate: dict, row: dict, harness_counts: bool) -> list[str]:
    hard = gate["hard"]
    out = []
    if (row.get("failed") or 0) > hard["failed_jobs"]:
        out.append(f"{row['failed']} failed jobs")
    ooms = (row.get("oom_kills") or 0) + (row.get("oom_kills_machine") or 0)
    if ooms > hard["oom_kills"]:
        out.append(f"{ooms} OOM kills")
    aborted = str(row.get("aborted") or "")
    if aborted and (harness_counts or not aborted.startswith("harness error")):
        out.append(f"aborted ({aborted})")
    return out


def psi_break(gate: dict, cand: dict, ctrl: dict | None) -> str:
    """The memory-pressure rule: a candidate round breaks it when its PSI max is above
    `psi_max` and above the control's in that round plus `psi_over_control` (without a control
    run, above `psi_max` alone)."""
    hard = gate["hard"]
    psi = cand.get("mem_some_max")
    if "psi_max" not in hard or psi is None or psi <= hard["psi_max"]:
        return ""
    base = (ctrl or {}).get("mem_some_max")
    over = hard.get("psi_over_control", 0.0)
    if base is not None and psi <= base + over:
        return ""
    return f"memory PSI max {psi}% (control {base}%) above {hard['psi_max']}%" + (
        f" and the control's plus {over}" if base is not None else ""
    )


def window(gate: dict, records: list[dict], workload: str, candidate: str, needed: int) -> dict:
    """The rounds of `candidate` against the control in `workload`: the first `needed` that
    count (`used`, as (round, control row, candidate row)), those that do not and why
    (`invalid`), those after the `needed`th (`extra`), and the hard rules the candidate broke
    in any round up to the last used one (or any round, while fewer than `needed` count)."""
    control = arm_in(gate, workload, gate["arms"]["control"])
    candidate = arm_in(gate, workload, candidate)
    runs: dict[tuple[str, int], dict] = {}
    for r in records:
        if r["workload"] == workload and r["condition"] in (control, candidate):
            runs[(r["condition"], r["round"])] = r
    used, invalid, extra, hard, psi = [], [], [], [], []
    for rnd in sorted({k[1] for k in runs}):
        if len(used) >= needed:
            extra.append(rnd)
            continue
        c, k = runs.get((control, rnd)), runs.get((candidate, rnd))
        if c and k and c["seed"] != k["seed"]:
            raise Refused(
                f"{workload!r} round {rnd}: the control ran seed {c['seed']}, the "
                f"candidate {k['seed']}"
            )
        if k and k["row"]:
            ctrl_row = c["row"] if c else None
            hard += [f"round {rnd}: {b}" for b in _breaks(gate, k["row"], False)]
            if why := psi_break(gate, k["row"], ctrl_row):
                hard.append(f"round {rnd}: {why}")
            psi.append((rnd, (ctrl_row or {}).get("mem_some_max"), k["row"].get("mem_some_max")))
        reason = ""
        if c is None:
            reason = "no control run"
        elif k is None:
            reason = "no candidate run"
        elif c["row"] and (broke := _breaks(gate, c["row"], True)):
            reason = "the control broke a hard rule: " + ", ".join(broke)
        elif not c["valid"]:
            reason = f"the control run is invalid: {c['why_invalid']}"
        elif not k["valid"]:
            reason = f"the candidate run is invalid: {k['why_invalid']}"
        if reason:
            invalid.append((rnd, reason))
        else:
            used.append((rnd, c["row"], k["row"]))
    return {
        "used": used,
        "invalid": invalid,
        "extra": extra,
        "hard": hard,
        "psi": psi,
        "needed": needed,
        "complete": len(used) >= needed,
    }


def log_ratios(paired: list[tuple], metric: str) -> tuple[list[float], list[int]]:
    """ln(candidate / control) of `metric` per round; the rounds where it is missing."""
    out, missing = [], []
    for rnd, ctrl, cand in paired:
        a, b = ctrl.get(metric), cand.get(metric)
        if a in (None, "") or b in (None, "") or float(a) <= 0 or float(b) <= 0:
            missing.append(rnd)
            continue
        out.append(math.log(float(b) / float(a)))
    return out, missing


def _window_out(w: dict) -> dict:
    return {
        "rounds": [u[0] for u in w["used"]],
        "needed": w["needed"],
        "complete": w["complete"],
        "invalid": [{"round": r, "why": why} for r, why in w["invalid"]],
        "extra": w["extra"],
        "hard": w["hard"],
        "psi": [{"round": r, "control": c, "candidate": k} for r, c, k in w["psi"]],
    }


def blind_sd(gate: dict, records: list[dict]) -> dict:
    """The pooled paired SD of each co-primary metric over the first `look_after` rounds that
    count, pooled over candidates and workloads, and the rounds the plan rule asks for at those
    SDs (between `planned` and `extend_to`). Nothing about which arm is ahead."""
    rounds = gate["rounds"]
    look = rounds.get("look_after") or rounds["planned"]
    sums: dict[str, list[float]] = {m["name"]: [0.0, 0] for m in gate["metrics"]}
    ready = True
    for workload in workloads_of(gate):
        for cand in gate["arms"]["candidates"]:
            w = window(gate, records, workload, cand, look)
            ready = ready and w["complete"]
            for spec in gate["metrics"]:
                d, _ = log_ratios(w["used"], spec["name"])
                if len(d) >= 2:
                    _, sd = mean_sd(d)
                    sums[spec["name"]][0] += (len(d) - 1) * sd**2
                    sums[spec["name"]][1] += len(d) - 1
    sds = {k: (math.sqrt(a / b) if b else None) for k, (a, b) in sums.items()}
    rule = gate["plan"] | {
        "min_rounds": rounds["planned"],
        "max_rounds": rounds.get("extend_to", rounds["planned"]),
    }
    planned = plan(
        sds, gate["metrics"], rule, gate["test"]["alpha"], len(gate["workloads"]), widen=False
    )
    ready = ready and not planned["missing"]
    return {
        "look_after": look,
        "ready": ready,
        "pooled_sd": sds,
        "rounds_needed": planned["rounds"] if ready else rounds["planned"],
        "plan": planned,
    }


def evaluate(gate: dict, records: list[dict]) -> dict:
    kind = gate["gate"]["kind"]
    return {"confirmatory": confirmatory, "screen": screen, "aa": aa}[kind](gate, records)


def confirmatory(gate: dict, records: list[dict]) -> dict:
    alpha = gate["test"]["alpha"]
    look = blind_sd(gate, records)
    needed = look["rounds_needed"]
    results, stop = [], False
    for cand in gate["arms"]["candidates"]:
        if stop:
            results.append(
                {
                    "candidate": cand,
                    "verdict": "not tested",
                    "why": ["an earlier candidate did not pass (sequential order)"],
                }
            )
            continue
        per_workload, why, incomplete = [], [], False
        for workload in workloads_of(gate):
            w = window(gate, records, workload, cand, needed)
            if not w["complete"]:
                incomplete = True
                why.append(f"{workload}: {len(w['used'])} of {needed} rounds count")
            metrics = []
            for spec in gate["metrics"]:
                d, missing = log_ratios(w["used"], spec["name"])
                res = {"metric": spec["name"], "missing_rounds": missing}
                res |= upper_test(d, spec["margin"], alpha)
                if missing:
                    incomplete = True
                    why.append(f"{workload}: {spec['name']} missing in rounds {missing}")
                elif not res["pass"] and "upper_ratio" in res:
                    why.append(
                        f"{workload}: {spec['name']} upper bound {res['upper_ratio']:.3f} not "
                        f"under {spec['margin']}"
                    )
                metrics.append(res)
            why += [f"{workload}: {b}" for b in w["hard"]]
            per_workload.append({"workload": workload, "metrics": metrics} | _window_out(w))
        broken = any(w["hard"] for w in per_workload)
        passed = all(m["pass"] for w in per_workload for m in w["metrics"])
        if broken:
            verdict = "fail"
        elif incomplete:
            verdict = "incomplete"
        else:
            verdict = "pass" if passed else "fail"
        results.append(
            {"candidate": cand, "verdict": verdict, "why": why, "workloads": per_workload}
        )
        # Sequential order: the next candidate is tested only after this one passed.
        if gate.get("order", {}).get("sequential", True) and verdict != "pass":
            stop = True
    return {"kind": "confirmatory", "rounds_needed": needed, "blind": look, "results": results}


def screen(gate: dict, records: list[dict]) -> dict:
    rule = gate.get("screen", {})
    needed = gate["rounds"]["planned"]
    results = []
    for cand in gate["arms"]["candidates"]:
        why, figures, windows, incomplete = [], {}, [], False
        for workload in workloads_of(gate):
            w = window(gate, records, workload, cand, needed)
            windows.append({"workload": workload} | _window_out(w))
            if rule.get("hard_rules", True):
                why += [f"{workload}: {b}" for b in w["hard"]]
            if not w["complete"]:
                incomplete = True
            for metric, limit_key in (
                (rule.get("wake_metric", "wake_same_p99_ms"), "wake_ratio_above"),
                ("makespan_s", "makespan_ratio_above"),
            ):
                d, missing = log_ratios(w["used"], metric)
                if missing or not d:
                    incomplete = True
                    continue
                gm = math.exp(sum(d) / len(d))
                figures[f"{workload}:{metric}"] = {"ratio": gm, "rounds": len(d)}
                limit = rule.get(limit_key)
                if limit is not None and gm > limit:
                    why.append(f"{workload}: {metric} ratio {gm:.3f} above {limit}")
        if why:
            verdict = "drop"
        elif incomplete:
            verdict = "incomplete"
            why.append(f"fewer than {needed} rounds count, or a metric is missing")
        else:
            verdict = "survives"
        results.append(
            {
                "candidate": cand,
                "verdict": verdict,
                "why": why,
                "figures": figures,
                "workloads": windows,
            }
        )
    keep = rule.get("max_survivors")
    rank = rule.get("rank_by", "makespan_s")
    alive = [r for r in results if r["verdict"] == "survives"]

    def score(r: dict) -> float:
        vals = [v["ratio"] for k, v in r["figures"].items() if k.endswith(f":{rank}")]
        return math.exp(sum(map(math.log, vals)) / len(vals)) if vals else math.inf

    if keep is not None and len(alive) > keep:
        for r in sorted(alive, key=score)[keep:]:
            r["verdict"] = "drop"
            r["why"].append(f"not among the {keep} best by {rank}")
    return {"kind": "screen", "results": results}


def aa(gate: dict, records: list[dict]) -> dict:
    rule = gate.get("aa", {})
    level = rule.get("ci", 0.95)
    max_sd = rule.get("max_paired_sd", 0.06)
    primary = rule.get("metric", "makespan_s")
    needed = gate["rounds"]["planned"]
    cand = gate["arms"]["candidates"][0]
    per, why, windows, incomplete = [], [], [], False
    for workload in workloads_of(gate):
        w = window(gate, records, workload, cand, needed)
        windows.append({"workload": workload} | _window_out(w))
        why += [f"{workload}: {b}" for b in w["hard"]]
        if not w["complete"]:
            incomplete = True
            why.append(f"{workload}: {len(w['used'])} of {needed} rounds count")
        for spec in gate["metrics"]:
            d, missing = log_ratios(w["used"], spec["name"])
            res: dict = {
                "workload": workload,
                "metric": spec["name"],
                "n": len(d),
                "missing_rounds": missing,
            }
            if missing:
                incomplete = True
                why.append(f"{workload}: {spec['name']} missing in rounds {missing}")
            if len(d) >= 2:
                m, sd = mean_sd(d)
                half = t_quantile(1 - (1 - level) / 2, len(d) - 1) * sd / math.sqrt(len(d))
                res |= {
                    "mean_log": m,
                    "sd_log": sd,
                    "ci_ratio": [math.exp(m - half), math.exp(m + half)],
                    "contains_1": m - half <= 0 <= m + half,
                }
            per.append(res)
    failed = bool([b for w in windows for b in w["hard"]])
    for r in per:
        if r["metric"] != primary or "sd_log" not in r:
            continue
        if not r["contains_1"]:
            failed = True
            why.append(f"{r['workload']}: the {level:.0%} interval of {primary} leaves out 1")
        if r["sd_log"] > max_sd:
            failed = True
            why.append(f"{r['workload']}: paired SD {r['sd_log']:.3f} above {max_sd}")
    verdict = "fail" if failed else "incomplete" if incomplete else "pass"
    sds = {r["metric"]: r.get("sd_log") for r in per}
    return {
        "kind": "aa",
        "sds": sds,
        "results": [
            {
                "candidate": cand,
                "verdict": verdict,
                "why": why,
                "metrics": per,
                "workloads": windows,
            }
        ],
    }


def balance_notes(infos: list[dict]) -> list[str]:
    """Where the order is not balanced: a directory whose rounds are not a whole number of
    Williams cycles."""
    out = []
    for info in infos:
        if info["arms"] < 2 or not info["rounds_seen"]:
            continue
        cycle = len(schedule.williams(info["arms"]))
        if info["rounds_seen"] % cycle:
            out.append(
                f"{info['name']}: {info['rounds_seen']} rounds of {info['arms']} arms is not a "
                f"multiple of the Williams cycle of {cycle}, so the order is balanced only over "
                "the whole cycles in it"
            )
    return out


def judge(gate: dict, dirs: list[Path]) -> dict:
    """The gate's verdict on the results in `dirs`; Refused if they cannot be judged."""
    records, infos = collect(dirs)
    check_provenance(gate, records, infos)
    return evaluate(gate, records) | {"notes": balance_notes(infos)}


# ---------------------------------------------------------------------------------------------
# The report


def _f(v, nd=3) -> str:
    return "" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _window_lines(w: dict) -> list[str]:
    lines = [
        f"Workload `{w['workload']}`: rounds {', '.join(map(str, w['rounds'])) or 'none'} "
        f"count ({len(w['rounds'])} of {w['needed']}).",
        "",
    ]
    lines += [f"- round {i['round']} does not count: {i['why']}" for i in w["invalid"]]
    if w["extra"]:
        many = len(w["extra"]) > 1
        lines.append(
            f"- {'rounds' if many else 'round'} {', '.join(map(str, w['extra']))} came after the "
            f"{w['needed']} the gate uses; {'they are' if many else 'it is'} not in the verdict"
        )
    lines += [f"- hard rule: {h}" for h in w["hard"]] or ["- hard rules: kept"]
    if w["psi"]:
        lines.append(
            "- memory PSI max by round, control / candidate: "
            + ", ".join(f"r{p['round']} {p['control']} / {p['candidate']}" for p in w["psi"])
        )
    lines.append("")
    return lines


def report(gate: dict, result: dict, dirs: list[Path]) -> str:
    about = gate["gate"]
    lines = [
        f"# Gate `{about['name']}` ({about['kind']})",
        "",
        f"- Intent: {about['intent']}",
        f"- Objective: {about['objective']}",
        f"- Stop rule: {about['stop']}",
        f"- Decision: {about['decision']}",
        "",
        f"Results: {', '.join(d.name for d in dirs)}. Control `{gate['arms']['control']}`.",
        "",
        *[f"- Note: {n}" for n in result.get("notes", [])],
        *([""] if result.get("notes") else []),
    ]
    if result["kind"] == "confirmatory":
        b = result["blind"]
        lines += [
            f"Rounds the verdict uses: {result['rounds_needed']}"
            + (
                f" (the plan rule at the pooled SDs of the first {b['look_after']} rounds)."
                if b["ready"]
                else f" (the blind look needs {b['look_after']} rounds of every candidate)."
            ),
            "",
        ]
    for r in result["results"]:
        lines += [f"## `{r['candidate']}`: {r['verdict']}", ""]
        lines += [f"- {w}" for w in r["why"]] + ([""] if r["why"] else [])
        for w in r.get("workloads", []):
            lines += _window_lines(w)
            if "metrics" not in w:
                continue
            lines += [
                "| metric | n | ratio | upper bound | margin | t | permutation p | verdict |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for m in w["metrics"]:
                lines.append(
                    f"| {m['metric']} | {m['n']} | {_f(m.get('ratio'))} | "
                    f"{_f(m.get('upper_ratio'))} | {m['margin']} | {_f(m.get('t'))} | "
                    f"{_f(m.get('permutation_p'))} | {'pass' if m['pass'] else 'fail'} |"
                )
            lines.append("")
        if r.get("figures"):
            lines += ["| workload:metric | geometric-mean ratio | rounds |", "|---|---|---|"]
            for k, v in r["figures"].items():
                lines.append(f"| {k} | {_f(v['ratio'])} | {v['rounds']} |")
            lines.append("")
        if r.get("metrics") and result["kind"] == "aa":
            lines += ["| workload | metric | n | paired SD | interval |", "|---|---|---|---|---|"]
            for m in r["metrics"]:
                ci = m.get("ci_ratio")
                lines.append(
                    f"| {m['workload']} | {m['metric']} | {m['n']} | {_f(m.get('sd_log'))} | "
                    + (f"{ci[0]:.3f} to {ci[1]:.3f}" if ci else "")
                    + " |"
                )
            lines.append("")
    return "\n".join(lines)


def blind_text(b: dict) -> str:
    lines = [f"Pooled paired SDs over the first {b['look_after']} rounds that count:"]
    for m in b["plan"]["metrics"]:
        sd = "not measured" if m["sd"] is None else f"{m['sd']:.4f}"
        lines.append(f"  {m['metric']}: {sd}")
    if b["ready"]:
        lines.append(f"The plan rule asks for {b['rounds_needed']} rounds.")
    else:
        lines.append(
            f"Not every candidate has {b['look_after']} rounds that count yet, or a metric has "
            "no SD; the gate stays at its planned rounds."
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="run.py compare", description=__doc__.split("\n\n")[0])
    parser.add_argument("dirs", nargs="+", type=Path, help="results directories")
    parser.add_argument("--gate", type=Path, required=True, help="a bench/gates/*.toml file")
    parser.add_argument(
        "--blind-sd", action="store_true", help="print only the pooled SDs for the blind look"
    )
    parser.add_argument(
        "--plan",
        type=Path,
        help="with an aa gate: print the rounds and margins this gate's [plan] rule gives",
    )
    parser.add_argument(
        "--out", type=Path, help="report path without suffix (default: DIR/compare-<gate name>)"
    )
    args = parser.parse_args(argv)
    gate = load_gate(args.gate)
    try:
        if args.blind_sd:
            records, infos = collect(args.dirs)
            check_provenance(gate, records, infos)
            print(blind_text(blind_sd(gate, records)))
            return 0
        result = judge(gate, args.dirs)
    except Refused as exc:
        print(f"compare: refused: {exc}", file=sys.stderr)
        return 2
    text = report(gate, result, args.dirs)
    if args.plan:
        if result["kind"] != "aa":
            parser.error("--plan reads the SDs of an aa gate's results")
        target = load_gate(args.plan) if _has_candidates(args.plan) else _load_plan_only(args.plan)
        names = [w["name"] for w in target["workloads"]] or ["one workload"]
        planned = plan(
            result["sds"], target["metrics"], target["plan"], target["test"]["alpha"], len(names)
        )
        measured = ", ".join(f"`{w}`" for w in workloads_of(gate))
        note = (
            f"The A/A measured {measured} only; its SDs stand for every workload of the gate "
            f"({', '.join(f'`{n}`' for n in names)})."
        )
        text += "\n" + plan_text(planned, f"## Plan for `{args.plan.name}`", note)
        if result["results"][0]["verdict"] != "pass":
            text += "\n\nThe A/A did not pass; fix the bench before planning on its SDs."
        result["plan"] = planned
    stem = args.out or args.dirs[0] / f"compare-{gate['gate']['name']}"
    stem.with_suffix(".md").write_text(text + "\n")
    stem.with_suffix(".json").write_text(
        json.dumps({"gate": gate["gate"], "arms": gate["arms"]} | result, indent=1)
    )
    print(text)
    return 0


def _has_candidates(path: Path) -> bool:
    return bool(tomllib.loads(path.read_text()).get("arms", {}).get("candidates"))


def _load_plan_only(path: Path) -> dict:
    """A gate file's metrics, `[plan]` and alpha, for a gate whose candidates are not yet
    filled in."""
    raw = tomllib.loads(path.read_text())
    if not raw.get("metrics"):
        raise SystemExit(f"{path}: needs [[metrics]]")
    return {
        "metrics": raw["metrics"],
        "plan": raw.get("plan", {}),
        "test": {"alpha": raw.get("test", {}).get("alpha", 0.05)},
        "workloads": raw.get("workloads", []),
    }
