"""`bench/run.py compare`: finished runs judged by a gate file written before they ran.

    python bench/run.py compare DIR [DIR ...] --gate bench/gates/X.toml
    python bench/run.py compare DIR [DIR ...] --gate bench/gates/X.toml --blind-sd
    python bench/run.py compare AA-DIR --gate bench/gates/aa.toml \
        --plan bench/gates/defaults.toml --arms N [--write bench/gates/defaults.plan.toml]

Provenance first. `compare` refuses results it cannot pool, and says why:

* a results directory whose seed base is not the gate's, whose workload the gate does not
  name, or whose recorded sizes (`spec.json` `args`) differ from the workload's `args`;
* two runs of one condition in one round of one workload (a directory passed twice, or a
  pilot beside a gate);
* runs from more than one commit, checkout state, leasehold version, machine class or machine,
  or directories with different harness settings (`spec.json` `harness`);
* a control and a candidate run of one round with different seeds.

The unit is the round. A round counts for a candidate when its control run and its candidate
run are both valid, the control broke no hard rule (no failed job, no OOM kill, no abort), and
the memory-pressure rule can be judged. A round that does not count is listed with the reason.
The verdict uses exactly the first N rounds that count, N being what the gate asks for; later
rounds are reported as extra and never enter it. In each of the N rounds, each co-primary
metric gives one paired log ratio, d = ln(candidate / control). Over the N rounds come the
mean, the SD and the one-sided upper bound mean + t(1 - alpha, N - 1) * SD / sqrt(N). A metric
passes when exp(upper bound) is under its margin.

A candidate passes only when every metric passes on every workload and it broke no hard rule.
This is an intersection-union test, so the number of metrics needs no correction. The hard
rules are checked on every candidate run up to the last of the N rounds, whether or not its
round counts: no failed job, no OOM kill, no abort other than the harness's own failure, and
memory pressure (PSI `some` max) not above `psi_max` and the control's plus `psi_over_control`
at once. Without both arms' PSI the pressure rule cannot break; the round does not count. A run
replaced by a rerun (under `superseded/`) is listed, and its breaks still count. A broken hard
rule fails a candidate at once. Otherwise, fewer than N rounds, or a metric missing in any of
the N, makes the verdict `incomplete`.

A sign-permutation test of the same hypothesis is reported beside each t bound as a check,
with d shifted by ln(margin). It is exact over all 2^N sign flips up to 12 rounds (else 20 000
seeded draws), and the verdict does not depend on it.

A gate has one of three kinds:

* `confirmatory`: candidates in their listed order; with `sequential = true`, a candidate is
  tested only if the one before it passed. N is the frozen plan's rounds, or more by whole
  Williams cycles after the blind look (below), up to `rounds.extend_to`.
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
raised to the number of workloads. The rule considers only round counts that are whole Williams
cycles of the gate's arms, from `min_rounds` (8) to `max_rounds` (12). It takes the fewest
whose joint power reaches `joint_power` (0.8). If none does, the margins in `widen` (makespan
and job mean, to 1.10) take their wider values and the search runs again. The latency margins
never widen. If the largest count still falls short, the gate runs it and the plan states its
joint power as a known limit. Power comes from the noncentral t distribution, integrated
numerically.

`--plan GATE --arms N` applies GATE's rule to the SDs of an A/A run for a gate of N arms. The
A/A runs one workload, and its SDs stand for all of GATE's. `--write FILE` freezes the plan
there (rounds, margins after widening, the A/A's SDs, the SHA-256 of its results and its
commit), from an A/A that passed. A confirmatory gate names that file in `[plan] frozen`, gives
no verdict without it, and takes its planned rounds and margins from it. The file is checked
first: its cycle, alpha, workloads and target must be the gate's, and the plan recomputed from
its SDs under the gate's rule must match it. With the A/A results at hand (the paths it records,
or `--aa DIR`), their SHA-256 and SDs must be the plan's; without them the report says the A/A
provenance was not checked.

The blind look applies the same rule, with no widening, to the pooled SDs of the first
`rounds.look_after` rounds: the frozen rounds, or more by whole cycles up to `extend_to`. The
look is fixed the first time it is ready and recorded as `blind-<gate>.json` in the first
results directory. `--blind-sd` prints only those pooled SDs and the rounds the rule asks for,
and nothing about which arm is ahead. Otherwise the report is printed, and written as markdown
and JSON to `DIR/compare-<gate name>.md` and `.json`.

Results are matched to the gate by workload (a run's `--workload`) and by condition. A
workload's `params` are added to every arm in it, so `lh-enforced@w=4` in a workload with
`params = "mem=declared"` is `lh-enforced@w=4,mem=declared`.

Everything is computed with the standard library. The t quantile comes from the regularized
incomplete beta function (a continued fraction).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import sys
import tomllib
from itertools import product
from pathlib import Path

import conditions as conds
import schedule

#: The checkout `bench/` is in; plan files record paths relative to it.
ROOT = Path(__file__).resolve().parent.parent

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


def round_counts(least: int, most: int, cycle: int, base: int | None = None) -> list[int]:
    """The round counts a plan may choose, each a whole number of Williams cycles of `cycle`
    rounds: from `least` to `most`; or, with `base`, `base` and whole cycles added to it up to
    `most`."""
    if base is not None:
        return list(range(base, max(base, most) + 1, cycle))
    return [n for n in range(least, most + 1) if n % cycle == 0]


def plan(
    sds: dict,
    metrics: list[dict],
    rule: dict,
    alpha: float,
    workloads: int = 1,
    widen: bool = True,
    cycle: int = 1,
    base: int | None = None,
) -> dict:
    """The gate's rounds and margins for the paired SDs `sds` (metric: SD), by `rule` (a gate's
    `[plan]`). It takes the fewest rounds in `round_counts` whose joint power over every metric
    and all `workloads` reaches `joint_power`. If none does, the margins in `widen` take their
    wider values and the search runs again. If that falls short too, the plan is the largest
    count and states its joint power. A metric without an SD is left out and named. A
    ValueError if no whole number of cycles fits between `min_rounds` and `max_rounds`."""
    target = rule.get("joint_power", 0.8)
    least, most = rule.get("min_rounds", 8), rule.get("max_rounds", 12)
    counts = round_counts(least, most, cycle, base)
    if not counts:
        raise ValueError(
            f"no whole number of Williams cycles of {cycle} rounds lies between {least} and "
            f"{most} rounds; raise max_rounds to at least {-(-least // cycle) * cycle}"
        )
    wider = rule.get("widen", {}) if widen else {}
    measured = [m for m in metrics if sds.get(m["name"]) is not None]
    margins = {m["name"]: m["margin"] for m in measured}

    def search(margins: dict) -> int | None:
        return next(
            (n for n in counts if joint_power(n, sds, margins, alpha, workloads) >= target),
            None,
        )

    rounds, widened = search(margins), []
    if rounds is None and any(k in margins for k in wider):
        widened = [k for k in margins if k in wider]
        margins = margins | {k: wider[k] for k in widened}
        rounds = search(margins)
    reached = rounds is not None
    rounds = rounds if reached else counts[-1]
    return {
        "rounds": rounds,
        "cycle": cycle,
        "counts": counts,
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
    counts = ", ".join(map(str, result["counts"]))
    lines += [
        f"Rounds: {result['rounds']}, chosen from whole Williams cycles of {result['cycle']} "
        f"rounds ({counts}). Joint power, the chance an equal candidate passes every metric on "
        f"{_workloads(result['workloads'])}, taking them as independent: "
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


def data_digest(dirs: list[Path]) -> str:
    """The SHA-256 of every file under `dirs`, by path, apart from what `compare` writes."""
    h = hashlib.sha256()
    for d in dirs:
        for f in sorted(x for x in d.rglob("*") if x.is_file()):
            rel = f.relative_to(d)
            if len(rel.parts) == 1 and rel.name.startswith(("compare-", "blind-")):
                continue
            h.update(f"{d.name}/{rel.as_posix()}\0".encode())
            h.update(f.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


def frozen_text(planned: dict, *, gate: str, arms: int, alpha: float, aa: dict) -> str:
    """The frozen plan file `compare --plan --write` writes: what the gate runs and judges by."""
    lines = [
        f"# The frozen plan of gate `{gate}`, written by `bench/run.py compare --plan --write`",
        "# from an A/A run. The gate takes its rounds and margins from this file; write it",
        "# again rather than editing it.",
        f"gate = {json.dumps(gate)}",
        f"arms = {arms}",
        f"cycle = {planned['cycle']}",
        f"rounds = {planned['rounds']}",
        f"workloads = {planned['workloads']}",
        f"alpha = {alpha}",
        f"target = {planned['target']}",
        f"joint_power = {planned['joint_power']!r}",
        f"reached = {'true' if planned['reached'] else 'false'}",
        f"aa_gate = {json.dumps(aa['gate'])}",
        f"aa_dirs = {json.dumps(aa['dirs'])}",
        f"aa_paths = {json.dumps(aa['paths'])}",
        f"aa_gate_file = {json.dumps(aa['gate_file'])}",
        f"aa_data_sha256 = {json.dumps(aa['digest'])}",
        f"aa_git_sha = {json.dumps(aa['git_sha'] or '')}",
        "",
        "[margins]",
        *[f"{m['metric']} = {m['margin']}" for m in planned["metrics"]],
        "",
        "[sds]",
        *[f"{m['metric']} = {m['sd']!r}" for m in planned["metrics"] if m["sd"] is not None],
    ]
    return "\n".join(lines) + "\n"


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
    if kind == "confirmatory" and gate["plan"]:
        if not gate["plan"].get("frozen"):
            raise SystemExit(f"{path}: [plan] needs frozen, the file compare --plan --write writes")
        apply_frozen(gate, path.parent / gate["plan"]["frozen"])
    return gate


def frozen_mismatch(gate: dict, frozen: dict) -> list[str]:
    """How the frozen plan `frozen` differs from what `gate`'s `[plan]` rule gives at the plan's
    own recorded SDs, for the gate's arm count, alpha and workloads (empty: it does not)."""
    arms = 1 + len(gate["arms"]["candidates"])
    cycle = len(schedule.williams(arms))
    alpha = gate["test"]["alpha"]
    workloads = len(gate["workloads"])
    why = [
        f"{key} is {frozen.get(key)!r}, and the gate's is {want!r}"
        for key, want in (
            ("cycle", cycle),
            ("alpha", alpha),
            ("workloads", workloads),
            ("target", gate["plan"].get("joint_power", 0.8)),
        )
        if frozen.get(key) != want
    ]
    sds = frozen.get("sds", {})
    if not sds:
        return [*why, "it records no SDs"]
    try:
        again = plan(sds, gate["metrics"], gate["plan"], alpha, workloads, cycle=cycle)
    except ValueError as exc:
        return [*why, str(exc)]
    if frozen.get("rounds") != again["rounds"]:
        why.append(f"rounds is {frozen.get('rounds')!r}, and the rule gives {again['rounds']}")
    for m in again["metrics"]:
        if frozen.get("margins", {}).get(m["metric"]) != m["margin"]:
            why.append(
                f"the {m['metric']} margin is {frozen['margins'].get(m['metric'])!r}, and the "
                f"rule gives {m['margin']}"
            )
    if not math.isclose(frozen.get("joint_power", -1.0), again["joint_power"], abs_tol=1e-9):
        why.append(
            f"joint_power is {frozen.get('joint_power')!r}, and the rule gives "
            f"{again['joint_power']!r}"
        )
    if frozen.get("reached") != again["reached"]:
        why.append(f"reached is {frozen.get('reached')!r}, and the rule gives {again['reached']}")
    return why


def check_aa(frozen: dict, given: list[Path] | None) -> str:
    """Check the frozen plan's A/A provenance: with its results (`given`, or the paths the plan
    records, when present), their SHA-256 and paired SDs must be the plan's. Refused if they
    differ; otherwise what was checked, for the report."""
    names = frozen.get("aa_dirs", [])
    dirs = given or [ROOT / p for p in frozen.get("aa_paths", [])]
    if not dirs or not all((d / "spec.json").exists() for d in dirs):
        return (
            f"The A/A provenance was not checked: its results ({', '.join(names) or 'none'}) "
            "were not found. Pass --aa DIR to check them."
        )
    digest = data_digest(dirs)
    if digest != frozen.get("aa_data_sha256"):
        raise Refused(
            f"the A/A results in {', '.join(map(str, dirs))} are not the ones the frozen plan "
            f"was written from (SHA-256 {digest}, the plan records {frozen.get('aa_data_sha256')})"
        )
    aa_gate = load_gate(ROOT / frozen.get("aa_gate_file", "bench/gates/aa.toml"))
    sds = judge(aa_gate, dirs)["sds"]
    recorded = frozen.get("sds", {})
    if {k: v for k, v in sds.items() if v is not None} != recorded:
        raise Refused(f"the A/A results give SDs {sds}, and the frozen plan records {recorded}")
    return (
        f"The A/A provenance was checked: {', '.join(d.name for d in dirs)} match the plan's "
        "SHA-256 and SDs."
    )


def _repo_relative(path: Path) -> str:
    """`path` relative to the checkout when it is inside it, else only its name, so that a
    plan file never records a path on the machine."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def apply_frozen(gate: dict, where: Path) -> None:
    """Set `gate`'s planned rounds and its margins from the frozen plan at `where`; a
    SystemExit if there is none, or it was planned for another gate or another arm count."""
    name = gate["gate"]["name"]
    if not where.exists():
        raise SystemExit(
            f"{where} does not exist: gate {name!r} gives no verdict without its frozen plan. "
            "Write it from the A/A results first: python bench/run.py compare AA-DIR --gate "
            f"bench/gates/aa.toml --plan GATE-FILE --write {where}"
        )
    frozen = tomllib.loads(where.read_text())
    arms = 1 + len(gate["arms"]["candidates"])
    if frozen.get("gate") != name:
        raise SystemExit(f"{where} is the plan of gate {frozen.get('gate')!r}, not {name!r}")
    if frozen.get("arms") != arms:
        raise SystemExit(
            f"{where} was planned for {frozen.get('arms')} arms, and the gate has {arms}; "
            f"write the plan again with --arms {arms}"
        )
    margins = frozen.get("margins", {})
    missing = [m["name"] for m in gate["metrics"] if m["name"] not in margins]
    if missing:
        raise SystemExit(f"{where} has no margin for {', '.join(missing)}")
    if why := frozen_mismatch(gate, frozen):
        raise SystemExit(
            f"{where} is not the plan the gate's [plan] rule gives at its recorded SDs: "
            f"{'; '.join(why)}. Write it again with compare --plan --write; do not edit it"
        )
    for m in gate["metrics"]:
        m["margin"] = margins[m["name"]]
    gate["rounds"]["planned"] = frozen["rounds"]
    gate["frozen_plan"] = frozen | {"path": where.name}


#: The facts of the machine a run ran on that results from one machine share.
MACHINE_FACTS = ("cpus", "memory_bytes", "swap_bytes", "kernel", "virt")


def collect(dirs: list[Path]) -> tuple[list[dict], list[dict]]:
    """(every run under `dirs` that has a manifest, each directory's facts). A run has its
    workload, round, seed, condition, whether it is valid and why not, the code and machine
    it ran on, and, if it has results, its summary row. Runs under `superseded/` are marked so,
    with why they were replaced."""
    import run as bench

    records, infos = [], []
    for d in dirs:
        spec = json.loads((d / "spec.json").read_text())
        workload = spec.get("workload") or spec.get("preset_name")
        seed_base = spec.get("seed_base", spec.get("seed", 1) - 1)
        rounds_seen = 0
        found = [(m, False) for m in sorted(d.glob("*/manifest.json"))]
        found += [(m, True) for m in sorted(d.glob("superseded/*/manifest.json"))]
        for manifest_path, superseded in found:
            m = json.loads(manifest_path.read_text())
            workdir = manifest_path.parent
            has = (workdir / "outer.json").exists()
            row = bench.summarize_condition(workdir, m["label"]) if has else None
            if not superseded:
                rounds_seen = max(rounds_seen, m["round"])
            replaced = ""
            if superseded:
                replaced = m.get("why_invalid") or (
                    "it stopped before it finished" if m.get("valid") is None else "it ran again"
                )
            machine = m.get("machine", {})
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
                    "git_dirty": (m.get("git") or {}).get("dirty"),
                    "leasehold": m.get("leasehold"),
                    "machine_class": machine.get("class"),
                    "machine": [machine.get(k) for k in MACHINE_FACTS],
                    "superseded": superseded,
                    "replaced_why": replaced,
                }
            )
        infos.append(
            {
                "name": d.name,
                "workload": workload,
                "seed_base": seed_base,
                "args": spec.get("args"),
                "harness": spec.get("harness"),
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
    harness = sorted({json.dumps(i.get("harness"), sort_keys=True) for i in infos})
    if len(harness) > 1:
        why.append(
            "the directories ran with different harness settings (--no-scope, --no-wait, "
            f"--drop-caches, --allow-dirty): {'; '.join(harness)}"
        )
    seen: dict[tuple, str] = {}
    for r in records:
        if r.get("superseded"):
            continue
        key = (r["workload"], r["condition"], r["round"])
        if key in seen:
            why.append(
                f"{r['condition']} round {r['round']} of {r['workload']!r} is in both "
                f"{seen[key]} and {r['dir']}"
            )
        seen[key] = r["dir"]
    for field, label in (
        ("git_sha", "commits"),
        ("git_dirty", "states of the checkout (with and without uncommitted changes)"),
        ("leasehold", "leasehold versions"),
        ("machine_class", "machine classes"),
        ("machine", "machines (CPUs, memory, swap, kernel, virtualisation)"),
    ):
        values = sorted({str(r.get(field)) for r in records})
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
    `psi_max` and above the control's in that round plus `psi_over_control`. Without the
    control's PSI it cannot break (`psi_unjudged` makes that round not count)."""
    hard = gate["hard"]
    psi = cand.get("mem_some_max")
    base = (ctrl or {}).get("mem_some_max")
    if "psi_max" not in hard or psi is None or base is None or psi <= hard["psi_max"]:
        return ""
    over = hard.get("psi_over_control", 0.0)
    if psi <= base + over:
        return ""
    return (
        f"memory PSI max {psi}% (control {base}%) above {hard['psi_max']}% and the "
        f"control's plus {over}"
    )


def psi_unjudged(gate: dict, cand: dict | None, ctrl: dict | None) -> str:
    """Why the memory-pressure rule cannot be judged in a round ("": it can): the candidate's
    PSI is missing, or it is above `psi_max` and the control's is missing."""
    hard = gate["hard"]
    if "psi_max" not in hard:
        return ""
    psi = (cand or {}).get("mem_some_max")
    if psi is None:
        return "the candidate's memory PSI is missing"
    if psi > hard["psi_max"] and (ctrl or {}).get("mem_some_max") is None:
        return (
            f"the candidate's memory PSI max {psi}% is above {hard['psi_max']}% and the "
            "control's is missing, so the rule cannot be judged"
        )
    return ""


def _not_counting(gate: dict, c: dict | None, k: dict | None) -> str:
    """Why a round with control run `c` and candidate run `k` does not count ("": it does)."""
    if c is None:
        return "no control run"
    if k is None:
        return "no candidate run"
    if c["row"] and (broke := _breaks(gate, c["row"], True)):
        return "the control broke a hard rule: " + ", ".join(broke)
    if not c["valid"]:
        return f"the control run is invalid: {c['why_invalid']}"
    if not k["valid"]:
        return f"the candidate run is invalid: {k['why_invalid']}"
    return psi_unjudged(gate, k["row"], c["row"])


def window(gate: dict, records: list[dict], workload: str, candidate: str, needed: int) -> dict:
    """The rounds of `candidate` against the control in `workload`: the first `needed` that
    count (`used`, as (round, control row, candidate row)), those that do not and why
    (`invalid`), those after the `needed`th (`extra`), and the hard rules the candidate broke
    in any round up to the last used one (or any round, while fewer than `needed` count). A
    superseded run in those rounds is listed; its hard-rule breaks still count, as the
    candidate's breaks or as a reason the control's round does not count."""
    control = arm_in(gate, workload, gate["arms"]["control"])
    candidate = arm_in(gate, workload, candidate)
    runs: dict[tuple[str, int], dict] = {}
    old: dict[tuple[str, int], list[dict]] = {}
    for r in records:
        if r["workload"] == workload and r["condition"] in (control, candidate):
            if r.get("superseded"):
                old.setdefault((r["condition"], r["round"]), []).append(r)
            else:
                runs[(r["condition"], r["round"])] = r
    used, invalid, extra, hard, psi, superseded = [], [], [], [], [], []
    for rnd in sorted({k[1] for k in runs} | {k[1] for k in old}):
        if len(used) >= needed:
            extra.append(rnd)
            continue
        c, k = runs.get((control, rnd)), runs.get((candidate, rnd))
        if c and k and c["seed"] != k["seed"]:
            raise Refused(
                f"{workload!r} round {rnd}: the control ran seed {c['seed']}, the "
                f"candidate {k['seed']}"
            )
        reason = ""
        for r in old.get((control, rnd), []) + old.get((candidate, rnd), []):
            arm = "control" if r["condition"] == control else "candidate"
            superseded.append({"round": rnd, "arm": arm, "why": r["replaced_why"]})
            broke = _breaks(gate, r["row"], arm == "control") if r["row"] else []
            if broke and arm == "control":
                reason = reason or "a superseded control run broke a hard rule: " + ", ".join(broke)
            hard += [f"round {rnd}: a superseded run: {b}" for b in broke if arm == "candidate"]
        ctrl_row = c["row"] if c else None
        if k and k["row"]:
            hard += [f"round {rnd}: {b}" for b in _breaks(gate, k["row"], False)]
            if why := psi_break(gate, k["row"], ctrl_row):
                hard.append(f"round {rnd}: {why}")
            psi.append((rnd, (ctrl_row or {}).get("mem_some_max"), k["row"].get("mem_some_max")))
        reason = reason or _not_counting(gate, c, k)
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
        "superseded": superseded,
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
        "superseded": w["superseded"],
    }


def cycle_of(gate: dict) -> int:
    """The Williams cycle, in rounds, of the gate's arms (the control and its candidates)."""
    return len(schedule.williams(1 + len(gate["arms"]["candidates"])))


def blind_sd(gate: dict, records: list[dict]) -> dict:
    """The pooled paired SD of each co-primary metric over the first `look_after` rounds that
    count, pooled over candidates and workloads, and the rounds the plan rule asks for at those
    SDs: `planned` and whole Williams cycles added to it, up to `extend_to`, with no margin
    widened. Nothing about which arm is ahead."""
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
    rule = gate["plan"] | {"max_rounds": rounds.get("extend_to", rounds["planned"])}
    planned = plan(
        sds,
        gate["metrics"],
        rule,
        gate["test"]["alpha"],
        len(gate["workloads"]),
        widen=False,
        cycle=cycle_of(gate),
        base=rounds["planned"],
    )
    ready = ready and not planned["missing"]
    return {
        "look_after": look,
        "ready": ready,
        "frozen": False,
        "pooled_sd": sds,
        "rounds_needed": planned["rounds"] if ready else rounds["planned"],
        "plan": planned,
    }


def look_once(gate: dict, dirs: list[Path], records: list[dict]) -> dict:
    """The blind look of a confirmatory gate, fixed the first time it is ready: the one
    recorded in `dirs` (`blind-<gate>.json`), or a new one, recorded in the first directory
    once it is ready. Refused if two directories record different looks."""
    name = f"blind-{gate['gate']['name']}.json"
    found = {d.name: (d / name).read_text() for d in dirs if (d / name).exists()}
    if len({json.dumps(json.loads(t), sort_keys=True) for t in found.values()}) > 1:
        raise Refused(f"the blind looks recorded in {', '.join(found)} differ")
    if found:
        return json.loads(next(iter(found.values()))) | {"frozen": True}
    look = blind_sd(gate, records)
    if look["ready"] and gate["gate"]["kind"] == "confirmatory":
        look["frozen"] = True
        (dirs[0] / name).write_text(json.dumps(look, indent=1))
    return look


def evaluate(gate: dict, records: list[dict]) -> dict:
    kind = gate["gate"]["kind"]
    return {"confirmatory": confirmatory, "screen": screen, "aa": aa}[kind](gate, records)


def confirmatory(gate: dict, records: list[dict]) -> dict:
    alpha = gate["test"]["alpha"]
    look = gate.get("look") or blind_sd(gate, records)
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
    frozen = gate.get("frozen_plan") or {}
    sds = frozen.get("sds") or {}
    margins = {m["name"]: m["margin"] for m in gate["metrics"] if m["name"] in sds}
    power = joint_power(needed, sds, margins, alpha, len(gate["workloads"])) if sds else None
    return {
        "kind": "confirmatory",
        "rounds_needed": needed,
        "blind": look,
        "joint_power": power,
        "plan_file": frozen.get("path"),
        "aa_check": frozen.get("aa_check"),
        "results": results,
    }


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


def balance_notes(infos: list[dict], kind: str = "") -> list[str]:
    """Where the order is not balanced: a directory whose rounds are not a whole number of
    Williams cycles."""
    out = []
    for info in infos:
        if info["arms"] < 2 or not info["rounds_seen"]:
            continue
        cycle = len(schedule.williams(info["arms"]))
        if info["rounds_seen"] % cycle:
            text = (
                f"{info['name']}: {info['rounds_seen']} rounds of {info['arms']} arms are not a "
                f"whole number of Williams cycles ({cycle} rounds). The arms ran in unbalanced "
                "positions and orders, so position and carry-over effects are mixed into the "
                "ratios"
            )
            if kind == "screen":
                text += ", and the screen's thresholds are coarse for that reason"
            out.append(text + ".")
    return out


def judge(gate: dict, dirs: list[Path]) -> dict:
    """The gate's verdict on the results in `dirs`; Refused if they cannot be judged."""
    records, infos = collect(dirs)
    check_provenance(gate, records, infos)
    if gate["gate"]["kind"] == "confirmatory":
        gate = gate | {"look": look_once(gate, dirs, records)}
    code = {k: next((r[k] for r in records), None) for k in ("git_sha", "leasehold")}
    return evaluate(gate, records) | {
        "notes": balance_notes(infos, gate["gate"]["kind"]),
        "code": code,
    }


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
    lines += [
        f"- round {x['round']}: a superseded {x['arm']} run, replaced because {x['why']}"
        for x in w.get("superseded", [])
    ]
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
        if b["ready"]:
            how = f" (the blind look at the pooled SDs of the first {b['look_after']} rounds"
            how += ", fixed when it was first taken)." if b.get("frozen") else ")."
        else:
            how = f" (the blind look needs {b['look_after']} rounds of every candidate)."
        lines += [f"Rounds the verdict uses: {result['rounds_needed']}{how}", ""]
        if result.get("plan_file"):
            lines += [f"Planned rounds and margins come from `{result['plan_file']}`.", ""]
        if result.get("aa_check"):
            lines += [result["aa_check"], ""]
        if result.get("joint_power") is not None:
            lines += [
                f"Joint power at {result['rounds_needed']} rounds, at the frozen plan's A/A "
                f"SDs: {result['joint_power']:.2f}. This is the chance a candidate equal to the "
                "control passes every metric on every workload.",
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
        if b.get("frozen"):
            lines.append("This look is recorded and fixed; later looks print it again.")
    else:
        lines.append(
            f"Not every candidate has {b['look_after']} rounds that count yet, or a metric has "
            "no SD. The gate stays at its planned rounds until a look is ready."
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
        "--arms",
        type=int,
        help="with --plan: the arms the planned gate runs (the control and each candidate), "
        "when its file names no candidates yet",
    )
    parser.add_argument(
        "--write", type=Path, help="with --plan: write the frozen plan the gate runs by here"
    )
    parser.add_argument(
        "--aa",
        type=Path,
        action="append",
        help="the A/A results a confirmatory gate's frozen plan came from, to check them "
        "(default: the paths the plan records); may repeat",
    )
    parser.add_argument(
        "--out", type=Path, help="report path without suffix (default: DIR/compare-<gate name>)"
    )
    args = parser.parse_args(argv)
    if (args.write or args.arms) and not args.plan:
        parser.error("--write and --arms go with --plan")
    gate = load_gate(args.gate)
    try:
        if gate.get("frozen_plan"):
            gate["frozen_plan"]["aa_check"] = check_aa(gate["frozen_plan"], args.aa)
        if args.blind_sd:
            records, infos = collect(args.dirs)
            check_provenance(gate, records, infos)
            print(blind_text(look_once(gate, args.dirs, records)))
            return 0
        result = judge(gate, args.dirs)
    except Refused as exc:
        print(f"compare: refused: {exc}", file=sys.stderr)
        return 2
    text = report(gate, result, args.dirs)
    if args.plan:
        if result["kind"] != "aa":
            parser.error("--plan reads the SDs of an aa gate's results")
        target = _load_plan_only(args.plan)
        arms = args.arms or (1 + len(target["candidates"]) if target["candidates"] else None)
        if not arms or arms < 2:
            parser.error(
                f"{args.plan.name} names no candidates yet; pass --arms, the arms its gate "
                "will run (the control and each candidate)"
            )
        names = [w["name"] for w in target["workloads"]] or ["one workload"]
        try:
            planned = plan(
                result["sds"],
                target["metrics"],
                target["plan"],
                target["test"]["alpha"],
                len(names),
                cycle=len(schedule.williams(arms)),
            )
        except ValueError as exc:
            parser.error(f"{args.plan.name}: {exc}")
        measured = ", ".join(f"`{w}`" for w in workloads_of(gate))
        note = (
            f"For {arms} arms. The A/A measured {measured} only; its SDs stand for every "
            f"workload of the gate ({', '.join(f'`{n}`' for n in names)})."
        )
        text += "\n" + plan_text(planned, f"## Plan for `{args.plan.name}`", note)
        passed = result["results"][0]["verdict"] == "pass"
        if not passed:
            text += "\n\nThe A/A did not pass; fix the bench before planning on its SDs."
        result["plan"] = planned
        if args.write:
            if not passed:
                print(text)
                print("compare: refused: no plan is written from an A/A that did not pass")
                return 2
            aa_facts = {
                "gate": gate["gate"]["name"],
                "gate_file": _repo_relative(args.gate),
                "dirs": [d.name for d in args.dirs],
                "paths": [_repo_relative(d) for d in args.dirs],
                "digest": data_digest(args.dirs),
                "git_sha": result["code"]["git_sha"],
            }
            args.write.write_text(
                frozen_text(
                    planned,
                    gate=target["name"],
                    arms=arms,
                    alpha=target["test"]["alpha"],
                    aa=aa_facts,
                )
            )
            text += f"\n\nThe frozen plan is written to {args.write}."
    stem = args.out or args.dirs[0] / f"compare-{gate['gate']['name']}"
    stem.with_suffix(".md").write_text(text + "\n")
    stem.with_suffix(".json").write_text(
        json.dumps({"gate": gate["gate"], "arms": gate["arms"]} | result, indent=1)
    )
    print(text)
    return 0


def _load_plan_only(path: Path) -> dict:
    """What `--plan` needs of a gate file: its name, candidates, metrics, `[plan]`, alpha and
    workloads. Its candidates may not be filled in yet."""
    raw = tomllib.loads(path.read_text())
    if not raw.get("metrics"):
        raise SystemExit(f"{path}: needs [[metrics]]")
    return {
        "name": raw.get("gate", {}).get("name", path.stem),
        "candidates": raw.get("arms", {}).get("candidates", []),
        "metrics": raw["metrics"],
        "plan": raw.get("plan", {}),
        "test": {"alpha": raw.get("test", {}).get("alpha", 0.05)},
        "workloads": raw.get("workloads", []),
    }
