# Running a simulation study well

Read when: choosing warm-up, run length and replication counts, deciding between a fixed horizon and steady state,
comparing two designs, or when an interval looks too good. A general experiment-design skill may later supersede
this; it covers only what a discrete-event model needs.

## Fixed horizon or steady state

- **Fixed horizon** (a shift, a night, a mission, a batch): the start state is part of the question. Start every
  replication from the same real starting state and run to the natural end. No warm-up.
- **Steady state** (long-run average of a system with no natural end): the empty start biases early results, so
  discard a warm-up. Choose it from a pilot: plot the running mean of several replications and take the point where
  they settle, then use a run many times longer than the warm-up. Check that doubling the warm-up does not move the
  answer. Do not call a one-day model steady state because it has many events.

Decide the measurement window before looking at results (for example customers who arrive after warm-up). Different
rules estimate different quantities.

## Replications and intervals

- One run is one sample. Make at least 10 independent replications of the whole run, each with its own seed, and take
  one number per replication (its mean wait, its throughput).
- Interval: `mean +/- t * s / sqrt(n)` over those replication numbers, which `scripts/queueing.py summarize` computes.
  Never build an interval from the individual customer waits of one run: they are correlated and the interval will
  be far too narrow.
- Precision: if the half-width is too wide to decide the question, add replications (width falls as `1 / sqrt(n)`),
  or lengthen runs. If the interval already separates the options, stop.
- Report the seeds or the base seed so the run can be reproduced.

## Random streams

Give each random source its own `random.Random` (arrivals, service, failures) instead of the module-level generator,
so changing one source does not reshuffle the others. To compare two designs, give both the same seeds per replication
(common random numbers) and analyse the per-replication differences; the interval on the difference is much tighter
than comparing two independent sets.

## Checking the model

1. **Verification**: it does what you intended. Trace a few events by hand, check conservation (arrivals =
   completed + still in system), check degenerate cases (one customer, zero arrivals, infinite servers).
2. **Against a closed form**: set times exponential and compare with M/M/c or the finite-source formula; the
   interval should contain the exact value. Disagreement means a model bug, not an interesting finding.
3. **Against data**: compare model output to observed logs (a night's trial counts, measured waits) in the regime
   where both exist. If you have none, say so and label the inputs as assumptions.

## Sensitivity

Vary the inputs you are least sure of (rates, service-time spread, the distribution shape) over a believable range
and say whether the verdict holds. A verdict that flips within the range needs measuring, and the report should
name which input to measure. A narrow interval around a wrong model is still wrong.
