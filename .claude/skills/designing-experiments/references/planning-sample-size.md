# Planning sample size

Read when: deciding how many seeds, pairs or trials to run, or saying what an existing n could have detected. `S` is
`scripts/stats.py`.

## Rules

1. **Effect size first.** Power comes from the smallest difference that would change the decision, not from the
   difference you hope for or just observed. If nobody can name it, ask. Size from the smallest difference that
   matters; small pilots exaggerate their gain.
2. **Report the minimum detectable difference (MDD) at the planned n.** "20 seeds per arm can only see a rise of 39
   points over a 10% baseline" is the useful statement. After a run, report the MDD and the interval; that is the power statement.
3. **Vary the assumed effect.** Give the n for two or three effects (say 1, 2 and 3 points), not one.
4. **Ask whether arms share seeds.** Pairing is the largest lever and the answer changes n by a factor of 3 or more.

## Rate difference

10% to 8% unsafe_rate at alpha 0.05 and 80% power needs about 3,200 runs per arm unpaired
(`S seeds-for-rate --baseline-rate 0.10 --minimum-detectable-difference 0.02` gives 3,213, and the exact power of the
test at that n, 0.801). Twenty seeds per arm cannot resolve it; say so plainly: they cannot detect any drop from a 10% baseline,
and only a rise of about 39 points
(`S mdd-rate --baseline-rate 0.10 --units-per-arm 20 --direction increase`).

### Pairing

Paired seeds need only the discordant pairs, those where exactly one arm failed. Pairs needed scale with the
discordant rate `d`, so the gain over unpaired is about `d_indep / d`.

- **Baseline, no gain.** With independent outcomes `d_indep = p1(1-p2) + p2(1-p1)`: 10% vs 8% gives
  0.1 x 0.92 + 0.08 x 0.9 = 0.164 (16%). At that rate `S pairs-for-paired-rate --discordant-rate 0.164
  --minimum-detectable-difference 0.02` gives 3,216 pairs, the same as the 3,213 unpaired.
- **Gain from correlation.** `d` is about `d_indep x (1 - phi)`, with phi the correlation between the arms'
  outcomes on the same seed. phi 0.5 gives d 8% and 1,568 pairs; phi 0.75 gives d 4% and 783 pairs.
- **Measure `d`.** Run a small paired pilot (50 to 100 shared seeds) or count discordant pairs in existing paired
  runs, then add margin for the pilot's error. Report the figure as "measured d = 0.05 over 80 pilot pairs". A
  guessed `d` is an assumption: label it, and give the no-gain n beside it.

Worked answer: "Unpaired, 10% to 8% needs 3,213 runs per arm. Paired saves runs only if the arms' outcomes correlate:
at a measured discordant rate of 4% it needs 783 pairs; at the independent-case 16% it needs 3,216 (no gain). Run
80 shared-seed pairs to measure it."

Near 0% or 100%, or with n under 50, the normal formulas are poor. Use `simulate_power` (below) or
`exact_rate_power` in `S`.

## Continuous metric

- Paired (same seeds or frames): get the SD of the per-pair differences from a pilot, not each arm's SD.
  `S pairs-for-delta --paired-delta-sd 0.04 --minimum-detectable-delta 0.02`.
- What a fixed n could detect, in metric units: `S mdd --standard-deviation SD --units n [--paired]`. For a
  110-frame validation set, use the SD of the per-frame IoU differences and `--paired`; for clips, `--units` is the
  number of clips.
- Unpaired: `S mdd` with the within-arm SD gives the detectable difference per arm; raise `--units` until it is
  small enough for the decision.

## Clustered runs

If episodes share a scenario, multiply the independent-unit n by the design effect `1 + (m - 1) * ICC`
(`S design-effect --cluster-size m --intraclass-correlation icc`). It assumes equal clusters and several of them. It
fails with few or uneven clusters; simulate. Take the ICC from data, not a guess: ICC 0.05 with 20 episodes per
scenario already inflates n by 1.95.

## Simulation fallback

For clustered, rare-event, adaptive or non-standard analyses:

1. Write `generate_and_test(generator, sample_size) -> bool`: draw one whole experiment from the assumed truth, run the
   exact analysis you will use, and return whether it rejects (alpha inside it).
2. `simulate_power(generate_and_test, sample_size, simulations, seed)` returns power with a Wilson interval on the
   simulation error. Use 1,000 or more simulations, and 5,000 for a stable figure near 80%.
3. Run the same function with no effect first: the rejection rate must be close to alpha. Then confirm a chosen n
   with a fresh seed; searching for n on a noisy estimate wobbles.
4. `paired_seeds(base_seed, count)` gives the shared seed list for arms; `generate_paired_binary_outcomes` simulates
   two arms on shared seeds with a stated `shared_fraction`.

Worked examples are in `tests/designing_experiments/test_stats.py` (power agreeing with the exact figure, and the false
win rate of peeking ten times).
