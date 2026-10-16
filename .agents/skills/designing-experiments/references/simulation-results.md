# Simulation results as experiments

Read when: a number comes from a simulator run (a game engine, a robotics simulator, a SimPy model) and feeds a comparison or a
claim. Queueing and discrete-event models (warm-up, replications, validating the model) are covered by
`testing-ideas-with-simulation`: read its `references/simulation-method.md` for those; this file leaves them there. This file covers the part that is about experiments: using a simulator's runs as units.

## Rules

- **A simulator is a source of units, not of certainty.** Deterministic with a seed means reproducible, not
  replicated. Each seed is one unit; one deterministic run is n = 1.
- **Seed every source.** The sim's seed, the scenario generator, the agent or planner's RNG, data order, and any
  threaded or GPU nondeterminism. A `--seed` flag counts only if every process seeds itself. Check by running the
  same seed twice and comparing; differing results mean something is unseeded.
- **Common random numbers pair the arms.** Use the same seed list and scenarios for baseline and candidate
  (`paired_seeds` in `scripts/stats.py`), so the per-seed difference has far less noise than either arm. Analyse the
  differences (`references/choosing-the-analysis.md`).
- **Scenarios are the clusters.** Episodes drawn from the same scenario are correlated; decide whether you are
  generalising to new seeds of known scenarios or to new scenarios, and make that the unit.
- **Censored and unfinished runs stay in.** A run cut off by the time limit is an outcome (count it as a failure or
  as censored), not a missing row. Dropping unfinished runs flatters whatever is slow.
- **Validate before claiming a real-world number.** A simulated rate is a statement about the model. Compare with a
  real measurement in the regime where both exist, or label the claim "in simulation". Simulation gain is not a
  measured effect on the robot.
- **Report the sim version, scenario set and seed list** with the result, so a rerun reproduces it.

Planning how many seeds: `references/planning-sample-size.md`.
