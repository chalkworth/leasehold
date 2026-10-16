# Response-surface and space-filling layouts

Read when: tuning continuous factors where curvature matters (`surface`), or sweeping parameters of an expensive
run (`sweep`). Commands are `scripts/layouts.py` subcommands in this skill's folder.

## Traps first

- **A circumscribed central composite design leaves the stated range.** Its axial points sit beyond low and high;
  for 2 factors with the default orthogonal alpha they reach 1.414 times the half-range. The note prints the
  `actual_range` of each factor. If the range is a limit (a valid learning rate, memory), use `--face inscribed`,
  which keeps the axial points at the limit and pulls the factorial corners inside it, or `--face faced`, which puts
  the axial points on the faces of the box. Do not clip rows.
  <!-- VERIFIED(pydoe 1.5.0): circumscribed ccdesign exceeds +-1; inscribed and faced stay within +-1 => ccdesign(2, center=(2,2)) max 1.414, inscribed 1.0 with corners at 0.707, faced 1.0 -->
- **Centre runs are the only pure-error replicates in the design.** The default here is 2 in each block (`--center
  2,2`); pydoe's own default is 4 and 4. They give pure error only when each runs on its own seed. Without them there
  is no lack-of-fit test.
  <!-- VERIFIED(pydoe 1.5.0): ccdesign default center is (4, 4) => inspect.signature(ccdesign): center=(4, 4) -->
- **Check that the quadratic model is estimable.** It has 1 + 2k + k(k-1)/2 coefficients (10 for 3 factors, 15 for
  4); the layout needs more distinct runs than that, plus replicates for error.
- **The output has no block column.** pydoe's composite design is built from a factorial block, an axial block and
  centres. If these run in separate sessions or on different machines, add that column yourself and shuffle within it.
- **Box-Behnken needs 3 or more factors** and never visits the all-low or all-high corners, so use it when those
  corners crash or cost too much. <!-- VERIFIED(pydoe 1.5.0): bbdesign rejects fewer than 3 factors => bbdesign(2) raises "Number of variables must be at least 3" -->

## Run counts

Runs without centres: Box-Behnken takes 12, 24, 40, 60 and 84 for 3 to 7 factors; a full composite takes 2^k + 2k,
which is 14, 24, 42 and 76 for 3 to 6 factors. The two tie at 4 factors, and a half-fraction composite (26 runs at
5 factors) is smaller than both from 5 factors; the script builds only the full composite.
<!-- VERIFIED(pydoe 1.5.0): bbdesign(k, center=1) has 12, 24, 40, 60, 84 non-centre rows for k=3..7; ccdesign(k, center=(1,1)) has 10, 16, 26, 44 rows for k=2..5 => counts above: 2^k+2k plus 2 centres -->
For `--kind bbd`, `--bbd-center` sets the centre runs (default 3).

## Space-filling sweeps

`sweep --n N` draws a maximin Latin hypercube: each factor's range is cut into N bins, each used once, and the
points are spread apart.

- **Choose N for the question.** A rule of thumb for fitting a surrogate is about 10 runs per factor; sensitivity
  indices need a Sobol design (`pydoe.saltelli_sampling`), which a Latin hypercube is not.
- **A stochastic run needs its own seeds.** Add a seed column per point, separate from the design seed, and
  replicate a few points to measure the noise the surrogate would otherwise fit as signal.
- **Integer, log and categorical factors need care.** Generate on a continuous log scale, then round: coverage is no
  longer exactly Latin, so check for duplicate rows (`design.duplicated()`).
- **The seed must go to pydoe.** Seeding numpy's global generator does nothing; the script passes `seed` to `lhs`.
  <!-- VERIFIED(pydoe 1.5.0): lhs(..., criterion="maximin", seed=s) is reproducible => two calls with seed=3 gave identical arrays -->
- **A Latin hypercube cannot be extended.** More points later means a new hypercube, or a Sobol sequence
  (`pydoe.sobol_sequence`) from the start when the budget is open.
  <!-- VERIFIED(pydoe 1.5.0): sobol_sequence and saltelli_sampling are exported => both appear in dir(pydoe) -->
