#!/usr/bin/env python3
"""Three-way Jacobian check: independent symbolic derivation, the code's Jacobian, and finite differences.

Run the built-in demo (a coordinated-turn model, correct and with a planted sign flip):

    uv run --no-project --with sympy==1.14.0 python check_jacobian.py --demo

To check your own model, import this file from a script and call `check_jacobian`. The model equations are the
independent source of truth; `code_jacobian` is a Python transcription (or a call into) the Jacobian under test.

What `check_jacobian` runs:

* Step 1, derive: the Jacobian of `model_equations`, evaluated in 50-digit arithmetic so small denominators do not
  hide behind rounding noise.
* Step 2, symbolic comparison: `simplify(derived - code)` per entry, only when you pass `code_expressions` (the
  code's Jacobian as a SymPy matrix). Without it the report says step 2 was not run.
* Step 3, numeric comparison: code vs derived vs central differences, at your named `edge_points` first and then at
  random points. Edge points are how you test omega = 1e-8 or both sides of a branch boundary.
* `distinct_parameter_groups` flags any checked point where parameters that must differ are nearly equal.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

import mpmath
import sympy

JacobianFunction = Callable[[Sequence[float]], Sequence[Sequence[float]]]

# Central differences balance truncation error (grows with the step squared) against rounding error (grows as
# 1/step); the cube root of machine epsilon, scaled by the size of the input, is the usual optimum.
FINITE_DIFFERENCE_STEP_SCALE = 6.0e-6
# Tolerance rule: |a - b| <= relative_tolerance * (1 + max(|a|, |b|)). Small entries get an absolute floor,
# large entries a relative allowance.
SYMBOLIC_RELATIVE_TOLERANCE = 1.0e-9
FINITE_DIFFERENCE_RELATIVE_TOLERANCE = 1.0e-5
DEFAULT_POINT_COUNT = 50
DEFAULT_RANDOM_SEED = 20261016
# Digits used to evaluate the model and its derived Jacobian, enough that cancellation near a pole (omega = 1e-8)
# is not mistaken for a defect.
EVALUATION_DIGITS = 50
# Two parameters that must differ count as "nearly equal" when they agree to this relative tolerance.
NEAR_EQUAL_RELATIVE_TOLERANCE = 1.0e-3
RANDOM_POINT_NAME_PREFIX = "random"


class Comparison(Enum):
    """Which pair of Jacobians disagreed."""

    CODE_VS_DERIVED = "code vs derived"
    CODE_VS_FINITE_DIFFERENCE = "code vs finite difference"
    DERIVED_VS_FINITE_DIFFERENCE = "derived vs finite difference"


class SymbolicStatus(Enum):
    """Outcome of step 2, the symbolic comparison."""

    ALL_PROVEN_EQUAL = "all entries proven equal"
    NOT_PROVEN_EQUAL = "entries not proven equal"
    NOT_RUN = "NOT RUN"


@dataclass(frozen=True)
class NamedPoint:
    """An edge point to check by name, such as `small_omega`. `values` must give every state symbol."""

    name: str
    values: Mapping[sympy.Symbol, float]


@dataclass(frozen=True)
class EntryMismatch:
    """The first checked point at which one Jacobian entry disagreed in one comparison."""

    row: int
    column: int
    entry_name: str
    comparison: Comparison
    reference_value: float
    other_value: float
    point_name: str
    point: tuple[float, ...]


@dataclass(frozen=True)
class SymbolicMismatch:
    """An entry for which `simplify(derived - code)` did not reduce to zero."""

    row: int
    column: int
    entry_name: str
    difference: sympy.Expr


@dataclass(frozen=True)
class NearEqualWarning:
    """A checked point at which two parameters named as distinct were nearly equal."""

    point_name: str
    first_symbol: sympy.Symbol
    second_symbol: sympy.Symbol
    value: float


@dataclass(frozen=True)
class CheckReport:
    random_point_count: int
    edge_point_names: tuple[str, ...]
    mismatches: tuple[EntryMismatch, ...]
    symbolic_status: SymbolicStatus
    symbolic_mismatches: tuple[SymbolicMismatch, ...]
    near_equal_warnings: tuple[NearEqualWarning, ...]
    entry_count: int

    @property
    def all_terms_match(self) -> bool:
        """True when no numeric comparison and no symbolic comparison that ran found a difference."""
        return len(self.mismatches) == 0 and len(self.symbolic_mismatches) == 0

    def summary(self) -> str:
        lines: list[str] = []
        checked = f"{len(self.edge_point_names)} named edge points, {self.random_point_count} random points"
        if len(self.mismatches) == 0:
            lines.append(f"numeric: all terms match ({checked}, three-way)")
        else:
            lines.append(f"numeric: {len(self.mismatches)} mismatching entries over {checked}:")
            for mismatch in self.mismatches:
                lines.append(
                    f"  {mismatch.entry_name} [{mismatch.row},{mismatch.column}] {mismatch.comparison.value} "
                    f"at {mismatch.point_name}: {mismatch.reference_value:.9g} vs {mismatch.other_value:.9g}"
                )
        if self.symbolic_status is SymbolicStatus.NOT_RUN:
            lines.append(
                "symbolic: NOT RUN (step 2 skipped: pass code_expressions, the code's Jacobian as a SymPy matrix)"
            )
        elif len(self.symbolic_mismatches) == 0:
            lines.append(f"symbolic: all {self.entry_count} entries proven equal")
        else:
            lines.append(f"symbolic: {len(self.symbolic_mismatches)} entries not proven equal:")
            for symbolic_mismatch in self.symbolic_mismatches:
                lines.append(
                    f"  {symbolic_mismatch.entry_name} [{symbolic_mismatch.row},{symbolic_mismatch.column}] "
                    f"simplify(derived - code) = {symbolic_mismatch.difference}"
                )
        for warning in self.near_equal_warnings:
            lines.append(
                f"warning: {warning.first_symbol} and {warning.second_symbol} are nearly equal "
                f"({warning.value:.6g}) at {warning.point_name}; swapped-variable bugs can hide here"
            )
        return "\n".join(lines)


def within_tolerance(first_value: float, second_value: float, relative_tolerance: float) -> bool:
    allowed_difference = relative_tolerance * (1.0 + max(abs(first_value), abs(second_value)))
    return abs(first_value - second_value) <= allowed_difference


def wrap_angle(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def central_finite_difference_jacobian(
    model_function: Callable[[Sequence[float]], Sequence[float]],
    point: Sequence[float],
    wrapped_output_rows: frozenset[int] = frozenset(),
) -> list[list[float]]:
    """Jacobian of `model_function` at `point`. Rows listed in `wrapped_output_rows` hold angles, so a difference
    that straddles the +-pi cut is wrapped before dividing by the step."""
    output_count = len(model_function(point))
    jacobian = [[0.0] * len(point) for _ in range(output_count)]
    for column in range(len(point)):
        step = FINITE_DIFFERENCE_STEP_SCALE * max(1.0, abs(point[column]))
        forward_point = list(point)
        backward_point = list(point)
        forward_point[column] += step
        backward_point[column] -= step
        forward_outputs = model_function(forward_point)
        backward_outputs = model_function(backward_point)
        for row in range(output_count):
            difference = forward_outputs[row] - backward_outputs[row]
            if row in wrapped_output_rows:
                difference = wrap_angle(difference)
            jacobian[row][column] = difference / (2.0 * step)
    return jacobian


def symbolic_difference_is_zero(
    derived_expression: sympy.Expr, code_expression: sympy.Expr
) -> bool:
    """True when `simplify` proves the two expressions equal. False means "not proven", not "different": the
    numeric comparison in `check_jacobian` says whether the entries really differ."""
    difference = sympy.simplify(derived_expression - code_expression)
    return difference == 0


def limiting_form_mismatches(
    derived_jacobian: sympy.Matrix,
    code_limiting_jacobian: sympy.Matrix,
    variable: sympy.Symbol,
    limit_point: sympy.Expr = sympy.S.Zero,
) -> list[tuple[int, int, sympy.Expr, sympy.Expr]]:
    """Entries where `limit(derived, variable -> limit_point)` differs from the code's limiting-form branch.
    Returns (row, column, limit_of_derived, code_value)."""
    mismatches: list[tuple[int, int, sympy.Expr, sympy.Expr]] = []
    for row in range(derived_jacobian.rows):
        for column in range(derived_jacobian.cols):
            limit_value = sympy.limit(derived_jacobian[row, column], variable, limit_point)
            code_value = code_limiting_jacobian[row, column]
            if sympy.simplify(limit_value - code_value) != 0:
                mismatches.append((row, column, limit_value, code_value))
    return mismatches


def values_nearly_equal(first_value: float, second_value: float) -> bool:
    larger_magnitude = max(abs(first_value), abs(second_value))
    return abs(first_value - second_value) <= NEAR_EQUAL_RELATIVE_TOLERANCE * larger_magnitude


def near_equal_warnings_at_point(
    point_name: str,
    point_values: Mapping[sympy.Symbol, float],
    distinct_parameter_groups: Sequence[Sequence[sympy.Symbol]],
) -> list[NearEqualWarning]:
    warnings: list[NearEqualWarning] = []
    for group in distinct_parameter_groups:
        for first_index in range(len(group)):
            for second_index in range(first_index + 1, len(group)):
                first_symbol = group[first_index]
                second_symbol = group[second_index]
                if values_nearly_equal(point_values[first_symbol], point_values[second_symbol]):
                    warnings.append(
                        NearEqualWarning(
                            point_name, first_symbol, second_symbol, point_values[first_symbol]
                        )
                    )
    return warnings


def compare_symbolically(
    derived_matrix: sympy.Matrix,
    code_matrix: sympy.Matrix,
    entry_names: Sequence[Sequence[str]],
) -> list[SymbolicMismatch]:
    """Step 2: `simplify(derived - code)` for every entry."""
    if derived_matrix.shape != code_matrix.shape:
        raise ValueError(
            f"code_expressions has shape {code_matrix.shape}, expected {derived_matrix.shape}"
        )
    mismatches: list[SymbolicMismatch] = []
    for row in range(derived_matrix.rows):
        for column in range(derived_matrix.cols):
            derived_entry = derived_matrix[row, column]
            code_entry = code_matrix[row, column]
            if not symbolic_difference_is_zero(derived_entry, code_entry):
                difference = sympy.simplify(derived_entry - code_entry)
                mismatches.append(
                    SymbolicMismatch(row, column, entry_names[row][column], difference)
                )
    return mismatches


def check_jacobian(
    state_symbols: Sequence[sympy.Symbol],
    model_equations: Sequence[sympy.Expr],
    code_jacobian: JacobianFunction,
    sample_ranges: Mapping[sympy.Symbol, tuple[float, float]],
    parameter_values: Mapping[sympy.Symbol, float] | None = None,
    wrapped_output_rows: frozenset[int] = frozenset(),
    point_count: int = DEFAULT_POINT_COUNT,
    random_seed: int = DEFAULT_RANDOM_SEED,
    output_names: Sequence[str] | None = None,
    edge_points: Sequence[NamedPoint] = (),
    code_expressions: sympy.Matrix | None = None,
    distinct_parameter_groups: Sequence[Sequence[sympy.Symbol]] = (),
) -> CheckReport:
    """Compare `code_jacobian` with the Jacobian derived from `model_equations` and with central differences.

    `model_equations` are the next-state (or measurement) equations written from the physics, not from the code's
    Jacobian. `output_names` label the rows in the report. `sample_ranges` must keep random points away from poles
    and, for angle inputs, away from the +-pi cut; use `edge_points` for the points you chose on purpose (small
    omega, both sides of each branch boundary), which are checked first and reported by name. A pole itself (such
    as omega = 0 exactly) cannot be evaluated; use a point next to it.

    `code_expressions` is the code's Jacobian as a SymPy matrix over `state_symbols`. When given, step 2 runs and
    its result is its own line in the report; without it the report says step 2 was not run.
    `distinct_parameter_groups` lists groups of symbols that must take different values; a checked point where two
    of a group are nearly equal produces a warning.
    """
    fixed_parameters = dict(parameter_values or {})
    equation_vector = sympy.Matrix(list(model_equations)).subs(fixed_parameters)
    derived_matrix = equation_vector.jacobian(sympy.Matrix(list(state_symbols)))
    entry_names = [
        [
            f"d{output_names[row] if output_names is not None else f'f{row}'}/d{state_symbols[column]}"
            for column in range(derived_matrix.cols)
        ]
        for row in range(derived_matrix.rows)
    ]

    symbolic_mismatches: list[SymbolicMismatch] = []
    if code_expressions is None:
        symbolic_status = SymbolicStatus.NOT_RUN
    else:
        symbolic_mismatches = compare_symbolically(
            derived_matrix, sympy.Matrix(code_expressions).subs(fixed_parameters), entry_names
        )
        if len(symbolic_mismatches) == 0:
            symbolic_status = SymbolicStatus.ALL_PROVEN_EQUAL
        else:
            symbolic_status = SymbolicStatus.NOT_PROVEN_EQUAL

    model_function = _lambdify_vector(state_symbols, list(equation_vector))
    derived_function = _lambdify_matrix(state_symbols, derived_matrix)

    named_points: list[tuple[str, dict[sympy.Symbol, float]]] = []
    for edge_point in edge_points:
        missing_symbols = [symbol for symbol in state_symbols if symbol not in edge_point.values]
        if missing_symbols:
            raise ValueError(f"edge point {edge_point.name!r} has no value for {missing_symbols}")
        named_points.append((edge_point.name, dict(edge_point.values)))
    random_generator = random.Random(random_seed)
    for point_index in range(point_count):
        random_values = {
            symbol: random_generator.uniform(*sample_ranges[symbol]) for symbol in state_symbols
        }
        named_points.append((f"{RANDOM_POINT_NAME_PREFIX} {point_index}", random_values))

    first_mismatches: dict[tuple[int, int, Comparison], EntryMismatch] = {}
    near_equal_warnings: list[NearEqualWarning] = []
    for point_name, point_values in named_points:
        near_equal_warnings.extend(
            near_equal_warnings_at_point(point_name, point_values, distinct_parameter_groups)
        )
        point = tuple(point_values[symbol] for symbol in state_symbols)
        derived_values = derived_function(point)
        code_values = code_jacobian(point)
        finite_difference_values = central_finite_difference_jacobian(
            model_function, point, wrapped_output_rows
        )
        for row in range(derived_matrix.rows):
            for column in range(derived_matrix.cols):
                comparisons = (
                    (
                        Comparison.CODE_VS_DERIVED,
                        derived_values[row][column],
                        code_values[row][column],
                        SYMBOLIC_RELATIVE_TOLERANCE,
                    ),
                    (
                        Comparison.CODE_VS_FINITE_DIFFERENCE,
                        finite_difference_values[row][column],
                        code_values[row][column],
                        FINITE_DIFFERENCE_RELATIVE_TOLERANCE,
                    ),
                    (
                        Comparison.DERIVED_VS_FINITE_DIFFERENCE,
                        finite_difference_values[row][column],
                        derived_values[row][column],
                        FINITE_DIFFERENCE_RELATIVE_TOLERANCE,
                    ),
                )
                for comparison, reference_value, other_value, tolerance in comparisons:
                    key = (row, column, comparison)
                    if key in first_mismatches:
                        continue
                    if not within_tolerance(reference_value, other_value, tolerance):
                        first_mismatches[key] = EntryMismatch(
                            row,
                            column,
                            entry_names[row][column],
                            comparison,
                            reference_value,
                            other_value,
                            point_name,
                            point,
                        )
    return CheckReport(
        random_point_count=point_count,
        edge_point_names=tuple(edge_point.name for edge_point in edge_points),
        mismatches=tuple(first_mismatches.values()),
        symbolic_status=symbolic_status,
        symbolic_mismatches=tuple(symbolic_mismatches),
        near_equal_warnings=tuple(near_equal_warnings),
        entry_count=derived_matrix.rows * derived_matrix.cols,
    )


def _lambdify_vector(
    state_symbols: Sequence[sympy.Symbol], expressions: Sequence[sympy.Expr]
) -> Callable[[Sequence[float]], Sequence[float]]:
    """Compile to mpmath and evaluate at `EVALUATION_DIGITS` digits, returning plain floats."""
    compiled = sympy.lambdify(list(state_symbols), list(expressions), modules="mpmath")

    def evaluate(point: Sequence[float]) -> Sequence[float]:
        with mpmath.workdps(EVALUATION_DIGITS):
            # Convert the inputs first: a float quotient such as omega / 10 would round before the cancellation.
            return [
                float(value)
                for value in compiled(*[mpmath.mpf(coordinate) for coordinate in point])
            ]

    return evaluate


def _lambdify_matrix(
    state_symbols: Sequence[sympy.Symbol], matrix: sympy.Matrix
) -> JacobianFunction:
    flat_function = _lambdify_vector(state_symbols, list(matrix))
    column_count = matrix.cols

    def evaluate(point: Sequence[float]) -> Sequence[Sequence[float]]:
        flat_values = flat_function(point)
        return [
            list(flat_values[start : start + column_count])
            for start in range(0, len(flat_values), column_count)
        ]

    return evaluate


# ---------------------------------------------------------------------------------------------------------------
# Demo: planar coordinated-turn model, state (x, y, vx, vy, omega), as in a tracking filter's motion model.
# ---------------------------------------------------------------------------------------------------------------

DEMO_TIME_STEP_RATIONAL = sympy.Rational(1, 10)
DEMO_TIME_STEP_SECONDS = float(DEMO_TIME_STEP_RATIONAL)
DEMO_OMEGA_BRANCH_THRESHOLD = 1.0e-6
DEMO_SMALL_OMEGA = 1.0e-9


def demo_model() -> tuple[list[sympy.Symbol], list[sympy.Expr], sympy.Symbol]:
    x, y, vx, vy, omega = sympy.symbols("x y vx vy omega", real=True)
    time_step = DEMO_TIME_STEP_RATIONAL
    sine = sympy.sin(omega * time_step)
    cosine = sympy.cos(omega * time_step)
    equations = [
        x + sine / omega * vx - (1 - cosine) / omega * vy,
        y + (1 - cosine) / omega * vx + sine / omega * vy,
        cosine * vx - sine * vy,
        sine * vx + cosine * vy,
        omega,
    ]
    return [x, y, vx, vy, omega], equations, omega


def demo_limiting_form(
    delta: sympy.Expr, vx: sympy.Expr, vy: sympy.Expr, sign_of_omega_column: float
) -> sympy.Matrix:
    """The omega -> 0 limit of the demo Jacobian. `sign_of_omega_column` of -1.0 plants a wrong sign in the
    d(x', y')/d(omega) entries, the kind of slip a small-omega branch hides from random sampling."""
    limiting_form = sympy.eye(5)
    limiting_form[0, 2] = delta
    limiting_form[1, 3] = delta
    limiting_form[0, 4] = -sign_of_omega_column * sympy.Rational(1, 2) * delta**2 * vy
    limiting_form[1, 4] = sign_of_omega_column * sympy.Rational(1, 2) * delta**2 * vx
    limiting_form[2, 4] = -delta * vy
    limiting_form[3, 4] = delta * vx
    return limiting_form


def demo_code_jacobian(
    sign_of_omega_term: float, sign_of_limiting_branch: float = 1.0
) -> JacobianFunction:
    """The hand-written Jacobian under test. `sign_of_omega_term` of -1.0 plants a sign flip in d(vx')/d(omega);
    `sign_of_limiting_branch` of -1.0 plants one in the small-omega branch only."""

    def jacobian(point: Sequence[float]) -> Sequence[Sequence[float]]:
        _, _, vx, vy, omega = point
        delta = DEMO_TIME_STEP_SECONDS
        if abs(omega) < DEMO_OMEGA_BRANCH_THRESHOLD:
            limiting_form = demo_limiting_form(delta, vx, vy, sign_of_limiting_branch)
            return [[float(limiting_form[row, column]) for column in range(5)] for row in range(5)]
        sine = math.sin(omega * delta)
        cosine = math.cos(omega * delta)
        omega_squared = omega * omega
        return [
            [
                1.0,
                0.0,
                sine / omega,
                -(1 - cosine) / omega,
                (cosine * delta * vx) / omega
                - (sine * vx) / omega_squared
                - (sine * delta * vy) / omega
                - ((-1 + cosine) * vy) / omega_squared,
            ],
            [
                0.0,
                1.0,
                (1 - cosine) / omega,
                sine / omega,
                (sine * delta * vx) / omega
                - ((1 - cosine) * vx) / omega_squared
                + (cosine * delta * vy) / omega
                - (sine * vy) / omega_squared,
            ],
            [
                0.0,
                0.0,
                cosine,
                -sine,
                sign_of_omega_term * ((-sine * delta * vx) - (cosine * delta * vy)),
            ],
            [0.0, 0.0, sine, cosine, (cosine * delta * vx) - (sine * delta * vy)],
            [0.0, 0.0, 0.0, 0.0, 1.0],
        ]

    return jacobian


def demo_code_expressions(sign_of_omega_term: float) -> sympy.Matrix:
    """The exact-form branch of `demo_code_jacobian`, transcribed entry by entry into SymPy for step 2."""
    state_symbols, _, omega = demo_model()
    _, _, vx, vy, _ = state_symbols
    delta = DEMO_TIME_STEP_RATIONAL
    sine = sympy.sin(omega * delta)
    cosine = sympy.cos(omega * delta)
    omega_squared = omega * omega
    return sympy.Matrix(
        [
            [
                1,
                0,
                sine / omega,
                -(1 - cosine) / omega,
                (cosine * delta * vx) / omega
                - (sine * vx) / omega_squared
                - (sine * delta * vy) / omega
                - ((-1 + cosine) * vy) / omega_squared,
            ],
            [
                0,
                1,
                (1 - cosine) / omega,
                sine / omega,
                (sine * delta * vx) / omega
                - ((1 - cosine) * vx) / omega_squared
                + (cosine * delta * vy) / omega
                - (sine * vy) / omega_squared,
            ],
            [
                0,
                0,
                cosine,
                -sine,
                sign_of_omega_term * ((-sine * delta * vx) - (cosine * delta * vy)),
            ],
            [0, 0, sine, cosine, (cosine * delta * vx) - (sine * delta * vy)],
            [0, 0, 0, 0, 1],
        ]
    )


def demo_edge_points() -> list[NamedPoint]:
    """Named edge points with unequal velocities: omega deep inside the small-omega branch (both signs), a slow
    turn on the exact branch, and a large turn rate. Points right at the branch threshold are left out because the
    limiting form's O(omega) error and the exact form's rounding both exceed the demo's tolerance there."""
    x, y, vx, vy, omega = demo_model()[0]
    return [
        NamedPoint("small_omega", {x: 1.0, y: -2.0, vx: 2.5, vy: -1.5, omega: DEMO_SMALL_OMEGA}),
        NamedPoint(
            "small_negative_omega", {x: 1.0, y: -2.0, vx: 2.5, vy: -1.5, omega: -DEMO_SMALL_OMEGA}
        ),
        NamedPoint("slow_turn", {x: 0.5, y: 0.5, vx: 2.0, vy: 3.0, omega: 0.05}),
        NamedPoint("large_omega", {x: 0.0, y: 0.0, vx: -2.0, vy: 1.0, omega: 3.0}),
    ]


def run_demo() -> int:
    state_symbols, equations, omega = demo_model()
    _, _, vx, vy, _ = state_symbols
    ranges = {symbol: (-3.0, 3.0) for symbol in state_symbols}
    ranges[omega] = (0.2, 2.0)  # random points stay away from omega = 0; the edge points cover it
    all_passed = True

    cases = (
        ("correct Jacobian", 1.0, 1.0, True),
        ("planted sign flip in d(vx')/d(omega)", -1.0, 1.0, False),
        ("planted sign flip in the small-omega branch only", 1.0, -1.0, False),
    )
    for label, sign_of_omega_term, sign_of_limiting_branch, should_pass in cases:
        report = check_jacobian(
            state_symbols,
            equations,
            demo_code_jacobian(sign_of_omega_term, sign_of_limiting_branch),
            ranges,
            output_names=["x", "y", "vx", "vy", "omega"],
            edge_points=demo_edge_points(),
            code_expressions=demo_code_expressions(sign_of_omega_term),
            distinct_parameter_groups=[(vx, vy)],
        )
        print(f"{label}:\n{report.summary()}")
        all_passed = all_passed and (report.all_terms_match == should_pass)

    derived = sympy.Matrix(equations).jacobian(sympy.Matrix(state_symbols))
    limiting_form = demo_limiting_form(DEMO_TIME_STEP_RATIONAL, vx, vy, 1.0)
    limit_problems = limiting_form_mismatches(derived, limiting_form, omega)
    print(
        f"limiting form at omega -> 0: {'all terms match' if not limit_problems else limit_problems}"
    )
    return 0 if all_passed and not limit_problems else 1


def main(argument_list: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--demo", action="store_true", help="run the coordinated-turn demonstration"
    )
    arguments = parser.parse_args(argument_list)
    if arguments.demo:
        return run_demo()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
