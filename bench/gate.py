"""`bench/run.py compare`: a finished run judged by a gate file written before it ran.

    python bench/run.py compare RESULTS-DIR [RESULTS-DIR ...] --gate bench/gates/X.toml
    python bench/run.py compare RESULTS-DIR --gate bench/gates/X.toml --blind-sd

The unit is the round. For each candidate arm, each workload and each co-primary metric, every
round where the candidate and the control both ran validly gives one paired log ratio,
d = ln(candidate / control). Over the n rounds: the mean, the SD, and the one-sided upper
bound mean + t(1 - alpha, n - 1) * SD / sqrt(n). A metric passes when exp(upper bound) is under
its margin; a candidate passes only when every metric passes on every workload and no hard rule
breaks (an intersection-union test, so no correction for the number of metrics is needed). A
sign-permutation test of the same hypothesis (d shifted by ln(margin); exact over all 2^n
signs up to 12 rounds, else 20 000 seeded draws) is reported beside each t bound as a check;
it does not change the verdict.

A gate has one of three kinds:

* `confirmatory`: the test above, candidates in their listed order; with `sequential = true`,
  a candidate is tested only if the one before it passed. A broken hard rule fails a candidate
  at once. Otherwise, with fewer valid rounds than the gate asks for, the verdict is
  `incomplete`. The number asked for is `rounds.planned`, or `rounds.extend_to` when the blind
  look says so (below).
* `screen`: drops arms and never promotes one: an arm goes if the geometric mean of its wake
  ratio, or of its makespan ratio, is above the gate's limit, or a hard rule breaks. At most
  `max_survivors` stay, the best by `rank_by`.
* `aa`: two arms of one condition. It passes when the two-sided interval of the metric's mean
  log ratio contains 0 (a ratio of 1) and the paired SD is at most `max_paired_sd`.

Results are matched to the gate by workload (a run's `--workload`) and by condition. A
workload's `params` are added to every arm in it, so `lh-enforced@w=4` in a workload with
`params = "mem=declared"` is `lh-enforced@w=4,mem=declared`. The report warns about results
whose seed base is not the gate's, or whose workload the gate does not name.

`--blind-sd` prints only the pooled SD of the paired log ratios of `rounds.blind_metric` over
the first `rounds.look_after` rounds, pooled over candidates and workloads, and what the
extension rule makes of it. It says nothing about which arm is ahead. Otherwise the report is
printed, and written as markdown and JSON to `DIR/compare-<gate name>.md` and `.json`.

The t quantile is computed here, from the regularized incomplete beta function (a continued
fraction), so that `compare` needs only the standard library.
"""

from __future__ import annotations

import json
import math
import random
import tomllib
from itertools import product
from pathlib import Path

import conditions as conds

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
# Results


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
    gate.setdefault("hard", {})
    gate.setdefault("workloads", [])
    for w in gate["workloads"]:
        if not w.get("name"):
            raise SystemExit(f"{path}: every [[workloads]] needs a name")
        try:
            for arm in [arms["control"], *arms["candidates"]]:
                arm_in(gate, w["name"], arm)
        except ValueError as exc:
            raise SystemExit(f"{path}: workload {w['name']}: {exc}") from exc
    if not gate.get("metrics") and kind != "screen":
        raise SystemExit(f"{path}: needs [[metrics]]")
    return gate


def collect(dirs: list[Path]) -> list[dict]:
    """Every run under `dirs` that has a manifest: workload, round, condition, valid, and, for a
    run with results, its summary row."""
    import run as bench

    out = []
    for d in dirs:
        spec = json.loads((d / "spec.json").read_text())
        workload = spec.get("workload") or spec.get("preset_name")
        seed_base = spec.get("seed_base", spec.get("seed", 1) - 1)
        for manifest_path in sorted(d.glob("*/manifest.json")):
            m = json.loads(manifest_path.read_text())
            workdir = manifest_path.parent
            has = (workdir / "outer.json").exists()
            row = bench.summarize_condition(workdir, m["label"]) if has else None
            out.append(
                {
                    "dir": d.name,
                    "workload": workload,
                    "seed_base": seed_base,
                    "round": m["round"],
                    "condition": m["condition"]["text"],
                    "valid": bool(m.get("valid")) and has,
                    "row": row,
                }
            )
    return out


def workloads_of(gate: dict, records: list[dict]) -> list[str]:
    named = [w["name"] for w in gate["workloads"]]
    return named or sorted({r["workload"] for r in records})


def arm_in(gate: dict, workload: str, arm: str) -> str:
    """The condition `arm` runs as in `workload`: with the workload's `params` added, so that
    one gate can name `lh-enforced@w=4` and have it declare memory in a memory-heavy mix."""
    extra = next((w.get("params") for w in gate["workloads"] if w["name"] == workload), None)
    if not extra:
        return arm
    return conds.parse(arm + ("," if "@" in arm else "@") + extra).text


def warnings(gate: dict, records: list[dict]) -> list[str]:
    """What in the results does not match the gate: a seed base other than the gate's (a
    pilot's seeds are never pooled with a gate's), or a workload the gate does not name."""
    out = []
    base = gate.get("rounds", {}).get("seed_base")
    named = {w["name"] for w in gate["workloads"]}
    for d, seed_base, workload in sorted(
        {(r["dir"], r["seed_base"], r["workload"]) for r in records}
    ):
        if base is not None and seed_base != base:
            out.append(f"{d} ran with seed base {seed_base}, the gate names {base}")
        if named and workload not in named:
            out.append(f"{d} is workload {workload!r}, which the gate does not name; left out")
    return out


def pairs(
    records: list[dict], workload: str, control: str, candidate: str, gate: dict | None = None
) -> list[tuple]:
    """(round, control row, candidate row) for every round where both ran validly."""
    if gate is not None:
        control, candidate = arm_in(gate, workload, control), arm_in(gate, workload, candidate)
    by: dict[tuple[str, int], dict] = {}
    for r in records:
        if r["workload"] == workload and r["valid"]:
            by[(r["condition"], r["round"])] = r["row"]
    mine = {rnd for c, rnd in by if c == candidate}
    rounds = sorted({rnd for c, rnd in by if c == control} & mine)
    return [(rnd, by[(control, rnd)], by[(candidate, rnd)]) for rnd in rounds]


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


def hard_breaks(gate: dict, paired: list[tuple]) -> list[str]:
    """The hard rules a candidate broke, round by round."""
    hard = gate["hard"]
    out = []
    for rnd, _ctrl, cand in paired:
        if "failed_jobs" in hard and (cand.get("failed") or 0) > hard["failed_jobs"]:
            out.append(f"round {rnd}: {cand['failed']} failed jobs")
        ooms = (cand.get("oom_kills") or 0) + (cand.get("oom_kills_machine") or 0)
        if "oom_kills" in hard and ooms > hard["oom_kills"]:
            out.append(f"round {rnd}: {ooms} OOM kills")
        psi = cand.get("mem_some_max")
        if "psi_max" in hard and psi is not None and psi > hard["psi_max"]:
            out.append(f"round {rnd}: memory PSI max {psi}% above {hard['psi_max']}%")
        if cand.get("aborted"):
            out.append(f"round {rnd}: aborted ({cand['aborted']})")
    return out


def blind_sd(gate: dict, records: list[dict]) -> dict:
    """The pooled SD of the paired log ratios of the blind metric over the first
    `look_after` rounds, and what the extension rule makes of it."""
    rounds = gate.get("rounds", {})
    metric = rounds.get("blind_metric", "makespan_s")
    look = rounds.get("look_after")
    arms = gate["arms"]
    num, dof, n_pairs = 0.0, 0, 0
    for workload in workloads_of(gate, records):
        for cand in arms["candidates"]:
            paired = pairs(records, workload, arms["control"], cand, gate)
            if look:
                paired = [p for p in paired if p[0] <= look]
            d, _ = log_ratios(paired, metric)
            if len(d) >= 2:
                _, sd = mean_sd(d)
                num += (len(d) - 1) * sd**2
                dof += len(d) - 1
                n_pairs += len(d)
    sd = math.sqrt(num / dof) if dof else None
    limit = rounds.get("extend_if_sd_above")
    out = {
        "metric": metric,
        "look_after": look,
        "paired_rounds": n_pairs,
        "pooled_sd": sd,
        "extend_if_sd_above": limit,
    }
    if sd is not None and limit is not None:
        extend = sd > limit
        out["rounds_needed"] = rounds.get("extend_to") if extend else rounds.get("planned")
        out["extend"] = extend
    return out


def evaluate(gate: dict, records: list[dict]) -> dict:
    kind = gate["gate"]["kind"]
    return {"confirmatory": confirmatory, "screen": screen, "aa": aa}[kind](gate, records)


def confirmatory(gate: dict, records: list[dict]) -> dict:
    arms = gate["arms"]
    alpha = gate["test"]["alpha"]
    look = blind_sd(gate, records)
    needed = look.get("rounds_needed") or gate.get("rounds", {}).get("planned") or 2
    results, stop = [], False
    for cand in arms["candidates"]:
        if stop:
            results.append(
                {
                    "candidate": cand,
                    "verdict": "not tested",
                    "why": ["an earlier candidate did not pass (sequential order)"],
                }
            )
            continue
        per_workload, why, complete = [], [], True
        for workload in workloads_of(gate, records):
            paired = pairs(records, workload, arms["control"], cand, gate)
            if len(paired) < needed:
                complete = False
                why.append(f"{workload}: {len(paired)} of {needed} rounds")
            metrics = []
            for spec in gate["metrics"]:
                d, missing = log_ratios(paired, spec["name"])
                res = {"metric": spec["name"], "missing_rounds": missing}
                res |= upper_test(d, spec["margin"], alpha)
                if not res["pass"]:
                    why.append(
                        f"{workload}: {spec['name']} "
                        + (
                            res.get("why")
                            or f"upper bound {res['upper_ratio']:.3f} not under {spec['margin']}"
                        )
                    )
                metrics.append(res)
            breaks = hard_breaks(gate, paired)
            why += [f"{workload}: {b}" for b in breaks]
            per_workload.append(
                {
                    "workload": workload,
                    "rounds": [p[0] for p in paired],
                    "metrics": metrics,
                    "hard": breaks,
                }
            )
        broken = any(w["hard"] for w in per_workload)
        passed = all(m["pass"] for w in per_workload for m in w["metrics"]) and not broken
        # A broken hard rule is final; a bound is judged only on the rounds the gate needs.
        verdict = "fail" if broken else "pass" if passed and complete else "incomplete"
        if complete and not passed:
            verdict = "fail"
        if not per_workload:
            verdict, why = "incomplete", ["no results for this candidate"]
        results.append(
            {"candidate": cand, "verdict": verdict, "why": why, "workloads": per_workload}
        )
        if gate.get("order", {}).get("sequential", True) and verdict != "pass":
            stop = True
    return {
        "kind": "confirmatory",
        "rounds_needed": needed,
        "blind": look,
        "results": results,
        "warnings": warnings(gate, records),
    }


def screen(gate: dict, records: list[dict]) -> dict:
    arms = gate["arms"]
    rule = gate.get("screen", {})
    results = []
    for cand in arms["candidates"]:
        why, figures = [], {}
        for workload in workloads_of(gate, records):
            paired = pairs(records, workload, arms["control"], cand, gate)
            if not paired:
                why.append(f"{workload}: no paired rounds")
                continue
            for metric, limit_key in (
                (rule.get("wake_metric", "wake_same_p99_ms"), "wake_ratio_above"),
                ("makespan_s", "makespan_ratio_above"),
            ):
                d, _ = log_ratios(paired, metric)
                if not d:
                    why.append(f"{workload}: no {metric}")
                    continue
                gm = math.exp(sum(d) / len(d))
                figures[f"{workload}:{metric}"] = {"ratio": gm, "rounds": len(d)}
                limit = rule.get(limit_key)
                if limit is not None and gm > limit:
                    why.append(f"{workload}: {metric} ratio {gm:.3f} above {limit}")
            if rule.get("hard_rules", True):
                why += [f"{workload}: {b}" for b in hard_breaks(gate, paired)]
        results.append(
            {
                "candidate": cand,
                "verdict": "drop" if why else "survives",
                "why": why,
                "figures": figures,
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
    return {"kind": "screen", "results": results, "warnings": warnings(gate, records)}


def aa(gate: dict, records: list[dict]) -> dict:
    arms = gate["arms"]
    rule = gate.get("aa", {})
    level = rule.get("ci", 0.95)
    max_sd = rule.get("max_paired_sd", 0.06)
    primary = rule.get("metric", "makespan_s")
    cand = arms["candidates"][0]
    per = []
    for workload in workloads_of(gate, records):
        paired = pairs(records, workload, arms["control"], cand, gate)
        for spec in gate["metrics"]:
            d, missing = log_ratios(paired, spec["name"])
            res: dict = {
                "workload": workload,
                "metric": spec["name"],
                "n": len(d),
                "missing_rounds": missing,
            }
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
    why = []
    main = [r for r in per if r["metric"] == primary]
    if not main or any("sd_log" not in r for r in main):
        why.append(f"fewer than 2 paired rounds of {primary}")
    for r in main:
        if "sd_log" not in r:
            continue
        if not r["contains_1"]:
            why.append(f"{r['workload']}: the {level:.0%} interval of {primary} leaves out 1")
        if r["sd_log"] > max_sd:
            why.append(f"{r['workload']}: paired SD {r['sd_log']:.3f} above {max_sd}")
    return {
        "kind": "aa",
        "warnings": warnings(gate, records),
        "results": [
            {"candidate": cand, "verdict": "fail" if why else "pass", "why": why, "metrics": per}
        ],
    }


# ---------------------------------------------------------------------------------------------
# The report


def _f(v, nd=3) -> str:
    return "" if v is None else f"{v:.{nd}f}" if isinstance(v, float) else str(v)


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
        *[f"- Warning: {w}" for w in result.get("warnings", [])],
        *([""] if result.get("warnings") else []),
    ]
    if result["kind"] == "confirmatory":
        b = result["blind"]
        lines += [
            f"Rounds needed: {result['rounds_needed']} (pooled paired SD of {b['metric']} over "
            f"the first {b['look_after']} rounds: "
            f"{_f(b['pooled_sd']) or 'too few rounds to measure'}).",
            "",
        ]
    for r in result["results"]:
        lines += [f"## `{r['candidate']}`: {r['verdict']}", ""]
        lines += [f"- {w}" for w in r["why"]] + ([""] if r["why"] else [])
        for w in r.get("workloads", []):
            lines += [
                f"Workload `{w['workload']}`, rounds {', '.join(map(str, w['rounds'])) or 'none'}.",
                "",
                "| metric | n | ratio | upper bound | margin | t | permutation p | verdict |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for m in w["metrics"]:
                lines.append(
                    f"| {m['metric']} | {m['n']} | {_f(m.get('ratio'))} | "
                    f"{_f(m.get('upper_ratio'))} | {m['margin']} | {_f(m.get('t'))} | "
                    f"{_f(m.get('permutation_p'))} | {'pass' if m['pass'] else 'fail'} |"
                )
            lines += [""] + ([f"- hard rule: {h}" for h in w["hard"]] or ["- hard rules: kept"])
            lines.append("")
        if "figures" in r:
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


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="run.py compare", description=__doc__.split("\n\n")[0])
    parser.add_argument("dirs", nargs="+", type=Path, help="results directories")
    parser.add_argument("--gate", type=Path, required=True, help="a bench/gates/*.toml file")
    parser.add_argument(
        "--blind-sd", action="store_true", help="print only the pooled SD for the blind look"
    )
    parser.add_argument(
        "--out", type=Path, help="report path without suffix (default: DIR/compare-<gate name>)"
    )
    args = parser.parse_args(argv)
    gate = load_gate(args.gate)
    records = collect(args.dirs)
    if args.blind_sd:
        b = blind_sd(gate, records)
        sd = b["pooled_sd"]
        sd = "none (fewer than 2 paired rounds)" if sd is None else f"{sd:.4f}"
        print(
            f"pooled paired SD of ln({b['metric']} ratio), first {b['look_after']} rounds, "
            f"{b['paired_rounds']} paired rounds: {sd}"
        )
        if "extend" in b:
            print(
                f"rule: above {b['extend_if_sd_above']} runs {b['rounds_needed']} rounds; "
                + ("extend" if b["extend"] else "do not extend")
            )
        return 0
    result = evaluate(gate, records)
    text = report(gate, result, args.dirs)
    stem = args.out or args.dirs[0] / f"compare-{gate['gate']['name']}"
    stem.with_suffix(".md").write_text(text + "\n")
    stem.with_suffix(".json").write_text(
        json.dumps({"gate": gate["gate"], "arms": gate["arms"]} | result, indent=1)
    )
    print(text)
    return 0
