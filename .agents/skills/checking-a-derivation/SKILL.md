---
name: checking-a-derivation
description: Use when asked "is this formula, closed form, Jacobian or derivative right?", "check this derivation" or "does this maths match the code?", in situations such as EKF/UKF models, kinematics, TTC/PET/braking-distance maths, or when C++ for a model should be generated from its derivation instead of typed by hand
---

# Checking a Derivation

Check a hand-derived formula a second way before trusting it. The check takes seconds, so run it without asking.
Re-deriving with the same mistake proves nothing: derive from the model equations, never from the code's expression.

```bash
uv run --no-project --with sympy==1.14.0 python <script>
```

Before trusting any output → read `references/traps-tolerances-and-examples.md` (required: SymPy domain and branch
traps, the tolerance rule, and examples of where the check applies).

## Keep the check independent of the code

- Take the expected value from the model equations or a brute-force integration, never from the code's own constants
  or intermediate variables.
- Test at inputs where the parameters differ (no two equal, such as `abort_speed != cross_speed`), at random points
  and at edge cases. Equal parameters hide swapped-variable bugs.

## The three-way check

1. **Derive independently.** Write the model equations (next-state or measurement function) from the physics in
   SymPy and take `Matrix(f).jacobian(state)`. For a closed form, solve the underlying equations, not the code's
   algebra.
2. **Compare symbolically** with the code's expression, transcribed mechanically entry by entry:
   `simplify(derived - transcribed) == 0`.
3. **Confirm numerically** at 50 or more random points: code vs derived, and code vs central finite differences of
   the model.
4. **Check limiting forms.** Where code branches on `|omega| < eps` or `v == 0`, compare the second formula with
   `limit(derived_entry, omega, 0)` for every entry.
5. **Report** `all terms match`, or the exact entries, such as `dPitch/droll [4,3] code vs derived`.

## The template

`scripts/check_jacobian.py` runs steps 1 to 3 for any model. Import it from your own script:

```python
from check_jacobian import NamedPoint, check_jacobian

report = check_jacobian(
    state_symbols,
    model_equations,
    code_jacobian,
    sample_ranges,
    edge_points=[NamedPoint("small_omega", {...})],
    code_expressions=code_matrix,
    distinct_parameter_groups=[(abort_speed, cross_speed)],
)
print(report.summary())
```

`code_jacobian` maps a state tuple to a nested list: port or call the code under test. `edge_points` are checked
before the random ones and reported by name. `code_expressions` (the code's Jacobian as a SymPy matrix) turns on
step 2; without it the report says `symbolic: NOT RUN`. Step 4 is separate: call `limiting_form_mismatches`. Run
`python scripts/check_jacobian.py --demo` to see a correct coordinated-turn Jacobian pass, a planted sign flip and a
small-omega-branch flip fail by entry and point name.

## Replacing hand-written C++ with generated code

Generating the C++ from the derivation removes typing errors, but it changes the repo: ask the user first, naming the
file that would be replaced (required: ask the user, with AskUserQuestion where your CLI has it, otherwise in prose, and stop until the user answers). On a yes → read `references/generating-cpp.md` and follow it.

For a claim about throughput, queues or capacity, use `testing-ideas-with-simulation` instead.
