# Traps, tolerances and worked examples

Read when running the check on a real model (required before trusting a "not proven" or a mismatch).

## SymPy traps

- A bare `Symbol` is complex. `sqrt(x**2)` is not `x` and `log(a*b)` is not `log(a)+log(b)` until you declare
  `real=True` or `positive=True`, and only when the problem justifies it.
- `solveset` solves over the complex numbers; pass `domain=S.Reals` for real questions. A `ConditionSet` result means
  unresolved, not "no solution"; only `EmptySet` means none. A `solve` root is a candidate: substitute it back into the
  original equation and the real domain.
- `==` compares structure, not value. `simplify(a - b) == 0` proves equality; a non-zero result means "not proven", so
  fall back to numeric points before calling it a mismatch.
- `lambdify` of a `Matrix` with `modules="math"` fails at call time (`ImmutableDenseMatrix` undefined). Flatten
  with `list(matrix)` first; the template script does.
- `sympify`, `parse_expr` and `lambdify` execute code. Never pass them untrusted strings, and never use a regex or
  `local_dict` as a sandbox.

## Tolerance rule

A pair passes when `|a-b| <= tol * (1 + max(|a|, |b|))`. Use `tol = 1e-9` for code vs derived and `1e-5` for
anything against central finite differences, with step `6e-6 * max(1, |x|)`. If the code differs from both other
values, the code is wrong; if derived and finite difference differ, suspect your model.

## Numeric traps

Sample away from poles (`cos(pitch) = 0`) and from the +-pi cut, or finite differences across a wrap give false
mismatches; wrap angle differences into [-pi, pi] (the template takes `wrapped_output_rows`). Random points
never reach a small denominator or a branch boundary, so pass them to the template as named `edge_points`:
`omega = 1e-9` (and its negative) on the limiting branch, and points on both sides of each branch boundary
whose true values double precision can still resolve. The template evaluates the derived Jacobian in 50-digit
arithmetic, so a mismatch there is a defect in the code's branch (cancellation in the exact form is a real defect
the symbolic check cannot see). Name groups of parameters that must differ in `distinct_parameter_groups` and it
warns when a checked point makes two of them nearly equal.

## Where this applies

- A state-transition Jacobian for a filter's predict step (roll, pitch and yaw trig terms). The transfer function in
  the same function is the model.
- A coordinated-turn motion model's Jacobian with a small-`omega` limiting form. The `--demo` model is this one.
- Closed-form timing functions with quadratic-root and ramp/cruise cases and `max(disc, 0)` guards. Derive each
  branch with `solve` over the stated regime, then compare at points on both sides of every branch boundary. A
  brute-force integrator in the tests is a third oracle.
