# Choosing the analysis

Read when: picking the interval or test for a result you are about to report. Start from the unit of independence
(`SKILL.md` step 3), then find the data shape below. `S` is `scripts/stats.py`.

## Rules that decide most cases

- The unit sets n. Ten seeds across thirty scenarios is 10 units per arm if seeds are the replicates, and 30 clusters
  if scenarios are; the 300 episodes are not 300 independent trials.
- Pair when the arms can share seeds, scenarios or frames. Analyse the per-unit differences, not two separate
  intervals; the paired interval is much tighter.
- Overlapping intervals of two arms leave the question open, and non-overlap is not required for a difference.
  Compare the interval of the difference.
- Mean of independent groups: Welch (unequal variances) by default; pooled Student only with a reason.
- At 5 to 30 units go straight to the paired bootstrap, which needs no normality screen.
- Report the raw difference in the metric's units, and in baseline seed SDs. Report it signed: some libraries
  (pingouin's `ttest`) return an absolute Cohen's d, and their `power` column is observed power; replace it with the MDD.
- Frame-level metrics (IoU over 74 to 110 validation frames) are clustered by clip or drive. Bootstrap by clip.

## By data shape

| Data | Interval or test | Tool |
|---|---|---|
| Pass/fail rate, any n | Wilson | `S wilson --successes k --trials n` |
| Zero or very few events | Clopper-Pearson; for zero events the rule of three, about 3/n at 95% | `S clopper-pearson`, `S rule-of-three` |
| Two arms, pass/fail, same seeds | Exact McNemar on the discordant pairs; ignore agreeing pairs | `S mcnemar` |
| Two arms, continuous, same seeds or frames | Paired bootstrap of the differences | `S paired-bootstrap` |
| Frames within clips, episodes within scenarios | Cluster bootstrap by clip or scenario; under about 10 clusters say the interval is optimistic | `S cluster-bootstrap` |
| Two arms, continuous, different seeds | Welch t interval on seed-level numbers | scipy `ttest_ind(equal_var=False)` |
| Many arms or parameters | Holm on the p-values, or report every arm; the best of K is optimistic | `S holm` |
| One training seed per arm | No seed variance is measured. Report the difference as unresolved, not as a result | none |

## Modelling a sweep (statsmodels, when a table of arms or parameters needs one model)

Statsmodels is not a dependency of `stats.py`; use it in the analysis environment.

- Rates as counts: `smf.glm("unsafe ~ param_a + param_b", data, family=sm.families.Binomial())` fitted to
  (failures, successes) per cell, not one row per episode.
- Binary outcomes clustered by scenario: GEE with an exchangeable correlation (`smf.gee(..., groups="scenario",
  cov_struct=sm.cov_struct.Exchangeable(), family=Binomial())`), or a GLM with `cov_type="cluster"`. HC3 robust errors
  fix unequal variance, not clustering.
- `MixedLM` is for Gaussian outcomes only. Under about 10 clusters its random-effect variance is unstable; prefer the
  cluster bootstrap.
- Many tests: `statsmodels.stats.multitest.multipletests(p, method="holm")`.
- Paired pass/fail: `statsmodels.stats.contingency_tables.mcnemar(table, exact=True)`.
