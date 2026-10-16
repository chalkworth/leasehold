#!/usr/bin/env python3
"""Interval, sample-size, paired, multiplicity, stopping and Beta-Binomial helpers for comparing runs.

Needs numpy and scipy. Run from anywhere with:
    uv run --no-project --with numpy --with scipy python stats.py <command> ... (prints JSON)

Commands (run `stats.py <command> --help` for the arguments):
    wilson, clopper-pearson, rule-of-three       interval for a rate from successes out of trials
    paired-bootstrap, cluster-bootstrap          interval for a mean paired difference, or a mean over clusters
    mcnemar                                      exact test on the discordant pairs of a paired pass/fail comparison
    holm                                         Holm-adjusted p-values
    seeds-for-rate, pairs-for-paired-rate        runs needed to see a change in a rate, unpaired or paired
    pairs-for-delta, mdd, mdd-rate               pairs for a paired mean delta; smallest difference n can see
    design-effect                                variance inflation for runs that share a cluster
    boundaries                                   group-sequential stopping boundaries (O'Brien-Fleming or Pocock)
    beta-posterior, prob-b-beats-a               Beta-Binomial posterior for a rate, and P(B beats A)
    noise-floor                                  the smallest candidate-minus-baseline gap that clears baseline spread

Library-only (no command): simulate_power, paired_seeds, generate_paired_binary_outcomes, exact_rate_power,
attempts_since_improvement.

Rates are fractions (0.10 is 10%). Every test is two-sided unless stated.

Examples:
    stats.py wilson --successes 8 --trials 100
    stats.py seeds-for-rate --baseline-rate 0.10 --minimum-detectable-difference 0.02
    stats.py paired-bootstrap --baseline 0.61 0.58 0.64 --candidate 0.66 0.60 0.65
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from enum import Enum

import numpy as np
from scipy import optimize, stats

DEFAULT_CONFIDENCE = 0.95
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.80
DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 0
DEFAULT_SIMULATIONS = 2_000
DEFAULT_POSTERIOR_DRAWS = 100_000
GRID_CELLS_FOR_BOUNDARIES = 1_500
MAXIMUM_SEARCH_SAMPLE_SIZE = 10_000_000
MINIMUM_SEARCH_SAMPLE_SIZE = 2
MAXIMUM_STANDARDIZED_EFFECT = 64.0
INITIAL_STANDARDIZED_EFFECT = 0.0625
JSON_INDENT = 2


class ChangeDirection(str, Enum):
    """Which way the candidate is expected to move a rate."""

    DECREASE = "decrease"
    INCREASE = "increase"


class Command(str, Enum):
    """Command line subcommands."""

    WILSON = "wilson"
    CLOPPER_PEARSON = "clopper-pearson"
    RULE_OF_THREE = "rule-of-three"
    PAIRED_BOOTSTRAP = "paired-bootstrap"
    CLUSTER_BOOTSTRAP = "cluster-bootstrap"
    MCNEMAR = "mcnemar"
    HOLM = "holm"
    SEEDS_FOR_RATE = "seeds-for-rate"
    PAIRS_FOR_PAIRED_RATE = "pairs-for-paired-rate"
    PAIRS_FOR_DELTA = "pairs-for-delta"
    MDD = "mdd"
    MDD_RATE = "mdd-rate"
    DESIGN_EFFECT = "design-effect"
    BOUNDARIES = "boundaries"
    BETA_POSTERIOR = "beta-posterior"
    PROB_B_BEATS_A = "prob-b-beats-a"
    NOISE_FLOOR = "noise-floor"


class BoundaryFamily(str, Enum):
    """Group-sequential boundary shapes."""

    OBRIEN_FLEMING = "obrien-fleming"
    POCOCK = "pocock"


class BetterDirection(str, Enum):
    """Whether a larger or a smaller value of a metric is better."""

    HIGHER = "higher"
    LOWER = "lower"


@dataclass(frozen=True)
class IntervalResult:
    """A point estimate with a two-sided interval."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    method: str
    successes: int | None = None
    trials: int | None = None


@dataclass(frozen=True)
class BootstrapResult:
    """A bootstrap interval for a mean paired difference or a cluster-level statistic."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    unit_count: int
    resamples: int
    seed: int
    method: str


@dataclass(frozen=True)
class McNemarResult:
    """Exact McNemar test on the discordant pairs."""

    baseline_only_failures: int
    candidate_only_failures: int
    discordant_pairs: int
    p_value: float


@dataclass(frozen=True)
class SampleSizeResult:
    """A planned number of independent units and the assumptions behind it."""

    units_needed: int
    power: float
    alpha: float
    method: str
    assumptions: dict[str, float]
    design_effect_applied: float = 1.0
    exact_power_at_units: float | None = None


@dataclass(frozen=True)
class MinimumDetectableResult:
    """The smallest difference a fixed number of units can detect."""

    minimum_detectable_difference: float
    units: int
    power: float
    alpha: float
    method: str


@dataclass(frozen=True)
class PowerEstimate:
    """Monte Carlo power with a Wilson interval on the simulation error."""

    power: float
    lower: float
    upper: float
    simulations: int
    sample_size: int


@dataclass(frozen=True)
class SequentialBoundaries:
    """Two-sided z boundaries at equally or explicitly spaced looks, with the alpha spent at each look."""

    family: str
    alpha: float
    information_fractions: list[float]
    z_boundaries: list[float]
    nominal_p_values: list[float]
    alpha_spent_at_each_look: list[float]


@dataclass(frozen=True)
class BetaPosterior:
    """A Beta posterior for a rate."""

    alpha: float
    beta: float
    mean: float
    lower: float
    upper: float
    credible_level: float


@dataclass(frozen=True)
class NoiseFloor:
    """How far a single candidate run must sit from the baseline runs to be more than baseline spread."""

    baseline_runs: int
    baseline_mean: float
    baseline_standard_deviation: float
    noise_floor: float
    confidence: float
    method: str


# ---------------------------------------------------------------------------------------------------------------
# Intervals for rates
# ---------------------------------------------------------------------------------------------------------------


def _check_counts(successes: int, trials: int) -> None:
    if trials <= 0:
        raise ValueError("trials must be positive")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between 0 and trials")


def _check_open_unit_interval(value: float, name: str) -> None:
    if not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be strictly between 0 and 1")


def wilson_interval(
    successes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE
) -> IntervalResult:
    """Wilson score interval for a rate. The default for a rate from a small or rare-event count."""
    _check_counts(successes, trials)
    _check_open_unit_interval(confidence, "confidence")
    z_value = float(stats.norm.ppf(0.5 + confidence / 2.0))
    proportion = successes / trials
    z_squared = z_value**2
    denominator = 1.0 + z_squared / trials
    centre = (proportion + z_squared / (2.0 * trials)) / denominator
    half_width = (z_value / denominator) * math.sqrt(
        proportion * (1.0 - proportion) / trials + z_squared / (4.0 * trials**2)
    )
    # At 0 or n successes the formula's bound equals the estimate up to rounding; return it exactly.
    lower = 0.0 if successes == 0 else min(proportion, max(0.0, centre - half_width))
    upper = 1.0 if successes == trials else max(proportion, min(1.0, centre + half_width))
    return IntervalResult(
        estimate=proportion,
        lower=lower,
        upper=upper,
        confidence=confidence,
        method="wilson",
        successes=successes,
        trials=trials,
    )


def clopper_pearson_interval(
    successes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE
) -> IntervalResult:
    """Exact (conservative) binomial interval. Use when the claim is a bound, such as zero failures in n runs."""
    _check_counts(successes, trials)
    _check_open_unit_interval(confidence, "confidence")
    tail = (1.0 - confidence) / 2.0
    if successes == 0:
        lower = 0.0
    else:
        lower = float(stats.beta.ppf(tail, successes, trials - successes + 1))
    if successes == trials:
        upper = 1.0
    else:
        upper = float(stats.beta.ppf(1.0 - tail, successes + 1, trials - successes))
    return IntervalResult(
        estimate=successes / trials,
        lower=lower,
        upper=upper,
        confidence=confidence,
        method="clopper-pearson",
        successes=successes,
        trials=trials,
    )


def rule_of_three_upper_bound(trials: int, confidence: float = DEFAULT_CONFIDENCE) -> float:
    """Upper bound on a rate after zero events in `trials` independent trials: -ln(1 - confidence) / trials.

    At 95% confidence this is the familiar 3 / trials (2.996 / trials).
    """
    if trials <= 0:
        raise ValueError("trials must be positive")
    _check_open_unit_interval(confidence, "confidence")
    return -math.log(1.0 - confidence) / trials


# ---------------------------------------------------------------------------------------------------------------
# Paired and clustered resampling, paired tests, multiplicity
# ---------------------------------------------------------------------------------------------------------------


def _percentile_interval(samples: np.ndarray, confidence: float) -> tuple[float, float]:
    tail = (1.0 - confidence) / 2.0 * 100.0
    lower, upper = np.percentile(samples, [tail, 100.0 - tail])
    return float(lower), float(upper)


def paired_bootstrap_delta(
    baseline: Sequence[float],
    candidate: Sequence[float],
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES,
    confidence: float = DEFAULT_CONFIDENCE,
) -> BootstrapResult:
    """Percentile bootstrap interval for the mean of (candidate - baseline) over matched units.

    The i-th baseline and candidate value must come from the same seed, scenario or frame. If the units sit in clips
    or other clusters, pass the deltas to `cluster_bootstrap_ci` instead.
    """
    baseline_array = np.asarray(baseline, dtype=float)
    candidate_array = np.asarray(candidate, dtype=float)
    if baseline_array.shape != candidate_array.shape or baseline_array.ndim != 1:
        raise ValueError("baseline and candidate must be one-dimensional and the same length")
    if baseline_array.size < 2:
        raise ValueError("need at least two pairs")
    _check_open_unit_interval(confidence, "confidence")
    deltas = candidate_array - baseline_array
    generator = np.random.default_rng(seed)
    indices = generator.integers(0, deltas.size, size=(resamples, deltas.size))
    resampled_means = deltas[indices].mean(axis=1)
    lower, upper = _percentile_interval(resampled_means, confidence)
    return BootstrapResult(
        estimate=float(deltas.mean()),
        lower=lower,
        upper=upper,
        confidence=confidence,
        unit_count=int(deltas.size),
        resamples=resamples,
        seed=seed,
        method="paired-bootstrap-percentile",
    )


def cluster_bootstrap_ci(
    values: Sequence[float],
    cluster_ids: Sequence[object],
    statistic: Callable[[np.ndarray], float] = np.mean,
    seed: int = DEFAULT_SEED,
    resamples: int = DEFAULT_RESAMPLES // 2,
    confidence: float = DEFAULT_CONFIDENCE,
) -> BootstrapResult:
    """Percentile bootstrap that resamples whole clusters (clips, scenarios) with replacement.

    Rows inside a cluster stay together, so the interval reflects the number of clusters, not the number of rows.
    With fewer than about 10 clusters the interval is too narrow; say so in the report.
    """
    value_array = np.asarray(values, dtype=float)
    if value_array.ndim != 1 or value_array.size != len(cluster_ids):
        raise ValueError("values and cluster_ids must be the same length")
    _check_open_unit_interval(confidence, "confidence")
    members_by_cluster: dict[object, list[int]] = {}
    for row_index, cluster_id in enumerate(cluster_ids):
        members_by_cluster.setdefault(cluster_id, []).append(row_index)
    clusters = [value_array[member_rows] for member_rows in members_by_cluster.values()]
    if len(clusters) < 2:
        raise ValueError("need at least two clusters")
    generator = np.random.default_rng(seed)
    resampled_statistics = np.empty(resamples)
    for resample_index in range(resamples):
        chosen = generator.integers(0, len(clusters), size=len(clusters))
        resampled_statistics[resample_index] = statistic(
            np.concatenate([clusters[index] for index in chosen])
        )
    lower, upper = _percentile_interval(resampled_statistics, confidence)
    return BootstrapResult(
        estimate=float(statistic(value_array)),
        lower=lower,
        upper=upper,
        confidence=confidence,
        unit_count=len(clusters),
        resamples=resamples,
        seed=seed,
        method="cluster-bootstrap-percentile",
    )


def mcnemar_exact(baseline_only_failures: int, candidate_only_failures: int) -> McNemarResult:
    """Exact two-sided McNemar test for paired pass/fail results.

    `baseline_only_failures` counts pairs where only the baseline failed; `candidate_only_failures` where only the
    candidate failed. Pairs that agree carry no information and are not needed.
    """
    if baseline_only_failures < 0 or candidate_only_failures < 0:
        raise ValueError("counts must be non-negative")
    discordant_pairs = baseline_only_failures + candidate_only_failures
    if discordant_pairs == 0:
        p_value = 1.0
    else:
        p_value = float(
            stats.binomtest(
                min(baseline_only_failures, candidate_only_failures), discordant_pairs, 0.5
            ).pvalue
        )
    return McNemarResult(baseline_only_failures, candidate_only_failures, discordant_pairs, p_value)


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, returned in the input order. Controls the family-wise error rate."""
    p_array = np.asarray(p_values, dtype=float)
    if p_array.ndim != 1 or p_array.size == 0:
        raise ValueError("p_values must be a non-empty one-dimensional sequence")
    if np.any(p_array < 0.0) or np.any(p_array > 1.0):
        raise ValueError("p-values must lie in [0, 1]")
    comparisons = p_array.size
    order = np.argsort(p_array)
    adjusted = np.empty(comparisons)
    running_maximum = 0.0
    for rank, original_index in enumerate(order):
        candidate_value = (comparisons - rank) * p_array[original_index]
        running_maximum = max(running_maximum, candidate_value)
        adjusted[original_index] = min(1.0, running_maximum)
    return [float(value) for value in adjusted]


# ---------------------------------------------------------------------------------------------------------------
# Sample size and minimum detectable difference
# ---------------------------------------------------------------------------------------------------------------


def design_effect(cluster_size: float, intraclass_correlation: float) -> float:
    """Variance inflation 1 + (m - 1) * ICC for equal clusters of size m. A planning approximation only.

    Multiply the independent-unit sample size by this. It does not fit unequal clusters or very few clusters;
    simulate those.
    """
    if cluster_size < 1.0:
        raise ValueError("cluster_size must be at least 1")
    if not 0.0 <= intraclass_correlation <= 1.0:
        raise ValueError("intraclass_correlation must be between 0 and 1")
    return 1.0 + (cluster_size - 1.0) * intraclass_correlation


def _candidate_rate(
    baseline_rate: float, minimum_detectable_difference: float, direction: ChangeDirection
) -> float:
    if minimum_detectable_difference <= 0.0:
        raise ValueError("minimum_detectable_difference must be positive")
    if direction is ChangeDirection.DECREASE:
        candidate_rate = baseline_rate - minimum_detectable_difference
    else:
        candidate_rate = baseline_rate + minimum_detectable_difference
    if not 0.0 <= candidate_rate <= 1.0:
        raise ValueError("the candidate rate falls outside [0, 1]")
    return candidate_rate


def _normal_quantiles(power: float, alpha: float) -> tuple[float, float]:
    _check_open_unit_interval(power, "power")
    _check_open_unit_interval(alpha, "alpha")
    return float(stats.norm.ppf(1.0 - alpha / 2.0)), float(stats.norm.ppf(power))


def required_seeds_for_rate_difference(
    baseline_rate: float,
    minimum_detectable_difference: float,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
    direction: ChangeDirection = ChangeDirection.DECREASE,
    design_effect_multiplier: float = 1.0,
) -> SampleSizeResult:
    """Independent runs per arm to detect a change in a rate with two UNPAIRED arms.

    Uses the two-proportion normal formula with unpooled variance under the alternative (Fleiss), then reports the
    exact power of the pooled z-test at that n. 10% -> 8% at alpha 0.05 and 80% power gives 3,213 per arm. Paired
    seeds change this a great deal: see `required_pairs_for_paired_rate_difference`.
    """
    candidate_rate = _candidate_rate(baseline_rate, minimum_detectable_difference, direction)
    z_alpha, z_power = _normal_quantiles(power, alpha)
    mean_rate = (baseline_rate + candidate_rate) / 2.0
    numerator = (
        z_alpha * math.sqrt(2.0 * mean_rate * (1.0 - mean_rate))
        + z_power
        * math.sqrt(baseline_rate * (1.0 - baseline_rate) + candidate_rate * (1.0 - candidate_rate))
    ) ** 2
    units_without_clustering = math.ceil(numerator / minimum_detectable_difference**2)
    units_needed = math.ceil(units_without_clustering * design_effect_multiplier)
    exact_power = exact_rate_power(units_without_clustering, baseline_rate, candidate_rate, alpha)
    return SampleSizeResult(
        units_needed=units_needed,
        power=power,
        alpha=alpha,
        method="two-proportion normal approximation, unpaired",
        assumptions={"baseline_rate": baseline_rate, "candidate_rate": candidate_rate},
        design_effect_applied=design_effect_multiplier,
        exact_power_at_units=exact_power,
    )


def exact_rate_power(
    units_per_arm: int, baseline_rate: float, candidate_rate: float, alpha: float = DEFAULT_ALPHA
) -> float:
    """Exact power of the two-sided pooled z-test for two independent binomial arms, by enumerating all outcomes."""
    if units_per_arm < 1:
        raise ValueError("units_per_arm must be at least 1")
    critical_value = float(stats.norm.ppf(1.0 - alpha / 2.0))
    outcomes = np.arange(units_per_arm + 1)
    baseline_probabilities = stats.binom.pmf(outcomes, units_per_arm, baseline_rate)
    candidate_probabilities = stats.binom.pmf(outcomes, units_per_arm, candidate_rate)
    rejection_probability = 0.0
    for baseline_count in outcomes:
        pooled = (baseline_count + outcomes) / (2.0 * units_per_arm)
        standard_error = np.sqrt(pooled * (1.0 - pooled) * 2.0 / units_per_arm)
        difference = (outcomes - baseline_count) / units_per_arm
        with np.errstate(divide="ignore", invalid="ignore"):
            z_scores = np.where(standard_error > 0.0, np.abs(difference) / standard_error, 0.0)
        rejection_probability += baseline_probabilities[baseline_count] * float(
            candidate_probabilities[z_scores > critical_value].sum()
        )
    return rejection_probability


def required_pairs_for_paired_rate_difference(
    discordant_rate: float,
    minimum_detectable_difference: float,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
) -> SampleSizeResult:
    """Pairs needed for a paired pass/fail comparison (McNemar, normal approximation; Connor 1987).

    `discordant_rate` is the fraction of pairs where exactly one arm failed, measured in a pilot with the same shared
    seeds. It must be at least `minimum_detectable_difference` (the net change in failure rate).
    """
    if minimum_detectable_difference <= 0.0:
        raise ValueError("minimum_detectable_difference must be positive")
    if not minimum_detectable_difference <= discordant_rate <= 1.0:
        raise ValueError("discordant_rate must lie between minimum_detectable_difference and 1")
    z_alpha, z_power = _normal_quantiles(power, alpha)
    numerator = (
        z_alpha * math.sqrt(discordant_rate)
        + z_power * math.sqrt(discordant_rate - minimum_detectable_difference**2)
    ) ** 2
    return SampleSizeResult(
        units_needed=math.ceil(numerator / minimum_detectable_difference**2),
        power=power,
        alpha=alpha,
        method="McNemar normal approximation, paired",
        assumptions={
            "discordant_rate": discordant_rate,
            "minimum_detectable_difference": minimum_detectable_difference,
        },
    )


def _t_test_power(standardized_effect: float, units: int, alpha: float, paired: bool) -> float:
    """Exact power of the two-sided t-test for a standardized effect (difference / standard deviation)."""
    if paired:
        degrees_of_freedom = units - 1
        noncentrality = standardized_effect * math.sqrt(units)
    else:
        degrees_of_freedom = 2 * units - 2
        noncentrality = standardized_effect * math.sqrt(units / 2.0)
    critical_value = float(stats.t.ppf(1.0 - alpha / 2.0, degrees_of_freedom))
    upper_tail = float(stats.nct.sf(critical_value, degrees_of_freedom, noncentrality))
    lower_tail = float(stats.nct.cdf(-critical_value, degrees_of_freedom, noncentrality))
    total = upper_tail + lower_tail
    if math.isnan(
        total
    ):  # scipy's noncentral t can fail for a large noncentrality; power is then effectively 1
        return 1.0 if abs(noncentrality) > critical_value else 0.0
    return total


def required_pairs_for_paired_delta(
    paired_delta_sd: float,
    minimum_detectable_delta: float,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
) -> SampleSizeResult:
    """Pairs needed to detect a mean paired difference, from the standard deviation of the per-pair DIFFERENCES.

    Exact noncentral-t search. The standard deviation must come from paired differences in a pilot, not from the
    spread of each arm. d = 0.5 gives 34 pairs.
    """
    if paired_delta_sd <= 0.0 or minimum_detectable_delta <= 0.0:
        raise ValueError("paired_delta_sd and minimum_detectable_delta must be positive")
    _check_open_unit_interval(power, "power")
    standardized_effect = minimum_detectable_delta / paired_delta_sd
    units = _smallest_units_reaching_power(
        lambda candidate_units: _t_test_power(
            standardized_effect, candidate_units, alpha, paired=True
        ),
        power,
    )
    return SampleSizeResult(
        units_needed=units,
        power=power,
        alpha=alpha,
        method="paired t-test, exact noncentral t",
        assumptions={
            "paired_delta_sd": paired_delta_sd,
            "minimum_detectable_delta": minimum_detectable_delta,
        },
        exact_power_at_units=_t_test_power(standardized_effect, units, alpha, paired=True),
    )


def _smallest_units_reaching_power(
    power_at_units: Callable[[int], float], target_power: float
) -> int:
    upper = MINIMUM_SEARCH_SAMPLE_SIZE
    while power_at_units(upper) < target_power:
        upper *= 2
        if upper > MAXIMUM_SEARCH_SAMPLE_SIZE:
            raise ValueError(
                "the effect is too small to reach the target power at a feasible sample size"
            )
    lower = MINIMUM_SEARCH_SAMPLE_SIZE
    while lower < upper:
        middle = (lower + upper) // 2
        if power_at_units(middle) >= target_power:
            upper = middle
        else:
            lower = middle + 1
    return lower


def minimum_detectable_difference(
    standard_deviation: float,
    units: int,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
    paired: bool = False,
) -> MinimumDetectableResult:
    """Smallest mean difference `units` runs per arm (or pairs) can detect, in the metric's own units.

    Paired: pass the standard deviation of the per-pair differences. Unpaired: the within-arm standard deviation
    (Welch/Student t, equal n). Report this beside the observed difference instead of observed power.
    """
    if standard_deviation <= 0.0:
        raise ValueError("standard_deviation must be positive")
    if units < 2:
        raise ValueError("units must be at least 2")
    _check_open_unit_interval(power, "power")
    upper_effect = INITIAL_STANDARDIZED_EFFECT
    while _t_test_power(upper_effect, units, alpha, paired) < power:
        upper_effect *= 2.0
        if upper_effect > MAXIMUM_STANDARDIZED_EFFECT:
            raise ValueError(
                "this sample size cannot reach the target power for any plausible effect"
            )
    standardized = optimize.brentq(
        lambda effect: _t_test_power(effect, units, alpha, paired) - power,
        1e-6,
        upper_effect,
        xtol=1e-10,
    )
    return MinimumDetectableResult(
        minimum_detectable_difference=float(standardized) * standard_deviation,
        units=units,
        power=power,
        alpha=alpha,
        method="paired t-test, exact noncentral t"
        if paired
        else "two-sample t-test, exact noncentral t",
    )


def minimum_detectable_rate_difference(
    baseline_rate: float,
    units_per_arm: int,
    power: float = DEFAULT_POWER,
    alpha: float = DEFAULT_ALPHA,
    direction: ChangeDirection = ChangeDirection.DECREASE,
) -> MinimumDetectableResult:
    """Smallest change in a rate that `units_per_arm` independent runs per arm can detect (unpaired, normal formula)."""
    if units_per_arm < 2:
        raise ValueError("units_per_arm must be at least 2")
    z_alpha, z_power = _normal_quantiles(power, alpha)
    sign = -1.0 if direction is ChangeDirection.DECREASE else 1.0
    upper_limit = baseline_rate if direction is ChangeDirection.DECREASE else 1.0 - baseline_rate

    def required_units_minus_available(difference: float) -> float:
        candidate_rate = baseline_rate + sign * difference
        mean_rate = (baseline_rate + candidate_rate) / 2.0
        numerator = (
            z_alpha * math.sqrt(2.0 * mean_rate * (1.0 - mean_rate))
            + z_power
            * math.sqrt(
                baseline_rate * (1.0 - baseline_rate) + candidate_rate * (1.0 - candidate_rate)
            )
        ) ** 2
        return numerator / difference**2 - units_per_arm

    if required_units_minus_available(upper_limit) > 0.0:
        raise ValueError("no change in this direction is detectable at this sample size")
    difference = optimize.brentq(required_units_minus_available, 1e-9, upper_limit, xtol=1e-12)
    return MinimumDetectableResult(
        minimum_detectable_difference=float(difference),
        units=units_per_arm,
        power=power,
        alpha=alpha,
        method="two-proportion normal approximation, unpaired",
    )


def simulate_power(
    generate_and_test: Callable[[np.random.Generator, int], bool],
    sample_size: int,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: int = DEFAULT_SEED,
) -> PowerEstimate:
    """Monte Carlo power (or false-positive rate, if the data are generated under the null).

    `generate_and_test(generator, sample_size)` draws one whole experiment, applies the planned analysis with its alpha
    inside, and returns True when it rejects. Report the interval: it is the simulation error only. Check a null
    scenario first, and confirm a chosen n with a fresh seed.
    """
    if simulations < 1 or sample_size < 1:
        raise ValueError("simulations and sample_size must be positive")
    generator = np.random.default_rng(seed)
    rejections = sum(1 for _ in range(simulations) if generate_and_test(generator, sample_size))
    interval = wilson_interval(rejections, simulations)
    return PowerEstimate(
        power=rejections / simulations,
        lower=interval.lower,
        upper=interval.upper,
        simulations=simulations,
        sample_size=sample_size,
    )


def paired_seeds(base_seed: int, count: int) -> list[int]:
    """`count` distinct seeds derived from `base_seed`. Give the SAME list to every arm so runs are paired."""
    if count < 1:
        raise ValueError("count must be at least 1")
    children = np.random.SeedSequence(base_seed).spawn(count)
    return [int(child.generate_state(1, dtype=np.uint32)[0]) for child in children]


def generate_paired_binary_outcomes(
    generator: np.random.Generator,
    pairs: int,
    baseline_failure_rate: float,
    candidate_failure_rate: float,
    shared_fraction: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Failure flags for two arms run on shared seeds, for `simulate_power`.

    Each pair shares one uniform draw (the scenario and seed). With probability `shared_fraction` the candidate reuses
    it, so both arms fail on the same hard cases; otherwise the candidate draws its own. shared_fraction 0 is
    unpaired, 1 is perfectly paired. Take the real value from a pilot's discordant rate, not from a guess.
    """
    if not 0.0 <= shared_fraction <= 1.0:
        raise ValueError("shared_fraction must be between 0 and 1")
    shared_draws = generator.random(pairs)
    own_draws = generator.random(pairs)
    uses_shared = generator.random(pairs) < shared_fraction
    candidate_draws = np.where(uses_shared, shared_draws, own_draws)
    return shared_draws < baseline_failure_rate, candidate_draws < candidate_failure_rate


# ---------------------------------------------------------------------------------------------------------------
# Group-sequential stopping boundaries
# ---------------------------------------------------------------------------------------------------------------


def _crossing_probabilities(
    information_fractions: np.ndarray, z_boundaries: np.ndarray
) -> np.ndarray:
    """P(first |Z_k| >= c_k at look k) under the null, by numerical integration of the score process."""
    cells = GRID_CELLS_FOR_BOUNDARIES
    crossing = np.zeros(information_fractions.size)
    scale = math.sqrt(information_fractions[0])
    edge = z_boundaries[0] * scale
    centres = (np.arange(cells) + 0.5) / cells * 2.0 * edge - edge
    width = 2.0 * edge / cells
    density = stats.norm.pdf(centres, scale=scale)
    surviving = float(density.sum() * width)
    crossing[0] = 1.0 - surviving
    for look in range(1, information_fractions.size):
        step_scale = math.sqrt(information_fractions[look] - information_fractions[look - 1])
        edge = z_boundaries[look] * math.sqrt(information_fractions[look])
        new_centres = (np.arange(cells) + 0.5) / cells * 2.0 * edge - edge
        new_width = 2.0 * edge / cells
        transition = stats.norm.pdf(new_centres[:, None] - centres[None, :], scale=step_scale)
        density = transition @ (density * width)
        centres, width = new_centres, new_width
        previously_surviving = surviving
        surviving = float(density.sum() * width)
        crossing[look] = previously_surviving - surviving
    return crossing


def group_sequential_boundaries(
    looks: int = 2,
    alpha: float = DEFAULT_ALPHA,
    family: BoundaryFamily = BoundaryFamily.OBRIEN_FLEMING,
    information_fractions: Sequence[float] | None = None,
) -> SequentialBoundaries:
    """Two-sided z boundaries so the overall false-positive rate across all looks is `alpha`.

    O'Brien-Fleming: c_k = C / sqrt(t_k), very strict early. Pocock: the same c at every look. `looks` equally spaced
    looks, or pass `information_fractions` ending at 1.0. Two equal looks at alpha 0.05 give 2.797 and 1.977
    (O'Brien-Fleming) or 2.178 and 2.178 (Pocock). Stop only when |z| passes the boundary at that look.
    """
    _check_open_unit_interval(alpha, "alpha")
    if information_fractions is None:
        if looks < 1:
            raise ValueError("looks must be at least 1")
        fractions = np.arange(1, looks + 1) / looks
    else:
        fractions = np.asarray(information_fractions, dtype=float)
        if (
            fractions.size < 1
            or np.any(np.diff(fractions) <= 0.0)
            or fractions[0] <= 0.0
            or fractions[-1] != 1.0
        ):
            raise ValueError("information_fractions must increase from above 0 and end at 1.0")

    def boundaries_for(constant: float) -> np.ndarray:
        if family is BoundaryFamily.OBRIEN_FLEMING:
            return constant / np.sqrt(fractions)
        return np.full(fractions.size, constant)

    def total_error_minus_alpha(constant: float) -> float:
        return float(_crossing_probabilities(fractions, boundaries_for(constant)).sum()) - alpha

    constant = optimize.brentq(total_error_minus_alpha, 1.0, 6.0, xtol=1e-9)
    z_boundaries = boundaries_for(constant)
    spent = _crossing_probabilities(fractions, z_boundaries)
    return SequentialBoundaries(
        family=family.value,
        alpha=alpha,
        information_fractions=[float(value) for value in fractions],
        z_boundaries=[float(value) for value in z_boundaries],
        nominal_p_values=[float(2.0 * stats.norm.sf(value)) for value in z_boundaries],
        alpha_spent_at_each_look=[float(value) for value in spent],
    )


# ---------------------------------------------------------------------------------------------------------------
# Beta-Binomial helpers
# ---------------------------------------------------------------------------------------------------------------


def beta_posterior(
    successes: int,
    trials: int,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    credible_level: float = DEFAULT_CONFIDENCE,
) -> BetaPosterior:
    """Beta posterior for a rate after `successes` of `trials`, with a Beta(prior_alpha, prior_beta) prior.

    State the prior in the report. Beta(1, 1) is uniform; Beta(0.5, 0.5) is Jeffreys. A prior from earlier runs is
    only honest if those runs are exchangeable with the new ones.
    """
    _check_counts(successes, trials)
    if prior_alpha <= 0.0 or prior_beta <= 0.0:
        raise ValueError("prior parameters must be positive")
    _check_open_unit_interval(credible_level, "credible_level")
    posterior_alpha = prior_alpha + successes
    posterior_beta = prior_beta + trials - successes
    tail = (1.0 - credible_level) / 2.0
    return BetaPosterior(
        alpha=posterior_alpha,
        beta=posterior_beta,
        mean=posterior_alpha / (posterior_alpha + posterior_beta),
        lower=float(stats.beta.ppf(tail, posterior_alpha, posterior_beta)),
        upper=float(stats.beta.ppf(1.0 - tail, posterior_alpha, posterior_beta)),
        credible_level=credible_level,
    )


def probability_difference_beyond_margin(
    posterior_a: BetaPosterior,
    posterior_b: BetaPosterior,
    margin: float = 0.0,
    better: BetterDirection = BetterDirection.HIGHER,
    seed: int = DEFAULT_SEED,
    draws: int = DEFAULT_POSTERIOR_DRAWS,
) -> float:
    """P(B is better than A by more than `margin`) under independent Beta posteriors, by Monte Carlo."""
    generator = np.random.default_rng(seed)
    draws_a = generator.beta(posterior_a.alpha, posterior_a.beta, size=draws)
    draws_b = generator.beta(posterior_b.alpha, posterior_b.beta, size=draws)
    if better is BetterDirection.HIGHER:
        improvement = draws_b - draws_a
    else:
        improvement = draws_a - draws_b
    return float(np.mean(improvement > margin))


def probability_b_beats_a(
    posterior_a: BetaPosterior,
    posterior_b: BetaPosterior,
    better: BetterDirection = BetterDirection.HIGHER,
    seed: int = DEFAULT_SEED,
    draws: int = DEFAULT_POSTERIOR_DRAWS,
) -> float:
    """P(B is better than A) under independent Beta posteriors. A probability, not a decision: pair it with a margin."""
    return probability_difference_beyond_margin(posterior_a, posterior_b, 0.0, better, seed, draws)


# ---------------------------------------------------------------------------------------------------------------
# Campaign noise floor
# ---------------------------------------------------------------------------------------------------------------


def baseline_noise_floor(
    baseline_scores: Sequence[float], confidence: float = DEFAULT_CONFIDENCE
) -> NoiseFloor:
    """How far one candidate run must sit from the mean of repeated, unmodified baseline runs to clear their spread.

    The half-width of the prediction interval for one new run: t * s * sqrt(1 + 1/n). A candidate score inside
    baseline_mean +/- noise_floor is not distinguishable from another baseline run. Needs at least 3 baseline runs
    that each re-seed everything; 5 or more is better.
    """
    scores = np.asarray(baseline_scores, dtype=float)
    if scores.ndim != 1 or scores.size < 3:
        raise ValueError("need at least 3 baseline runs to estimate spread")
    _check_open_unit_interval(confidence, "confidence")
    standard_deviation = float(scores.std(ddof=1))
    t_value = float(stats.t.ppf(0.5 + confidence / 2.0, scores.size - 1))
    return NoiseFloor(
        baseline_runs=int(scores.size),
        baseline_mean=float(scores.mean()),
        baseline_standard_deviation=standard_deviation,
        noise_floor=t_value * standard_deviation * math.sqrt(1.0 + 1.0 / scores.size),
        confidence=confidence,
        method="prediction interval for one new run",
    )


def attempts_since_improvement(
    scores_in_attempt_order: Sequence[float],
    noise_floor: float,
    better: BetterDirection = BetterDirection.HIGHER,
) -> int:
    """Attempts since the best score last improved by MORE than the noise floor. Compare with a plateau limit.

    The first score is the reference. A score resets the count, and becomes the new reference, only when it beats the
    reference by more than the noise floor.
    """
    if noise_floor < 0.0:
        raise ValueError("noise_floor must be non-negative")
    if len(scores_in_attempt_order) == 0:
        return 0
    sign = 1.0 if better is BetterDirection.HIGHER else -1.0
    reference = sign * scores_in_attempt_order[0]
    attempts_without_improvement = 0
    for score in scores_in_attempt_order[1:]:
        signed_score = sign * score
        if signed_score > reference + noise_floor:
            reference = signed_score
            attempts_without_improvement = 0
        else:
            attempts_without_improvement += 1
    return attempts_without_improvement


# ---------------------------------------------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------------------------------------------


def _to_jsonable(result: object) -> object:
    if hasattr(result, "__dataclass_fields__"):
        return asdict(result)  # type: ignore[call-overload]
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=help_text, description=help_text)

    def add_confidence(command: argparse.ArgumentParser) -> None:
        command.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE)

    def add_power_alpha(command: argparse.ArgumentParser) -> None:
        command.add_argument("--power", type=float, default=DEFAULT_POWER)
        command.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)

    def add_direction(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--direction",
            type=ChangeDirection,
            choices=list(ChangeDirection),
            default=ChangeDirection.DECREASE,
        )

    for interval_command in (Command.WILSON, Command.CLOPPER_PEARSON):
        command = add(interval_command.value, f"{interval_command.value} interval for a rate")
        command.add_argument("--successes", type=int, required=True)
        command.add_argument("--trials", type=int, required=True)
        add_confidence(command)

    command = add(Command.RULE_OF_THREE.value, "upper bound on a rate after zero events")
    command.add_argument("--trials", type=int, required=True)
    add_confidence(command)

    command = add(
        Command.PAIRED_BOOTSTRAP.value,
        "bootstrap interval for mean (candidate - baseline) over matched units",
    )
    command.add_argument("--baseline", type=float, nargs="+", required=True)
    command.add_argument("--candidate", type=float, nargs="+", required=True)
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    command.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    add_confidence(command)

    command = add(
        Command.CLUSTER_BOOTSTRAP.value,
        "bootstrap interval for the mean of values, resampling whole clusters",
    )
    command.add_argument("--values", type=float, nargs="+", required=True)
    command.add_argument("--clusters", nargs="+", required=True, help="one cluster id per value")
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)
    command.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES // 2)
    add_confidence(command)

    command = add(Command.MCNEMAR.value, "exact McNemar test on discordant pairs")
    command.add_argument("--baseline-only-failures", type=int, required=True)
    command.add_argument("--candidate-only-failures", type=int, required=True)

    command = add(Command.HOLM.value, "Holm-adjusted p-values")
    command.add_argument("p_values", type=float, nargs="+")

    command = add(
        Command.SEEDS_FOR_RATE.value, "runs per arm to detect a change in a rate, unpaired"
    )
    command.add_argument("--baseline-rate", type=float, required=True)
    command.add_argument("--minimum-detectable-difference", type=float, required=True)
    command.add_argument("--design-effect", type=float, default=1.0)
    add_power_alpha(command)
    add_direction(command)

    command = add(
        Command.PAIRS_FOR_PAIRED_RATE.value, "pairs needed for a paired pass/fail comparison"
    )
    command.add_argument("--discordant-rate", type=float, required=True)
    command.add_argument("--minimum-detectable-difference", type=float, required=True)
    add_power_alpha(command)

    command = add(Command.PAIRS_FOR_DELTA.value, "pairs needed for a paired mean difference")
    command.add_argument("--paired-delta-sd", type=float, required=True)
    command.add_argument("--minimum-detectable-delta", type=float, required=True)
    add_power_alpha(command)

    command = add(
        Command.MDD.value, "smallest mean difference n runs per arm (or pairs) can detect"
    )
    command.add_argument("--standard-deviation", type=float, required=True)
    command.add_argument("--units", type=int, required=True)
    command.add_argument("--paired", action="store_true")
    add_power_alpha(command)

    command = add(
        Command.MDD_RATE.value, "smallest change in a rate n runs per arm can detect, unpaired"
    )
    command.add_argument("--baseline-rate", type=float, required=True)
    command.add_argument("--units-per-arm", type=int, required=True)
    add_power_alpha(command)
    add_direction(command)

    command = add(Command.DESIGN_EFFECT.value, "variance inflation for clustered runs")
    command.add_argument("--cluster-size", type=float, required=True)
    command.add_argument("--intraclass-correlation", type=float, required=True)

    command = add(Command.BOUNDARIES.value, "group-sequential stopping boundaries")
    command.add_argument("--looks", type=int, default=2)
    command.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    command.add_argument(
        "--family",
        type=BoundaryFamily,
        choices=list(BoundaryFamily),
        default=BoundaryFamily.OBRIEN_FLEMING,
    )
    command.add_argument("--information-fractions", type=float, nargs="+", default=None)

    command = add(Command.BETA_POSTERIOR.value, "Beta posterior for a rate")
    command.add_argument("--successes", type=int, required=True)
    command.add_argument("--trials", type=int, required=True)
    command.add_argument("--prior-alpha", type=float, default=1.0)
    command.add_argument("--prior-beta", type=float, default=1.0)
    command.add_argument("--credible-level", type=float, default=DEFAULT_CONFIDENCE)

    command = add(
        Command.PROB_B_BEATS_A.value,
        "P(arm B better than arm A by more than a margin) from two Beta posteriors",
    )
    for arm in ("a", "b"):
        command.add_argument(f"--{arm}-successes", type=int, required=True)
        command.add_argument(f"--{arm}-trials", type=int, required=True)
    command.add_argument("--prior-alpha", type=float, default=1.0)
    command.add_argument("--prior-beta", type=float, default=1.0)
    command.add_argument("--margin", type=float, default=0.0)
    command.add_argument(
        "--better",
        type=BetterDirection,
        choices=list(BetterDirection),
        default=BetterDirection.HIGHER,
    )
    command.add_argument("--seed", type=int, default=DEFAULT_SEED)

    command = add(
        Command.NOISE_FLOOR.value,
        "gap a single candidate run needs to clear the spread of repeated baseline runs",
    )
    command.add_argument("baseline_scores", type=float, nargs="+")
    add_confidence(command)
    return parser


def run_command(arguments: argparse.Namespace) -> object:
    """Dispatch a parsed command line to the library function and return its result."""
    command = Command(arguments.command)
    if command is Command.WILSON:
        return wilson_interval(arguments.successes, arguments.trials, arguments.confidence)
    if command is Command.CLOPPER_PEARSON:
        return clopper_pearson_interval(arguments.successes, arguments.trials, arguments.confidence)
    if command is Command.RULE_OF_THREE:
        return {
            "trials": arguments.trials,
            "upper_bound": rule_of_three_upper_bound(arguments.trials, arguments.confidence),
        }
    if command is Command.PAIRED_BOOTSTRAP:
        return paired_bootstrap_delta(
            arguments.baseline,
            arguments.candidate,
            arguments.seed,
            arguments.resamples,
            arguments.confidence,
        )
    if command is Command.CLUSTER_BOOTSTRAP:
        return cluster_bootstrap_ci(
            arguments.values,
            arguments.clusters,
            np.mean,
            arguments.seed,
            arguments.resamples,
            arguments.confidence,
        )
    if command is Command.MCNEMAR:
        return mcnemar_exact(arguments.baseline_only_failures, arguments.candidate_only_failures)
    if command is Command.HOLM:
        return {"adjusted_p_values": holm_adjust(arguments.p_values)}
    if command is Command.SEEDS_FOR_RATE:
        return required_seeds_for_rate_difference(
            arguments.baseline_rate,
            arguments.minimum_detectable_difference,
            arguments.power,
            arguments.alpha,
            arguments.direction,
            arguments.design_effect,
        )
    if command is Command.PAIRS_FOR_PAIRED_RATE:
        return required_pairs_for_paired_rate_difference(
            arguments.discordant_rate,
            arguments.minimum_detectable_difference,
            arguments.power,
            arguments.alpha,
        )
    if command is Command.PAIRS_FOR_DELTA:
        return required_pairs_for_paired_delta(
            arguments.paired_delta_sd,
            arguments.minimum_detectable_delta,
            arguments.power,
            arguments.alpha,
        )
    if command is Command.MDD:
        return minimum_detectable_difference(
            arguments.standard_deviation,
            arguments.units,
            arguments.power,
            arguments.alpha,
            arguments.paired,
        )
    if command is Command.MDD_RATE:
        return minimum_detectable_rate_difference(
            arguments.baseline_rate,
            arguments.units_per_arm,
            arguments.power,
            arguments.alpha,
            arguments.direction,
        )
    if command is Command.DESIGN_EFFECT:
        return {
            "design_effect": design_effect(arguments.cluster_size, arguments.intraclass_correlation)
        }
    if command is Command.BOUNDARIES:
        return group_sequential_boundaries(
            arguments.looks, arguments.alpha, arguments.family, arguments.information_fractions
        )
    if command is Command.BETA_POSTERIOR:
        return beta_posterior(
            arguments.successes,
            arguments.trials,
            arguments.prior_alpha,
            arguments.prior_beta,
            arguments.credible_level,
        )
    if command is Command.PROB_B_BEATS_A:
        posterior_a = beta_posterior(
            arguments.a_successes, arguments.a_trials, arguments.prior_alpha, arguments.prior_beta
        )
        posterior_b = beta_posterior(
            arguments.b_successes, arguments.b_trials, arguments.prior_alpha, arguments.prior_beta
        )
        return {
            "posterior_a": posterior_a,
            "posterior_b": posterior_b,
            "margin": arguments.margin,
            "probability_b_better_by_more_than_margin": probability_difference_beyond_margin(
                posterior_a, posterior_b, arguments.margin, arguments.better, arguments.seed
            ),
        }
    if command is Command.NOISE_FLOOR:
        return baseline_noise_floor(arguments.baseline_scores, arguments.confidence)
    raise ValueError(f"unknown command {command.value}")


def _jsonable_tree(value: object) -> object:
    if isinstance(value, dict):
        return {key: _jsonable_tree(item) for key, item in value.items()}
    return _to_jsonable(value)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        result = run_command(arguments)
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(_jsonable_tree(result), indent=JSON_INDENT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
