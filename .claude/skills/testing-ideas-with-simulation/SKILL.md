---
name: testing-ideas-with-simulation
description: Use when a claim or question about throughput, waiting, queues, contention, capacity or staffing, timing, or rare-event rates, or about a stop, retry or abort rule or threshold for agent runs that could be tried on logged runs, would be costly to test for real, whether a person states it ("a second backend won't help", "two operators can cover six robots") or you are about to assert one yourself
---

# Testing Ideas with Simulation

A small model can support or kill a theory in minutes. The output is a verdict, not a paper.

## 1. Ask first (required)

Before any modelling, ask the user (AskUserQuestion where your CLI has it, otherwise in prose, then wait) whether to test the idea this way: your
recommended approach first (what you would model, how, rough time), then "No, just reason about it". An arithmetic
what-if ("what if one robot's battery drops 20%") gets a direct answer with no question and no invented percentages.

If the claim is whether a formula or derivative is right, use `checking-a-derivation` instead.

## 2. Procedure

1. **Frame it.** The decision, the comparator, the size of change that matters, each input tagged observed,
   modelled or assumed, and the result that would change the decision ("mean wait over 2 minutes", "no throughput
   gain from a second backend"). Several branches, unclear inputs, or logged or trial data → read
   `references/framing-the-what-if.md` now (required). A branch with no grounded check is reported as "not estimated".
   A rule, threshold or policy tried on logged runs goes to **Replaying logged runs** below; queues, capacity and
   timing continue at step 2.
2. **Try the closed form first** (read `references/closed-forms.md`). Little's law, utilisation, M/M/1, M/M/c with
   Erlang C, M/G/1, and the finite-source (machine-repair) model cover many theories. If arithmetic answers it,
   report and stop.
3. **Simulate only when a closed-form assumption breaks** (bursts, non-exponential times, priorities, shared
   resources, finite horizons): SimPy (`uv run --no-project --with simpy==4.1.2 python model.py`), or plain Monte
   Carlo when nothing waits; about 50 lines, the time unit beside every parameter.
4. **Replicate:** 10 or more seeded replications, one RNG per random source; warm-up discarded for steady state, or run
   to the natural end of a shift. Mean and 95% interval from replication-level numbers.
5. **Sanity-check the model** on one known case: the closed form in its valid regime, or observed data.
6. **Report the verdict:** supported, refuted or inconclusive; each modelled number with its 95% interval (or
   "closed form only") and the closed-form figure beside it; the assumptions that matter; the real measurement that
   would change the verdict. A model result is a prediction until measured.

**Replaying logged runs.** Apply the rule to each logged run as it happened. The unit is the run, clustered by task
id (or scenario). Interval: run the `stats.py` script in the designing-experiments skill's `scripts/` folder
(`uv run --no-project --with numpy --with scipy python stats.py cluster-bootstrap --values <1 or 0 per run>
--clusters <task id per run>`; `wilson --successes --trials` only when every run has its own task). Runs where the
rule would have acted are censored at that point: count them, and report everything after it (how a retry, successor
or handover would have gone) as "not estimated". Confirm a promising rule with a small trial on those cases.

## 3. Files

- Closed forms, Erlang C and when each applies, for queue, capacity and timing questions → read
  `references/closed-forms.md` (required at step 2).
- A numeric answer from a closed form or a replication summary:
  `python3 scripts/queueing.py --help` from this skill's folder (`mmc`, `mg1`,
  `finite`, `summarize` print JSON; tested against textbook values).
- Writing a SimPy model with a shared resource, priority, preemption, reneging, a buffer or a stock level → read
  `references/simpy-patterns.md`.
- Before reporting any SimPy number, or if results look wrong, hang, or end with `until` or `AnyOf` involved → read
  `references/simpy-pitfalls.md`.
- Choosing warm-up, run length, replication counts, or comparing two designs → read `references/simulation-method.md`.
