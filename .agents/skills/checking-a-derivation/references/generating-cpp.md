# Generating C++ from a derivation

Read when the user has agreed to replace hand-typed derivative code with generated code (required).

## Pitfalls

- Generated code carries the symbols' names: declare them with the names the C++ already uses, and keep `real=True`
  so no `re()`/`im()` or `conjugate` calls appear in the output.
- `cse` hoists shared sub-expressions into temporaries; print the temporaries in order, then the reduced entries.
  Never print the reduced entries first.
- Emit only non-zero entries into a matrix that is already zero-initialised. A dense dump of a 15x15 Jacobian hides
  the structure reviewers need to see.
- A limiting-form branch (small `omega`, `cos(pitch)` near zero) is still hand-written. Generate it from
  `limit(...)` or `series(...)` and keep the branch condition in the C++.
- Run the three-way check on the generated code too: port it with `scripts/check_jacobian.py`, or compile a small
  test against the same random points.

## Recipe

```python
from sympy import Matrix, cse, cxxcode, numbered_symbols

jacobian = Matrix(next_state_equations).jacobian(state_symbols)
temporaries, (reduced,) = cse(jacobian, symbols=numbered_symbols("t"))

for name, expression in temporaries:
    print(f"const double {name} = {cxxcode(expression)};")
for row in range(reduced.rows):
    for column in range(reduced.cols):
        if reduced[row, column] != 0:
            print(f"jacobian({row}, {column}) = {cxxcode(reduced[row, column])};")
```

`cxxcode` emits `std::sin`, `std::pow` and friends. To get a standalone C function with a header instead, use
`sympy.utilities.codegen.codegen(("name", expression), "C99", prefix="name", header=False)`; it returns
`[(c_name, c_code), (h_name, h_code)]`, and the header must be kept with the source.
