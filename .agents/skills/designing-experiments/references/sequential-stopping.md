# Stopping rules for experiments

Read when: you will look at results before the planned runs are finished, a loop repeats a trial until the
candidate wins, or you are deciding when an A/B is over. `S` is `scripts/stats.py`.

## The problem

Every look at accumulating data is another chance to cross the threshold by luck. Testing at alpha 0.05 after each of
10 batches and stopping at the first win gives a false-win rate near 0.2 (the exact figure depends on the
looks; `tests/designing_experiments/test_stats.py` simulates it). "Repeat until it passes or 10 repeats" has the same
flaw. Repeating a failed trial until it passes is a coin flip with several tosses.

## Options, in order of preference

1. **Fixed N.** Choose runs per arm from the smallest difference that matters (`references/planning-sample-size.md`),
   run them all, analyse once. Simple and unambiguous. Default.
2. **Two looks with O'Brien-Fleming boundaries.** Look at 50% and 100% of the planned runs. Stop early only if
   |z| >= 2.797 at the half-way look; otherwise finish and declare a win only if |z| >= 1.977 at the end (overall alpha
   0.05, two-sided). `S boundaries --looks 2`. Three looks: 3.471, 2.454, 2.004. The planned maximum is slightly
   larger than the fixed N.
3. **More looks or uneven timing.** `S boundaries --information-fractions 0.25 0.5 0.75 1.0`, set before the first
   run. Pocock (`--family pocock`) stops earlier but pays more at the end.
4. **Custom rules.** Simulate the whole procedure with `simulate_power` under no effect; the rejection rate must stay
   near alpha. Tune the rule until it does.

## Rules

- Fix the number and timing of looks, the boundary and the planned maximum before the first run. Adding a look
  because the effect is nearly significant spoils the guarantee.
- A boundary tests a z-statistic on the accumulated data: z = observed difference / its standard error using all units
  so far, and the information fraction is the share of planned units done.
- An early stop overstates the effect. Report the interval with the stopping context, and treat a barely-crossed
  early win as likely smaller.
- Stop for futility only by a pre-set rule (the interval excludes any difference that matters), not by feel.
- A nightly peek pays the tax. An overnight loop that checks every night needs the boundaries, not 0.05 each night.
- Bayesian posteriors carry the same problem: a stopping rule of "P(B beats A) > 0.95" still stops on luck.
  Evaluate the whole rule by simulation.
- Campaign stop rules (budget, deadline, plateau) are a different thing: see `references/running-experiment-campaigns.md`.
