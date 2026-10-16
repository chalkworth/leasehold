# Bayesian rates (Beta-Binomial)

Read when: you want P(B beats A) for a pass/fail rate, a prior from earlier runs, or pooled evidence over a few small
cells. A Beta-Binomial needs no MCMC: the posterior is exact and `scripts/stats.py` computes it. Show a Wilson or
Clopper-Pearson interval beside it, since the posterior depends on the prior.

## Use

```bash
S="uv run --no-project --with numpy --with scipy python scripts/stats.py"   # run from the designing-experiments skill's folder
$S beta-posterior --successes 20 --trials 27 --prior-alpha 1 --prior-beta 1
$S prob-b-beats-a --a-successes 12 --a-trials 12 --b-successes 20 --b-trials 27 --margin 0.05
```

Library calls: `beta_posterior`, `probability_b_beats_a`, `probability_difference_beyond_margin` (pass
`better=BetterDirection.LOWER` for failure rates, where smaller is better).

## Rules

- **State the prior in the report.** Beta(1, 1) is uniform; Beta(0.5, 0.5) is Jeffreys. A prior built from earlier
  runs is honest only if those runs are exchangeable with the new ones (same code, scenario set and simulator).
- **A probability is not a decision.** Decide on a margin: "P(B better than A by more than 2 points)", with the margin
  being the smallest difference that matters. P(B beats A) of 0.9 over a trivial margin is a weak result.
- **Say "unresolved at this n"** when the margin probability is neither high nor low, and give the n that would resolve
  it from `references/planning-sample-size.md`.
- **Independence.** Binomial counts assume independent trials. For episodes within scenarios, aggregate to the
  scenario level or use a cluster bootstrap; feed the scenario count in as the trials (30 scenarios, not 300 episodes).
- **Rare events.** Zero failures in n trials has an upper bound, about 3/n at 95% (`rule-of-three`), whatever the
  prior.
- **Pooling a few cells** needs a hierarchical model: estimates shrink towards the group mean. That needs PyMC (not
  installed by this skill). Check it against the cluster bootstrap on the same data; if they agree, keep the
  bootstrap. Fitting a new group's prediction from group-level parameters is a different quantity from a seen group's
  rate.
- A Bayesian stopping rule still needs evaluating by simulation (`references/sequential-stopping.md`).
