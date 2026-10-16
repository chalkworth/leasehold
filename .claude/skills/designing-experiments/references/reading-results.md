# Reading results

Read when: you are given a results table, a sweep summary, a "run B beat run A" claim, or an agent's report of what it
did, and must say whether to act on it. The question is "can we act on this number?", not a critique essay. Check in
this order and stop at the first failure.

## Checklist

1. **What is the number, from how many units?** Find the unit (seed, episode, clip, scenario), n, the comparator and
   who ran it. A single run is one sample: its gain may be seed noise. A swing of 0.16 to 0.30 IoU across configs on
   one seed each is a sweep with no replication, not a ranking.
2. **Is there an interval, and is it of the right thing?** A mean with no interval, or an interval built from frames
   of one run, understates the uncertainty. Wanted: an interval over units, or the paired difference's interval.
3. **Were the arms paired?** Same seeds, scenarios and frames across arms. If not, the comparison carries both
   arms' noise and the paired analysis is unavailable.
4. **Leakage.** Are train and validation frames from the same drive, clip or scenario? Frame-level splits of
   consecutive frames leak; split by clip. Was the held-out set used for choosing the winner? A winner picked on the
   evaluation set is optimistic; the best of K sweep arms is optimistic even on fresh data.
5. **Overlapping intervals.** Overlapping intervals of two arms leave the question open, and a gap between them is
   not the test. Look at the interval of the difference. "Not resolved at this n" is a legitimate result: report the
   MDD.
6. **Same measurement?** Metric definition, evaluation data and split, timing, checkpoint and units must match
   between runs. The same metric name over a changed definition is not comparable.
7. **Nulls and failures.** Are failed, crashed or dropped runs in the denominator? Missing reporting is not evidence
   that something was not done; an absent log line does not prove a step did not happen.

## Agent claims ("tests pass", "fixed")

Claim, then artefact, then result: name the file or command output that shows it, and check it yourself when it is
cheap. "Tests pass" gets the test run, reported in one line. "No evidence found" is not "disproved"; say which.

## Honest figures (for review of someone else's plot)

- A bar with a zero baseline; a shared y axis across panels being compared; units on axes.
- Uncertainty named (Wilson 95%, bootstrap by clip) with n, drawn only where seeds exist. A shaded band on a single
  run is invented.
- Gaps shown where runs are ragged or missing, not lines bridged across them.
- Two encodings, not colour alone. Faint ghost of the baseline to compare against.
