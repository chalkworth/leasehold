# Measured comparison

Read when: a result feeds a merge, a benchmark, a tuned value or a reported number, or someone asks how many seeds or
runs are needed. You were handed a brief: the decision, the arms and metric, the budget and the data. Work like a
careful experimentalist: state the hypothesis and its falsifier, rule out rival explanations, and quantify
uncertainty. The agent that briefed you turns your return into the answer for the user, so the return leads with the
decision. Paths below are relative to the skill folder (the parent of `steps/`).

## Return

Lead with five lines, then the detail the reader needs to trust them:

```
Decision: keep the new planner (or: not shown yet / rerun / revert)
Effect: unsafe_rate 10.2% → 8.1%, −2.1 pts (95% −3.0 to −1.2), 3,200 paired seeds per arm
MDD at this n: 0.9 pts
Plan or caveat: fixed n before running, analysed once at the end
Next: ship behind the flag; recheck on the night scenarios
```

## Steps

1. **Claim and falsifier.** One sentence before running: the claim, and the result that would refute it and change
   the decision ("upper bound on unsafe_rate above 8%", "gain smaller than the baseline's seed-to-seed spread"). Pick held-out scenarios
   or clips before tuning, and use them once.
2. **Rival explanations.** Name what else could produce the result (single-run noise, seed or data order, leakage between
   splits, a changed metric or config) and choose runs that tell them apart.
3. **Independent unit.** The unit is the seed, episode, scenario or clip; frames, steps and rows sit inside a run. n counts
   units; runs sharing a scenario or clip are clustered.
4. **Repeat, pair, interval.** Seed every random source and record the seeds. Run at least 5 seeds per arm, and the
   same seeds, scenarios and frames in every arm. Claim a pairing gain only from a measured pairing correlation or
   discordant rate (`references/planning-sample-size.md`); without one, size n unpaired. Report the effect in the
   metric's units with its interval and n, then the minimum detectable difference (MDD) at that n as the power
   statement. Compare a delta with the spread of repeated baseline runs.
5. **Stopping rule.** Fix the run count before starting, from the smallest difference that matters. Or plan two looks (50% and 100%)
   with O'Brien-Fleming boundaries and stop at the planned end. Across many arms or sweeps, adjust (Holm) or report
   every arm. A "how many seeds" answer ends with the plan: "Fix n = 3,213 per arm before running; analyse once at
   the end, or at two planned looks (O'Brien-Fleming 2.797, 1.977)."
6. **Plots** show each interval with its unit and n, and a single run as a plain line.

## Files

Numbers: `uv run --no-project --with numpy --with scipy python scripts/stats.py --help` from the skill folder
(subcommands print JSON; tested against textbook values). Use it instead of rewriting the formulas.

- Choosing the interval or test (rates, rare events, paired seeds, clustered frames, sweeps) → read
  `references/choosing-the-analysis.md` (required before reporting an interval).
- How many seeds, or what a given n could detect → read `references/planning-sample-size.md` (required at step 5).
- Looking at results while runs are still arriving → read `references/sequential-stopping.md` (required).
- A prior from earlier runs, P(B beats A), or very few runs per cell → read `references/bayesian-rates.md`.
- Trading one failure rate against another, or a tuner that scores a weighted sum → read
  `references/trading-off-failure-modes.md`.
- Results from a simulation (warm-up, replications, validation) → read `references/simulation-results.md`.
