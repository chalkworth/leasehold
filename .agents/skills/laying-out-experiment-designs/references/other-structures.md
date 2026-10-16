# Other structures: Latin square, split-plot, crossover, nested

Read when: a factor is expensive or slow to change between runs, two nuisance factors need controlling at once,
the same unit receives several conditions in sequence, or factors sit inside others. Only the Latin square has a
command (`square`); the rest are built by hand from a layout, with `order_runs(..., within=...)` from
`scripts/layouts.py` in this skill's folder for restricted shuffling.

## Split-plot (a hard-to-change factor)

Use when one factor cannot be reset for every run: a retrain, a dataset rebuild, a GPU type, a base model, a server
restart. Assign that factor to whole plots and the easy factor to subplots inside each.

1. Lay out the whole-plot factor with replicated whole plots (`factorial` or `allocate`).
2. Cross every whole plot with the subplot factor levels, keeping a `plot` column.
3. Shuffle the whole-plot order, then the subplot order within each plot: `order_runs(design, seed, within="plot")`.

There are two error terms, so the whole-plot factor is tested against the variation between plots and gets few
degrees of freedom. A global shuffle that ignores the plots, or an analysis that treats every row as free to
reorder, is wrong for the whole-plot factor (usually too optimistic). Plan more whole plots, not more subplots.

## Latin square (two nuisance factors)

`square --order n` gives n treatments with each once per row and per column. Rows are one nuisance factor (day,
machine) and columns the other (slot, position), and the rows, columns and treatments are permuted at random.
It assumes no treatment-by-nuisance interaction. A 3 by 3 square leaves 2 degrees of freedom for error, so repeat
it or use 4 or more.

## Crossover and repeated measures

Carry-over exists only when the state persists: a fine-tuned model, a memory or cache, an agent session reused
between conditions, a human who learned. When every condition starts from a clean state, a paired design needs no
sequence: use the same units in each arm and shuffle the run order (`designing-experiments` covers the pairing).

When state does persist, assign sequences and add a washout (a reset, or runs that are discarded). For n
treatments, a Williams design balances order and first-order carry-over: first row `0, 1, n-1, 2, n-2, ...`, each
later row adds 1 to every entry modulo n. For even n this gives n sequences; for odd n use two such squares (one
reversed), 2n sequences. Assign units to sequences in equal numbers. Analyse with period, sequence and unit in the
model.
<!-- VERIFIED(Williams construction, n=3 to 7): each ordered pair of treatments is adjacent once per even-n square, once per reversed pair for odd n => computed: n=4 12 of 12 and n=6 30 of 30 distinct adjacent pairs; n=3, 5, 7 with the reversed square 6, 20, 42 of n(n-1) -->

## Nested designs

When prompts sit inside tasks, seeds inside configs, or runs inside machines, the replicate is the level the
condition is assigned to, and rows below it are subsamples. Lay out and count at that level;
`designing-experiments` covers the independent unit.
