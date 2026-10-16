#!/usr/bin/env python3
"""Closed-form queueing answers and replication summaries, standard library only.

Run `queueing.py --help` for the commands:
    queueing.py mmc --arrival-rate 5 --service-rate 2 --servers 3
    queueing.py mg1 --arrival-rate 0.8 --mean-service-time 1 --service-time-cv 2
    queueing.py finite --sources 6 --servers 2 --mean-time-between-requests 30 --mean-service-time 5
    queueing.py summarize 12.1 11.8 12.6 ...      (one number per independent replication)

Rates share one time unit (for example per minute); the waits printed are in that unit.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# Two-sided Student-t critical values for 1..30 degrees of freedom, keyed by confidence level.
T_CRITICAL_VALUES: dict[float, tuple[float, ...]] = {
    0.90: (
        6.314, 2.920, 2.353, 2.132, 2.015, 1.943, 1.895, 1.860, 1.833, 1.812,
        1.796, 1.782, 1.771, 1.761, 1.753, 1.746, 1.740, 1.734, 1.729, 1.725,
        1.721, 1.717, 1.714, 1.711, 1.708, 1.706, 1.703, 1.701, 1.699, 1.697,
    ),
    0.95: (
        12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228,
        2.201, 2.179, 2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086,
        2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042,
    ),
    0.99: (
        63.657, 9.925, 5.841, 4.604, 4.032, 3.707, 3.499, 3.355, 3.250, 3.169,
        3.106, 3.055, 3.012, 2.977, 2.947, 2.921, 2.898, 2.878, 2.861, 2.845,
        2.831, 2.819, 2.807, 2.797, 2.787, 2.779, 2.771, 2.763, 2.756, 2.750,
    ),
}  # fmt: skip
NORMAL_CRITICAL_VALUES: dict[float, float] = {0.90: 1.6449, 0.95: 1.9600, 0.99: 2.5758}
MAX_TABULATED_DEGREES_OF_FREEDOM = 30


@dataclass(frozen=True)
class QueueMetrics:
    """Steady-state metrics of an M/M/c queue. Times are in the unit of the input rates."""

    servers: int
    arrival_rate: float
    service_rate: float
    utilisation: float
    probability_of_waiting: float
    mean_wait_in_queue: float
    mean_time_in_system: float
    mean_queue_length: float
    mean_number_in_system: float


@dataclass(frozen=True)
class ReplicationSummary:
    """Mean and Student-t confidence interval of one number per independent replication."""

    replications: int
    mean: float
    standard_deviation: float
    confidence_level: float
    half_width: float
    lower: float
    upper: float


@dataclass(frozen=True)
class FiniteSourceMetrics:
    """Steady-state metrics of the machine-repair (finite-source) model."""

    sources: int
    servers: int
    fraction_of_sources_waiting_or_served: float
    mean_waiting: float
    server_utilisation: float
    probability_all_servers_busy: float


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability that an arrival must wait in an M/M/c queue (Erlang C).

    `offered_load` is arrival_rate / service_rate (in Erlangs). Needs offered_load < servers.
    """
    if servers < 1:
        raise ValueError("servers must be at least 1")
    if offered_load < 0:
        raise ValueError("offered_load must not be negative")
    if offered_load >= servers:
        raise ValueError(
            "offered_load must be below servers, otherwise the queue grows without bound"
        )
    # Erlang B by its stable recursion, then convert: C = B / (1 - rho * (1 - B)).
    blocking_probability = 1.0
    for server_count in range(1, servers + 1):
        blocking_probability = (
            offered_load
            * blocking_probability
            / (server_count + offered_load * blocking_probability)
        )
    utilisation = offered_load / servers
    return blocking_probability / (1.0 - utilisation * (1.0 - blocking_probability))


def mmc_metrics(arrival_rate: float, service_rate: float, servers: int) -> QueueMetrics:
    """M/M/c steady state: Poisson arrivals, exponential service, `servers` identical servers, FIFO, infinite queue."""
    if arrival_rate <= 0 or service_rate <= 0:
        raise ValueError("arrival_rate and service_rate must be positive")
    offered_load = arrival_rate / service_rate
    probability_of_waiting = erlang_c(servers, offered_load)
    mean_wait_in_queue = probability_of_waiting / (servers * service_rate - arrival_rate)
    mean_time_in_system = mean_wait_in_queue + 1.0 / service_rate
    return QueueMetrics(
        servers=servers,
        arrival_rate=arrival_rate,
        service_rate=service_rate,
        utilisation=offered_load / servers,
        probability_of_waiting=probability_of_waiting,
        mean_wait_in_queue=mean_wait_in_queue,
        mean_time_in_system=mean_time_in_system,
        mean_queue_length=arrival_rate * mean_wait_in_queue,  # Little's law
        mean_number_in_system=arrival_rate * mean_time_in_system,  # Little's law
    )


def mg1_mean_wait_in_queue(
    arrival_rate: float, mean_service_time: float, service_time_cv: float
) -> float:
    """M/G/1 mean queue wait (Pollaczek-Khinchine). `service_time_cv` is service std / mean; 1 is exponential."""
    if arrival_rate <= 0 or mean_service_time <= 0 or service_time_cv < 0:
        raise ValueError(
            "arrival_rate and mean_service_time must be positive, service_time_cv non-negative"
        )
    utilisation = arrival_rate * mean_service_time
    if utilisation >= 1:
        raise ValueError("utilisation must be below 1, otherwise the queue grows without bound")
    return utilisation * mean_service_time * (1 + service_time_cv**2) / (2 * (1 - utilisation))


def finite_source_metrics(
    sources: int, servers: int, mean_time_between_requests: float, mean_service_time: float
) -> FiniteSourceMetrics:
    """Machine-repair model: `sources` units each ask for service after an exponential gap, `servers` serve them.

    Exact birth-death solution with exponential times. State k is the number of units waiting or in service.
    """
    if sources < 1 or servers < 1:
        raise ValueError("sources and servers must be at least 1")
    if mean_time_between_requests <= 0 or mean_service_time <= 0:
        raise ValueError("mean times must be positive")
    weights = [1.0]
    for waiting_or_served in range(1, sources + 1):
        request_rate = (sources - waiting_or_served + 1) / mean_time_between_requests
        service_rate = min(waiting_or_served, servers) / mean_service_time
        weights.append(weights[-1] * request_rate / service_rate)
    total_weight = sum(weights)
    probabilities = [weight / total_weight for weight in weights]
    mean_waiting_or_served = sum(
        count * probability for count, probability in enumerate(probabilities)
    )
    mean_waiting = sum(
        max(0, count - servers) * probability for count, probability in enumerate(probabilities)
    )
    mean_in_service = mean_waiting_or_served - mean_waiting
    return FiniteSourceMetrics(
        sources=sources,
        servers=servers,
        fraction_of_sources_waiting_or_served=mean_waiting_or_served / sources,
        mean_waiting=mean_waiting,
        server_utilisation=mean_in_service / servers,
        probability_all_servers_busy=sum(probabilities[servers:]),
    )


def t_critical_value(degrees_of_freedom: int, confidence_level: float) -> float:
    """Two-sided Student-t critical value for 0.90, 0.95 or 0.99 confidence."""
    if confidence_level not in T_CRITICAL_VALUES:
        raise ValueError(f"confidence_level must be one of {sorted(T_CRITICAL_VALUES)}")
    if degrees_of_freedom < 1:
        raise ValueError("degrees_of_freedom must be at least 1")
    if degrees_of_freedom <= MAX_TABULATED_DEGREES_OF_FREEDOM:
        return T_CRITICAL_VALUES[confidence_level][degrees_of_freedom - 1]
    normal_value = NORMAL_CRITICAL_VALUES[confidence_level]
    return normal_value * (
        1 + (normal_value**2 + 1) / (4 * degrees_of_freedom)
    )  # Cornish-Fisher correction


def summarize_replications(
    values: Sequence[float], confidence_level: float = 0.95
) -> ReplicationSummary:
    """Student-t interval for the mean of one estimate per independent replication (never per-customer values)."""
    if len(values) < 2:
        raise ValueError("need at least 2 replications to estimate variance")
    mean = statistics.fmean(values)
    standard_deviation = statistics.stdev(values)
    half_width = (
        t_critical_value(len(values) - 1, confidence_level)
        * standard_deviation
        / math.sqrt(len(values))
    )
    return ReplicationSummary(
        replications=len(values),
        mean=mean,
        standard_deviation=standard_deviation,
        confidence_level=confidence_level,
        half_width=half_width,
        lower=mean - half_width,
        upper=mean + half_width,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    mmc = commands.add_parser("mmc", help="M/M/c metrics (c=1 is M/M/1)")
    mmc.add_argument("--arrival-rate", type=float, required=True)
    mmc.add_argument("--service-rate", type=float, required=True, help="per server")
    mmc.add_argument("--servers", type=int, required=True)

    mg1 = commands.add_parser("mg1", help="M/G/1 mean queue wait (Pollaczek-Khinchine)")
    mg1.add_argument("--arrival-rate", type=float, required=True)
    mg1.add_argument("--mean-service-time", type=float, required=True)
    mg1.add_argument(
        "--service-time-cv",
        type=float,
        required=True,
        help="std / mean; 1 is exponential, 0 constant",
    )

    finite = commands.add_parser(
        "finite", help="Finite sources sharing servers (machine repair), exponential times"
    )
    finite.add_argument("--sources", type=int, required=True)
    finite.add_argument("--servers", type=int, required=True)
    finite.add_argument(
        "--mean-time-between-requests", type=float, required=True, help="per source"
    )
    finite.add_argument("--mean-service-time", type=float, required=True)

    summarize = commands.add_parser(
        "summarize", help="Mean and t interval over replication results"
    )
    summarize.add_argument(
        "values", type=float, nargs="+", help="one number per independent replication"
    )
    summarize.add_argument(
        "--confidence", type=float, default=0.95, choices=sorted(T_CRITICAL_VALUES)
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "mmc":
            result: object = asdict(
                mmc_metrics(arguments.arrival_rate, arguments.service_rate, arguments.servers)
            )
        elif arguments.command == "mg1":
            wait = mg1_mean_wait_in_queue(
                arguments.arrival_rate, arguments.mean_service_time, arguments.service_time_cv
            )
            result = {
                "mean_wait_in_queue": wait,
                "mean_queue_length": arguments.arrival_rate * wait,
            }
        elif arguments.command == "finite":
            result = asdict(
                finite_source_metrics(
                    arguments.sources,
                    arguments.servers,
                    arguments.mean_time_between_requests,
                    arguments.mean_service_time,
                )
            )
        else:
            result = asdict(summarize_replications(arguments.values, arguments.confidence))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
