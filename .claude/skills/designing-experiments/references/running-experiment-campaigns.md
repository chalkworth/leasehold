# Running experiment campaigns

Read when: an agent runs a loop unattended (an overnight campaign, a tuning run, a sweep) that proposes a change,
tests it and keeps or discards it, or you are setting the stop rules for one. Method after NVIDIA
`nemo-rl-auto-research` and `nvflare-autofl` (Apache-2.0), rewritten. `S` is `scripts/stats.py`.

## Set before the first run

1. **Turn the stop rule into values the loop can check:** attempt count (`target_attempts`), an absolute
   `campaign_deadline`, a `per_run_timeout`, and a `target_metric`. Convert "8 hours" to a clock time at the start.
2. **Without a stop rule from the user,** run the baseline plus three low-risk ideas, then summarise and ask.
3. **Keep a ledger** of every attempt: index, branch or change id, parent commit, commit, metric, runs and seeds,
   elapsed time, status (`keep`, `discard`, `crash`) and a one-line description. Keep failed branches.
4. **Measure the noise floor before trusting any delta.** Run the unmodified baseline several times (5 or more), each
   re-seeding every process. `S noise-floor <baseline scores...>` gives the baseline mean, SD and the noise floor: the gap
   a single candidate run needs to clear before it is more than baseline spread. A candidate whose delta is inside the
   floor is noise. Where a candidate arm has several seeds, use the paired interval instead
   (`references/choosing-the-analysis.md`).

## During the campaign

- **Count attempts, not successes.** A "do 50 experiments" rule is met when 50 attempts exist, including crashes and
  discards.
- **Restate the stop state after every run:** "stop rule not met: 17 of 24 attempted, 6 h 12 min left". Enforce the
  per-run timeout, and stop when too little budget is left to start another run.
- **A smoke run proves plumbing only.** Keep an idea alive when a tiny or underpowered run did not move
  the metric. Test it at a meaningful size, with the seeds the noise floor needs, before marking it `discard`.
- **One noisy win is not a win.** Keeping a candidate because one noisy run beat the baseline is the
  stopping-rule mistake (`references/sequential-stopping.md`). Re-run it on the planned seeds, or accept it only after
  it clears the noise floor and, for a final claim, the planned analysis. Many candidates against one baseline is a
  sweep: the best of K flatters.
- **After context compaction or a handoff,** reload this skill and restate the objective, stop rule, current change and
  latest result before continuing.

## Plateau and family stall

- **Plateau:** `attempts_since_improvement(scores, noise_floor)` counts attempts since the best score last improved
  by more than the noise floor. At a limit (8 is a reasonable default) stop the exploit loop, change approach, or
  stop and report. Improvements inside the floor do not reset the count.
- **Family stall:** tag each attempt with its idea family (the parameter or mechanism it touches). If the last N
  attempts (6 is reasonable) are all one family and none cleared the floor, switch families: ten variants of one idea
  is one experiment repeated.
- **Crash blocker:** several consecutive crashes (6) mean the harness is broken; stop and report rather than
  continue.
- **Stop files:** honour a `STOP` file the user can create.

## Report

Baseline spread, attempts made, what cleared the floor and by how much (with intervals), what was inside the floor and
so unresolved, which families were tried, and why the campaign stopped. A campaign with no candidate beyond the noise
floor ends as "nothing resolved", not as its best number.
