"""The searches the calibration and the study use, in the standard library alone: Nelder-Mead
over several parameters, golden-section search over one, and a Latin hypercube sample."""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Found:
    """Where a search ended and how it got there."""

    x: list[float]
    fun: float
    evaluations: int
    iterations: int
    #: Whether it stopped because the simplex shrank below the tolerances; False when it ran out
    #: of evaluations first.
    converged: bool


def nelder_mead(
    f: Callable[[list[float]], float],
    start: list[float],
    *,
    step: float = 0.5,
    max_evaluations: int = 800,
    xatol: float = 1e-3,
    fatol: float = 1e-6,
) -> Found:
    """The minimum of `f` near `start` by Nelder-Mead, with the coefficients scaled to the
    dimension (Gao and Han, 2012). The first simplex moves each coordinate by `step`."""
    n = len(start)
    alpha, gamma = 1.0, 1 + 2 / n
    rho, shrink = 0.75 - 1 / (2 * n), 1 - 1 / n
    evaluations = 0

    def value(x: list[float]) -> float:
        nonlocal evaluations
        evaluations += 1
        return f(x)

    simplex = [list(start)] + [
        [v + (step if i == j else 0.0) for j, v in enumerate(start)] for i in range(n)
    ]
    values = [value(x) for x in simplex]
    iterations = 0
    while evaluations < max_evaluations:
        order = sorted(range(n + 1), key=values.__getitem__)
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        spread_x = max(abs(a - b) for x in simplex[1:] for a, b in zip(x, simplex[0], strict=True))
        if spread_x <= xatol and values[-1] - values[0] <= fatol:
            return Found(simplex[0], values[0], evaluations, iterations, True)
        iterations += 1
        centre = [sum(x[j] for x in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]

        def toward(t: float, worst=worst, centre=centre) -> list[float]:
            return [c + t * (c - w) for c, w in zip(centre, worst, strict=True)]

        reflected = toward(alpha)
        fr = value(reflected)
        if fr < values[0]:
            expanded = toward(alpha * gamma)
            fe = value(expanded)
            simplex[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            simplex[-1], values[-1] = reflected, fr
        else:
            outside = fr < values[-1]
            contracted = toward(rho * alpha if outside else -rho)
            fc = value(contracted)
            if fc < (fr if outside else values[-1]):
                simplex[-1], values[-1] = contracted, fc
            else:
                best = simplex[0]
                simplex = [best] + [
                    [b + shrink * (v - b) for b, v in zip(best, x, strict=True)]
                    for x in simplex[1:]
                ]
                values = [values[0]] + [value(x) for x in simplex[1:]]
    i = min(range(n + 1), key=values.__getitem__)
    return Found(simplex[i], values[i], evaluations, iterations, False)


def golden(f: Callable[[float], float], lo: float, hi: float, *, tolerance: float = 1e-3) -> Found:
    """The minimum of `f` on [lo, hi] by golden-section search (`f` with one minimum there)."""
    ratio = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = f(c), f(d)
    evaluations, iterations = 2, 0
    while b - a > tolerance:
        iterations += 1
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = f(d)
        evaluations += 1
    x, fx = (c, fc) if fc < fd else (d, fd)
    return Found([x], fx, evaluations, iterations, True)


def latin_hypercube(dimensions: int, points: int, seed: int) -> list[list[float]]:
    """`points` points in the unit cube with one point in each of `points` equal strata of
    every coordinate."""
    rng = random.Random(f"lhs:{seed}")
    columns = []
    for _ in range(dimensions):
        strata = list(range(points))
        rng.shuffle(strata)
        columns.append([(s + rng.random()) / points for s in strata])
    return [list(row) for row in zip(*columns, strict=True)]
