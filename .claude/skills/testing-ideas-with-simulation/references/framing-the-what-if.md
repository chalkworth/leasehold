# Framing a what-if

Read when: the idea has several branches, unclear inputs, a choice among ways to check it, or rests on logged or trial data. Keep the result to a
few lines; this is a front end, not a report.

## Frame

One sentence each: the **decision** the answer feeds, the **comparator** (current setup, or the alternative), and
the **size of change that matters** (for example "a second backend must raise nightly trials by 20%, or it is not
worth it"). At most three branches: current, the adverse case, and one coupled second-order effect (drivers that move
together, such as denser traffic with more occlusion, change together).

## Tag inputs

| Tag | Meaning | Example |
| -- | -- | -- |
| observed | measured in logs or runs | trial duration from `started` and `ended` |
| modelled | produced by a model you checked | Erlang C wait at a measured arrival rate |
| assumed | nobody measured it | how many interventions per robot-hour |

An outcome that rests on assumed inputs is a conditional result; say so. Name the one observation that would flip the
decision and what it would read.

## Route each branch to the cheapest grounded check

| Branch is about | Check |
| -- | -- |
| Rates, capacity, waits with simple assumptions | Closed form (`closed-forms.md`) |
| Queues with bursts, priorities, preemption, finite horizon | SimPy or Monte Carlo (this skill) |
| A parameter's effect on a simulated system's behaviour | A seeded parameter sweep in that system's simulator |
| A rule, threshold or policy over logged agent runs | Replay it over the logs (SKILL.md, Replaying logged runs), then a small trial for what the replay cannot cover |
| An agent or loop change | A scenario x seed x repeat trial matrix |
| Plain arithmetic | Do the sum |

A branch with no grounded check available is reported as "not estimated": give no likelihood or percentage for it.

## Real data and calibration

Split the records into regimes before using them: backend, simulator type, and cadence-capped (a fixed schedule sets
the rate) versus uncapped (work sets the rate). Each regime gets its own rate, duration and capacity.

- Never carry a number from one regime to another. A duration from a fast simulator says nothing about a nightly
  rate set by a slower one, and a scaling efficiency nobody measured is an assumption, not a result.
- Headline the regime the question is about, and give the other regimes as context only.
- Before predicting, run the model on a period you observed (a past night, a past shift) and compare its output with
  what happened. If it misses, fix the model or report the prediction as unvalidated.
- Name every regime you leave out and say why, in one line each.
- A row that rests on an assumption nobody measured (for example that backends run independently, so two give twice
  the trials) is "not estimated", not a straight multiple of the one-backend figure.
