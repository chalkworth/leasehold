# Factorial and screening layouts

Read when: choosing or reading a full or fractional two-level factorial, a Plackett-Burman design, or its aliasing.
Commands are `scripts/layouts.py` subcommands in this skill's folder.

## Traps first

- **One run per cell leaves no error estimate.** A full 2^k with every effect in the model is saturated. Replicate
  with independent seeds, or fix the reduced model before the data arrive.
- **Aliased means inseparable, not small.** In a fraction two effects share one contrast, so a real interaction can
  cancel a real main effect. The note's `aliases` lists the groups up to three factors, with `letters` naming a, b,
  c... after your factors in order.
- **Plackett-Burman supports only some run counts.** Runs are the next multiple of 4 above the factor count, and
  pydoe 1.5.0 builds only 2^a, 12*2^a and 20*2^a runs: 11 factors take 12 runs, 12 take 16, 23 take 24, and 24 or 27
  raise `ValueError` (28 runs). For those use `factorial --resolution 3` or drop factors.
  <!-- VERIFIED(pydoe 1.5.0): pbdesign supports 2^a, 12*2^a, 20*2^a runs; 24, 27, 32, 35 and 40 factors raise ValueError => pbdesign(11)=12 runs, pbdesign(12)=16, pbdesign(23)=24, pbdesign(24) and pbdesign(27) raise "n must be a multiple of 4" -->
- **Plackett-Burman is resolution III, and each interaction spreads over many main effects.** No alias list exists
  for it. Use it to triage, then study the survivors.
- **Centre runs test curvature in aggregate only.** They show that a linear model fails, not which factor curves.
  Move to `surface` for that.
- **Factors with more than two levels are not in the script.** Build the grid with `itertools.product` and cross it
  with a two-level layout, or run the full grid when it is small.

## Resolution

| Resolution | Main effects | Two-factor interactions |
| -- | -- | -- |
| III | aliased with two-factor interactions | aliased with main effects |
| IV | clear of two-factor interactions | aliased with each other |
| V | clear | clear of each other (aliased with three-factor and higher) |

A claim that main effects or interactions are clear assumes the interactions they are aliased with are negligible.

## Choosing the fraction

`factorial --resolution R` returns the smallest 2^(k-p) design at resolution R or better, and prints the resolution
it actually has.
<!-- VERIFIED(pydoe 1.5.0): fracfact_by_res run counts => fracfact_by_res(6,3)=8 runs, (7,3)=8, (6,4)=16, (7,4)=16, (8,4)=16, (5,5)=16, (6,5)=32, (8,5)=64 -->
Runs: 6 factors take 8 runs at resolution III, 16 at IV and 32 at V; 8 factors take 16 at IV and 64 at V. Prefer IV
when a few interactions may matter and V when they are the goal.

`--generator "a b c abc"` fixes the fraction yourself in Yates notation (here 2^(4-1), D = ABC, resolution IV);
use it when a specific interaction must stay clear.

## De-aliasing

`--fold-over` adds the mirror image of every run as `block` 1. Folding a resolution III design makes it resolution
IV: seven factors go from 8 to 16 runs.
<!-- VERIFIED(pydoe 1.5.0): fold returns the original runs plus their sign-flipped mirror => fold(fracfact_by_res(7,3)) has 16 rows, first 8 equal the input, last 8 equal minus the input, resolution 4 -->
Run block 0 first when its result decides whether block 1 is worth running; analyse with `block` in the model.

## Staging

Screen, then fit a factorial on the survivors, then a response surface near the best region. Learning interactions
and finding the optimum in one cheap design fits neither.
