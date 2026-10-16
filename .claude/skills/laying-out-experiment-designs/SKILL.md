---
name: laying-out-experiment-designs
description: Use when generating the runs of a designed experiment, such as screening N factors, a DOE matrix, a fractional or full factorial with aliasing and resolution, Plackett-Burman, a response surface (central composite, Box-Behnken), a Latin hypercube or space-filling sweep over continuous parameters, a randomization schedule (permuted block, stratified, cluster allocation) with a balance check, or a Latin square, split-plot, crossover or nested layout. How many seeds, pairing, stopping rules and choosing the analysis go to designing-experiments
---

# Laying Out Experiment Designs

Produce the table of runs, and say what it cannot separate. Hand back the layout file, its run count, what is
confounded (resolution and aliases, or the partial last block), and the command and seed that regenerate it.

This skill generates layouts. For the comparison itself (arms, paired seeds, the independent unit, sample size,
stopping, reading the result), use `designing-experiments`.

## Pick the layout

| The task is | Command | Read |
| -- | -- | -- |
| Up to about 5 factors, interactions wanted | `factorial` | `references/factorial-and-screening.md` |
| Many factors, find the vital few | `factorial --resolution 3` or `4`, or `screen` | `references/factorial-and-screening.md` |
| Tune continuous factors with curvature (2 to 5) | `surface` | `references/response-surface-and-space-filling.md` |
| Sweep continuous parameters of an expensive run | `sweep` | `references/response-surface-and-space-filling.md` |
| Assign units, runs or clusters to arms in a ratio | `allocate` | `references/allocation-schedules.md` |
| A factor is hard to change, a Latin square, runs in sequence on one unit, factors nested in others | `square`, or build it by hand | `references/other-structures.md` |

Read the reference for the row before generating: each opens with the traps for its layout.

## Run it

`scripts/layouts.py` needs numpy, pandas and pydoe pinned to 1.5.0. <!-- VERIFIED(pydoe 1.5.0): pinned release installs and exposes fracfact, fracfact_by_res, fracfact_aliasing, fold, ff2n, pbdesign, ccdesign, bbdesign, lhs => `uv run --with pydoe==1.5.0` import of each name, 2026-10-16 -->

```bash
uv run --no-project --with numpy --with pandas --with pydoe==1.5.0 python scripts/layouts.py factorial \
  --factor lr_log10=-4:-2 --factor depth=2:8 --factor wd=0:0.1 --centers 3 --seed 20260101 --out layout.csv
```

`scripts/layouts.py <command> --help` lists the arguments. The CSV goes to `--out` or stdout; a JSON note (runs,
resolution, aliases, actual factor ranges, balance) goes to stderr: read it before using the layout. Paths are
relative to this skill's folder. The library functions (`order_runs`, `allocate`, `balance`, `resolution`) import from
`scripts/` when it is on `sys.path`.

## Rules for every layout

- **Name the confounding.** A fraction, a Plackett-Burman design and a partial block each leave something
  inseparable. Quote the printed resolution and aliases next to any "factor X has no effect".
- **Check the real range.** Every factor's minimum and maximum, printed in a `surface` note and visible in the CSV
  for the rest, must be values the system accepts. Do not clip a row: it changes the design.
- **Put scale-sensitive factors on their natural scale.** Give a learning rate or batch size as its log, and
  exponentiate or round after generating. Rounding can create duplicate rows, so check for them.
- **Centre and replicate runs must be independent.** A repeat with the same seed and deterministic code repeats the
  number, so it gives no error estimate. Give each repeat its own seed.
- **A global shuffle suits free-to-reorder runs.** When runs share a machine, a day, a warm cache or a whole plot,
  keep that column and shuffle within it (`order_runs(design, seed, within="block")`).
- **Keep the seed with the layout.** Record the seed, the pydoe version and the CSV: the same seed need not reproduce
  the layout after a package upgrade.
