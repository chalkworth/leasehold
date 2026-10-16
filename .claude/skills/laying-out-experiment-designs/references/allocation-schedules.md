# Allocation schedules

Read when: assigning units, runs, prompts, tasks or whole clusters to arms in a fixed ratio, or checking the balance
of a schedule. `allocate` in `scripts/layouts.py` (this skill's folder) builds permuted-block schedules.

## Traps first

- **Balance holds only at complete blocks.** The last block of a stratum may be partial, and any prefix of the
  schedule (an interim look, a run stopped early) need not have the ratio. The note's `partial_block_size` gives the
  size of each stratum's incomplete last block, 0 when none. Choose n as a multiple of the block size when the
  counts must be exact.
- **Block size is a multiple of the ratio total.** For arms in ratio 2:1 it is 3, 6, 9. Small blocks balance
  tightly but are easy to predict from earlier assignments, so a person or agent who sees them can steer which
  unit gets which arm. The script uses one fixed block size.
- **A sequence of stratum labels keeps input order.** `unit_id` is the unit's one-based position in the sequence you
  passed (`allocate(["a", "b", "a"], ...)` from Python), even when strata interleave: join the schedule to your
  roster on it and check every ID appears once. A `--strata a=30 b=30` count list creates grouped slots instead.
- **Each stratum has its own random stream, keyed by its position in the order given or first seen.** The same
  seed with the strata in another order gives a different schedule.
- **Few strata beat many.** Many strata leave too few units each to fill a block; with many covariates and small n
  use minimization, which the script does not offer.
- **The schedule is not concealed by being in a file.** Generate it before the first run, commit it with the seed,
  and let a separate step release the next arm when the choice of the next unit could be influenced.

## Choosing

| Method | `allocate` flags | Use when |
| -- | -- | -- |
| Simple (independent per unit) | none: use `rng.choice` | n is large and a 12 against 8 split is tolerable |
| Permuted block | `--n N --block-size B` | exact ratio at block boundaries during sequential intake |
| Stratified block | `--strata a=30 b=30` | a known covariate (site, model family, difficulty tier) must be balanced |
| Cluster | `--clusters c1 c2 ...` | the arm applies to a whole group; one row per cluster |

## Clusters

The cluster is the unit that is randomized and replicated: sizing and the analysis belong in
`designing-experiments` (independent unit, design effect). With few clusters one draw can land badly
unbalanced on a baseline covariate: stratify on it.

## Blocks in compute

Batch, machine, day, rate-limit window and GPU type behave like blocks. Never let an arm line up with one: give each
block every arm (a stratum per machine does it), keep the `block` column, and put it in the analysis.
