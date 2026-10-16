# Trading off failure modes

Read when: a change improves one failure rate or metric and may worsen another (safety against progress, collisions
against stalls), a tuner or score combines several metrics into one number, or you must choose a threshold from a
sweep. A typical case is a tuner that scores a trial as a weighted composite (tracking error, overshoot, a
penalty for tipping over). The weights silently decide the trade-off, and tuning against the composite hides it.

## Rules

1. **Check the objectives conflict.** If they do not, there is nothing to trade; optimise one.
2. **A hard limit is a constraint, not an objective.** Collisions = 0, or unsafe_rate within a budget, is a
   constraint. Keep it a constraint; a weight lets a gain buy it back. Judge the constraint on an upper confidence bound (the Wilson upper
   limit over seeds), not the mean: a setting passes only if the bound passes. `tip_penalty` as a weight lets a large
   enough gain in speed outvote tip-overs; as a constraint it cannot.
3. **Every point is a seeded mean with an interval.** A single seed per setting turns noise into a front. A point is
   dominated only if its interval separates from the dominator's on at least one objective and it is no better on any.
4. **Report the trade-off points.** Show them and let the owner choose, in place of one weighted optimum.

## Method (payoff table, then epsilon-constraint)

1. **Payoff table.** Optimise each objective alone and record every objective at each optimum. The diagonal is each
   objective's best; the off-diagonals give each objective's range. The ranges set sweep bounds and put objectives on
   one scale (seconds, percent and counts can't be weighted until divided by their ranges).

   | | unsafe_rate | wait_time_s |
   |---|---|---|
   | best for unsafe_rate | best | worst |
   | best for wait_time_s | worst | best |

2. **Epsilon-constraint sweep.** Keep one objective, constrain the others (`wait_time_s <= epsilon`), and sweep epsilon
   across the payoff range. Unlike a weighted sum, it reaches concave parts of the front. Use a coarse grid first,
   because each point costs repeated rollouts, then refine where the curve bends.
3. **Filter and read.** Drop dominated points (with intervals). Name the knee, where the cost of the next gain rises
   sharply. Quote exchange rates ("about 1.5 s of wait per point of unsafe_rate here"). Then ask which point to take.

## Tools

Optuna studies take several `directions` and need no new dependency. Run repeats per candidate rather than
NSGA-II over a handful of single-seed runs; with few evaluations, screen first (a coarse sweep of one or two
parameters), then optimise. Method after NVIDIA `cuopt-multi-objective-exploration` (Apache-2.0), rewritten for noisy
rollouts.
