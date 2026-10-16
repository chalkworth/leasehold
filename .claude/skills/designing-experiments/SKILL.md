---
name: designing-experiments
description: Use for genuine measured comparisons (skill trials, model and reviewer studies, A/B tests of prompts or configs): comparing runs, arms, seeds or models; deciding how many seeds or trials are needed; judging whether a change is real or noise; planning a campaign, sweep or A/B; reading a results table, sweep summary, leaderboard, or a claim that "X is best" or a tuned or chosen value is better; or deciding when to stop running.
---

# Designing Experiments

Answer the decision the reader has to make. Reply in a few lines: the **decision** (keep, revert, rerun, not shown
yet), the **number** with its interval and n, and the **next action**. Method detail comes last, and only as much as
the reader needs to trust the number.

✅ "Keep the new planner: unsafe_rate 10.2% → 8.1% (−2.1 pts, 95% −3.0 to −1.2), 3,200 paired seeds per arm. Next:
ship behind the flag."
✅ "Not shown yet: one seed per lr on 74 frames. Rerun the top two with 5 paired seeds each."

## Pick the path

- **Quick check** ("rerun it once to confirm the fix"): rerun it and answer plainly.
- **Someone else's result** ("lr 3e-4 is best", a sweep summary, an agent's claim): the verdict, the two or three
  other causes that matter, and the one run that would settle it → read `references/reading-results.md`.
- **A number that feeds a merge, benchmark, tuned value or report, or "how many seeds"**: the measured path.
- **Generating the runs themselves** (factorial or screening matrix, response surface, Latin hypercube sweep,
  block or cluster allocation schedule, Latin square) → use skill `laying-out-experiment-designs`.
- **An unattended loop, overnight campaign or tuning run** → read `references/running-experiment-campaigns.md`
  (required).

## Measured path

Three rules hold in every comparison, so keep them in the plan you agree with the user:
- Decide what result would change the plan, and pick held-out scenarios or clips before tuning; use them once.
- The run (seed, episode, scenario, clip) is the unit; frames and steps sit inside it.
- The same seeds and scenarios in every arm, and the run count fixed before starting.

Your first action is to delegate to a sub-agent (Sonnet or above) if your CLI supports one, with this prompt, then
pass its answer on in the reply shape above. If it does not, read the step file and follow it yourself:

```
Read steps/measured-comparison.md in the designing-experiments skill's folder first and follow it.
Decision: <what the answer changes>
Arms and metric: <arms>, <metric>; unit: <seed, scenario or clip>
Budget: <runs or hours>; data: <paths, or "plan only">
```

Write the step file's absolute path in the brief. When you are already a sub-agent, read the step file and follow it yourself.
