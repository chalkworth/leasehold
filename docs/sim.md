# Simulator: comparing weight defaults before the benchmark

[`bench/sim/`](../bench/sim/) is a discrete-event model of the [benchmark](bench.md): simulated
agents start jobs on one simulated machine, and leasehold's own admission code decides when
each job starts and with how many slots. It is for narrowing down which sizing rules and range
defaults are worth a real benchmark run. The benchmark decides; the simulator only ranks
candidates for it, and the calibration below limits which of its rankings can be used.

```sh
uv sync --group bench                                      # SimPy, the bench group
uv run python -m bench.sim run --weights 2-4 --reps 30     # one condition, 30 replications
uv run python -m bench.sim run --from-run bench/results/8cpu-16g/raw-k8-d240 --reps 10
uv run python -m bench.sim calibrate                       # against bench/results/8cpu-16g
uv run python -m bench.sim calibrate --fit                 # refit the parameters (15 to 30 min)
uv run python -m bench.sim study --reps 30                 # the policy study (a few minutes)
```

SimPy is in the `bench` dependency group, which only `uv sync --group bench` installs; a plain
`uv sync` leaves it out. The leasehold package does not depend on it and never imports the
simulator. The fit's search (Nelder-Mead), the study's Latin hypercube and the t quantiles are
written in the standard library ([`bench/sim/search.py`](../bench/sim/search.py)), so the group
needs neither SciPy nor NumPy.

## Method

Agents and jobs. Each agent runs the steps [`bench/run.py`](../bench/run.py) plans for it
(`make_plan`: exponential think times, then a `cpu` or `mem` job, until its planned time passes
the duration), or the plan a measured run recorded in its `spec.json`, or a measured run's
`jobs.csv` replayed (`--replay`). Every agent is its own session. A job asks for a weight or a
range MIN-MAX and has an amount of work in CPU-seconds. It declares no memory, a total, or a
size per slot; undeclared memory counts at `assumed_per_slot`, as in the daemon.

Run time. On `n` workers alone, a job runs at Downey's speedup `S(n)` for its kind
(parameters `A`, the average parallelism, and `sigma`, its variance). When several jobs run at
once, the machine shares out its CPUs: `k` runnable threads (the jobs' busy threads plus
`background` threads of other work) get `capacity(k) / k` of a core each. `capacity` counts
the first half of the CPUs as full cores and each CPU past them as `smt_yield` of a core. That
curve stands for SMT siblings, a host's oversubscribed vCPUs and the machine's other load
together. A job's work varies from job to job by a fitted lognormal spread.

Admission. On every request, every lease's end, and every tick (`tick_s`, 2 s), the simulator
builds the `State` the daemon's ledger would hold (held leases, the line, whose turn was when)
and the policy `MachineConfig.policy` gives the daemon, for the config `leasehold init --user`
writes for the machine. It then starts what `leasehold.admission.grants` admits, at the size
granted. The memory guard is the library's `Guard`. It is fed the machine's simulated available
memory: its memory, less what other work uses, less what the running jobs use. The allowance
the guard leaves (`memory_free`) and the overdue items are worked out as `Agent.policy` works
them out. `hold`'s own time (0.40 s from the call to the request, 0.11 s from the job's end to
the lease's end, medians of the measured runs) is added to every job.

Swap. By default the simulator leaves swap out: jobs run as fast with their memory in swap as
in RAM. An optional swap term (`Params.swap_slowdown` and `swap_reserve_gib`) slows every
running job once their memory passes the machine's memory less other work's and a reserve, or
the jobs' own memory limit when that is lower. The guard then reads the share of time the jobs
lose as memory pressure. The calibration fits it and reports every condition with it and
without it; the study leaves it off, for the reasons in the calibration section.

Elastic leases. When `leasehold.admission.targets` exists, the `lh-elastic` condition calls it
at every decision and moves each lease to its target after a settle time. The library does not
have it yet, so that condition is skipped with a message.

Measures, per run: makespan; each job's completion time and wait (mean, p95); slot and CPU
utilisation; the share of the slots left idle while something waited; the share of the time
the machine had at most one CPU idle; the share of the time every slot was held; Jain's index
of the sessions' slot-seconds; the peak memory the leases counted and the peak the jobs used.
Replications with different seeds give a mean and a 95% interval (Student's t). `run` writes
`runs.csv`, `summary.json` and `timeline.csv` (slots held, the line and memory over time, for
the first replication).

The wakeup latency the benchmark measures is not modelled. The share of time with at most one
CPU idle stands in for it: on the measured guest, the wakeup p99 went from about 3 to 9 ms to
15 to 30 ms once 7 of 8 vCPUs were busy ([benchmark](bench.md), "Why `nice` and CPU weights
did not help").

## Calibration

The simulator gets the one like-for-like comparison in the measured runs backwards. In
`k8-full-d240-two-at-once`, the 0.2 config (weight 4, one job at a time on 6 slots) and two
jobs at once (weight 3) ran the same plan in the same session, minutes apart:

| | 0.2 config s | two at once s | two at once against 0.2 |
|---|---|---|---|
| measured | 714 | 813 | +14% |
| simulated, main fit | 713 | 580 | -19% |
| simulated, main fit with the swap term | 713 | 580 | -19% |
| simulated, fitted on the 0.2 runs only | 710 | 541 | -24% |

So every result below in which the slots are full most of the time, or in which the jobs hold
more memory than the measured machine had when it went into swap, is untrusted for ranking.
The study marks those rows, and most of the gains it finds are in them.

What `calibrate` does. It replays all 34 measured conditions under
[`bench/results/8cpu-16g/`](../bench/results/8cpu-16g/). Each replay uses the run's plan, the
bytes its jobs hashed and touched, its weights and memory budget, and its load before the
condition started as background threads. Other work's memory is the median over the run's
samples of the machine's memory less what was available and what the benchmark used: 3.9 to
6.9 GiB of the 15.6 GiB. `--fit` chooses eight parameters (the SMT yield, the hashing rates of
the two job kinds, the touch time per GiB, and `A` and `sigma` per kind). It minimises the
squared log error of each condition's median run time per job kind, over the 24 leasehold
conditions. Makespan, completion time and wait are not fitted. It then fits the swap term to
the 10 runs that read more than 1 GiB back from swap, with the rest held.

The fit converged: Nelder-Mead stopped on its tolerances after 1218 evaluations, started from
an earlier run of 801 evaluations that had not. Its loss is 0.0056 (an RMS error of 7.5% on the
median run times), and no parameter ended at a bound. The search, its loss and the bounds are
in [`sim/calibration.json`](../bench/results/8cpu-16g/sim/calibration.json); the table, 30
replications per condition, is
[`sim/calibration.md`](../bench/results/8cpu-16g/sim/calibration.md).

| measured runs | conditions | makespan error, no swap term | with the swap term |
|---|---|---|---|
| 0.2 bench, 4 and 8 agents (`k4-d240`, `k8-d240`), leasehold | 9 | -6% to +4% | the same |
| 0.3 defaults, two rounds each (`v030-rounds`), leasehold | 8 | -7% to +3% | -7% to +3% |
| two at once, the other leasehold conditions | 6 | -8% to +7% | -8% to +8% |
| two at once, `lh-enforced`, 3.4 GiB swapped in | 1 | -29% | -29% |
| without leasehold, 4 agents | 2 | 0% to +2% | the same |
| without leasehold, 8 agents (1.6 to 9.8 GiB swapped in) | 8 | -29% to -1% | -18% to +89% |

Why the two-at-once run is missed. Its jobs were 21% slower than the model makes them (median
`cpu` job 22.7 s against 18.7, `mem` job 17.6 s against 14.6), and its mean wait was 40 s
against 18. The machine went into swap while the benchmark used at most 5.1 GiB, well inside
its 7 GiB limit: other work held about 6 GiB, about 4 GiB was already in swap before the run,
and swapping started with about 5 GiB still available. The guard then held new work back on
memory pressure. The simulator's jobs use the same 5.1 GiB, so no memory limit set from the
measured numbers is passed, and the swap term never comes on in that run. A term strong enough
to reach it there (a reserve near 6 GiB and a slowdown near 1 a GiB) makes the unmanaged runs
two to four times too slow. The fitted term (0.71 a GiB past a 1.5 GiB reserve) brings three of
the unmanaged runs within 6% and moves the memory-heavy `lh-enforced` run from -2% to +8%, but
puts four others 30 to 89% too slow. It does not fix the reversal, so the study leaves it off.

The same weights ran again later (`k8-full-d240-two-at-once-budget7g`, weight 3 with a 7 GiB
budget, which admits the same work here) in a quieter session, without swapping. That run took
638 s and the simulator says 663 (+4%). The simulator reproduces two at once when the machine
stays out of swap and misses it when it does not. Which of the two the gate machine will be in
is the open question, and the simulator cannot answer it.

A check on runs the fit did not see. Fitted on the 0.2 runs alone (`--fit-on
k4-d240,k8-d240`), with `hold`'s times, the job-to-job spread and the swap term also taken from
those runs only, it reproduces 14 of the 15 held-out leasehold conditions within 13% on
makespan (-13% to +5%). The two greedy two-at-once runs are its worst there, at -13% and -12%.
The fifteenth is the swapped run, at -33%
([`sim/holdout/calibration.md`](../bench/results/8cpu-16g/sim/holdout/calibration.md)). That
fit did not converge in 2000 evaluations, and two of its parameters ended at their bounds (SMT
yield 1.0, touch time 0.05 s a GiB): the 0.2 runs, with jobs on 4 and 8 workers only, cannot
separate the speedup curve from the SMT yield.

How many threads a job keeps busy at 1 to 3 workers is the least determined part of the model,
and the share of time with at most one CPU idle depends on it. The earlier fit had `cpu` jobs
at `sigma` 0.01 and put 2-4 on 6 slots at 31 to 60% of the time with at most one CPU idle; the
converged fit has `sigma` 0.52, at about the same loss, and puts it at 0%. Take that measure as
undetermined.

## Policy study

`study` compares weight defaults on three simulated machines with the main fit and no swap
term: 6, 8 and 16 slots (8, 10 and 18 CPUs with 2 kept back, 2 GiB of memory a CPU, 6 GiB of
it used by other work, one busy thread of other work). There are 8 agents with the bench's
240 s plans, and three workloads:

* `light`: the bench's main mix, 35% `mem` jobs of 16% of the memory.
* `heavy`: 60% `mem` jobs of 23%, the 0.2 bench's size.
* `heavy-declared`: the same jobs, each declaring its size plus 512 MiB.

It has two parts:

* named: fixed weight 4 (today's default, the control); the ranges 2-4 and 1-4, each with the
  pool's headroom at `auto`, 0 and `auto` + 1; 3-3 and 3-4 with headroom 1; and 4-8;
* screen: a 32-point Latin hypercube over the range's MIN (1 to 4), its MAX (MIN to the
  slots), the headroom (0 to 3) and the CPUs kept back (0 to 3).

Every configuration ran the same 30 plans (seeds 1 to 30). Each named configuration is compared
with the control seed by seed: the tables give the mean of the configuration less the control
on the same seed, with its 95% interval. Where that interval includes 0, the study detected no
difference. The study set no equivalence bound, so such a result does not show that the two
are the same.

A configuration is over budget when, in any replication, the memory its jobs really used
passed the budget. What the leases count never passes it; admission sees to that. Undeclared
memory counts at `assumed_per_slot` per slot, which can be less than a job uses. A row is
untrusted when every slot was held for 10% of the run or more, or when the jobs' peak memory
left less than 5 GiB available (more than 5, 9 and 25 GiB on the three machines). Both are
where the measured two-at-once run went wrong. Over-budget rows are dropped from the
non-dominated sets.

All tables: [`sim/study/study.md`](../bench/results/8cpu-16g/sim/study/study.md); per-seed
results for the named configurations: `sim/study/runs.csv`.

Makespan, mean of 30 runs, and the paired difference from fixed 4 (headroom `auto` unless
given). "Untrusted" and "over budget" are as above.

| slots | workload | weights | makespan s | against fixed 4, s | wait p95 s | peak memory GiB | flags |
|---|---|---|---|---|---|---|---|
| 6 | light | 4 | 800 | control | 92 | 2.6 | |
| 6 | light | 2-4 | 639 | -161 ± 10 | 50 | 7.5 | untrusted |
| 6 | light | 3-3, headroom 1 | 643 | -158 ± 9 | 59 | 5.2 | untrusted |
| 6 | light | 3-4, headroom 1 | 644 | -156 ± 9 | 59 | 5.2 | untrusted |
| 6 | light | 4-8 | 793 | -7 ± 1 | 92 | 2.6 | |
| 6 | heavy | 4 | 806 | control | 95 | 3.8 | |
| 6 | heavy | 2-4 | 686 | -120 ± 10 | 61 | 11.3 | untrusted |
| 6 | heavy | 3-3, headroom 1 | 648 | -158 ± 8 | 61 | 7.6 | untrusted |
| 6 | heavy | 3-4, headroom 1 | 648 | -158 ± 9 | 60 | 7.6 | untrusted |
| 6 | heavy | 1-4 | 804 | -2 ± 14 | 60 | 18.8 | untrusted, over budget in 100% |
| 6 | heavy-declared | 4 | 806 | control | 95 | 3.8 | |
| 6 | heavy-declared | 2-4 | 767 | -38 ± 6 | 77 | 7.6 | untrusted |
| 6 | heavy-declared | 3-3, headroom 1 | 648 | -158 ± 8 | 61 | 7.6 | untrusted |
| 6 | heavy-declared | 1-4 | 947 | +141 ± 18 | 100 | 7.6 | untrusted |
| 8 | light | 4 | 587 | control | 53 | 6.4 | untrusted |
| 8 | light | 2-4 | 588 | +2 ± 6 | 30 | 10.3 | untrusted |
| 8 | light | 3-4, headroom 1 | 664 | +77 ± 7 | 63 | 6.5 | |
| 8 | heavy | 4 | 603 | control | 57 | 9.5 | untrusted |
| 8 | heavy | 2-4 | 675 | +72 ± 8 | 54 | 16.8 | untrusted, over budget in 57% |
| 8 | heavy-declared | 4 | 603 | control | 57 | 9.5 | untrusted |
| 8 | heavy-declared | 2-4 | 715 | +111 ± 7 | 65 | 9.5 | untrusted |
| 8 | heavy-declared | 3-4, headroom 1 | 687 | +83 ± 5 | 67 | 9.4 | untrusted |
| 16 | light | 4 | 474 | control | 20 | 18.7 | untrusted |
| 16 | light | 2-4 | 528 | +54 ± 9 | 3.6 | 24.3 | over budget in 33% |
| 16 | light | 3-4, headroom 1 | 491 | +17 ± 5 | 17 | 21.0 | over budget in 10% |
| 16 | light | 4-8 | 462 | -12 ± 5 | 19 | 19.1 | untrusted |
| 16 | heavy | 4 | 503 | control | 33 | 33.3 | untrusted, over budget in 93% |
| 16 | heavy | 2-4 | 591 | +88 ± 10 | 29 | 43.7 | untrusted, over budget in 100% |
| 16 | heavy-declared | 4 | 535 | control | 42 | 25.4 | untrusted |
| 16 | heavy-declared | 2-4 | 724 | +190 ± 15 | 68 | 25.4 | untrusted |
| 16 | heavy-declared | 3-4, headroom 1 | 584 | +50 ± 7 | 49 | 25.4 | untrusted |
| 16 | heavy-declared | 4-8 | 529 | -6 ± 5 | 42 | 25.4 | untrusted |

* On 6 slots, every configuration that beat fixed 4 by much did it by running two jobs at once,
  with all 6 slots held for 60 to 86% of the run. These are the rows the calibration does not
  support.
* 3-3 with headroom 1 is the policy the measured two-at-once run used (weight 3, and `auto`
  headroom is 1 on 6 slots). The simulator puts it 158 s ahead of fixed 4; measured, it was 99 s
  behind.
* 3-3 and 3-4 with headroom 1 tie 2-4 on 6 slots for the light mix (643 and 644 s against 639).
  For the heavy mix they finish in 648 s against 686, and with declared memory in 648 s against
  767. Their memory peaks at 5.2 and 7.6 GiB, against 7.5 and 11.3 for 2-4.
* With declared memory, a range that can grant 2 slots admits fewer jobs, and narrower ones: a
  declared total takes the same share of the budget whatever the size granted.
* On 8 and 16 slots no range with a MIN below 4 finished sooner than fixed 4. 2-4 was 2 ± 6 s
  behind on 8 slots with the light mix (no difference detected) and 54 to 190 s behind in the
  other five cases. 3-3 and 3-4 were 17 to 96 s behind.
* Fixed 4 already runs two jobs at once on 8 slots and four on 16. A lease keeps the size it
  was granted, so jobs admitted narrow while others waited stay narrow after the line empties.
  Elastic leases are aimed at this, and the simulator can test them once `admission.targets`
  exists.
* Ranges cut the p95 wait where they did not cut the makespan: on 8 slots with the light mix
  from 53 to 30 s with 2-4, on 16 slots from 20 to 3.6 s.
* Without declared memory, 2-4 went over the budget in 57% of the `heavy` runs on 8 slots, in
  100% on 16, and in 33% of the `light` runs on 16. 1-4 went over in 7 to 100% of the
  undeclared runs on every machine.
* The cause is the same for both: a weight-2 lease of undeclared work counts twice
  `assumed_per_slot`, which is less than a `mem` job uses. Neither range is a default for
  undeclared memory-heavy work on 8 or more slots.
* 1-4 with declared memory and headroom `auto` was 141 to 689 s slower than fixed 4 on every
  machine.
* 4-8 (today's 4 as MIN) was ahead of fixed 4 by 7 ± 1 s on 6 slots for every mix. On 16 slots
  it was ahead by 12 ± 5 s for the light mix and 6 ± 5 s with declared memory. No difference
  was detected on 8 slots or for the undeclared heavy mix on 16.
* On 6 slots 4-8 changes almost nothing, since a lone job is granted 5 at most there (`auto`
  headroom keeps 1). The 6-slot gate machine cannot show its gain.
* Headroom moved 2-4's makespan by 5 s or less on 6 and 8 slots without declared memory.
  Elsewhere headroom 0 was faster than `auto` by 6 to 17 s.
* Almost every row in the non-dominated sets is untrusted. On 6 slots the sets hold 3-3 and
  3-4 with headroom 1 and configurations with 0 or 1 CPU kept back. For the light mix they also
  hold 2-4, 2-2, 2-3 and three MIN-1 ranges with 3 CPUs kept back.
* On 8 slots the sets hold fixed 4, 4-8 and other ranges, mostly MIN-3 and MIN-4; on 16 slots,
  only MIN-4 ranges. The only trusted members are MIN-4 ranges with 0 or 1 CPU kept back, for
  the light mix on 8 and 16 slots.

The same named comparison with the hold-out parameters
([`sim/study-holdout-params/study.md`](../bench/results/8cpu-16g/sim/study-holdout-params/study.md))
gives the same sign wherever the main study detected a difference, with faster jobs
throughout. It puts 3-3 with headroom 1 on 6 slots 200 to 233 s ahead of fixed 4, and 2-4 73 to
240 s ahead.

## Candidates for the benchmark gate

The gate machine has 6 slots, and on 6 slots the simulator's ranking runs against the one
measured comparison. Every range it puts ahead there runs two jobs at once, the policy that was
14% slower than one at a time when the machine went into swap and about as fast as the
simulator says when it did not. So the simulator cannot tell the maintainer whether 2-4 or 3-3
is better than fixed 4 on 6 slots; it can only say which runs would settle it.

Configurations worth running:

1. Fixed 4 with headroom `auto`, today's default, as the control.
2. 3-3 with headroom 1, which is the measured two-at-once policy. In the simulator it ties 2-4
   for the light mix and beats it for the heavy and declared mixes, with a lower memory peak
   (7.6 GiB against 11.3 for the heavy mix). The gate result for it is a direct retest of the
   813 s run.
3. 2-4 with headroom `auto`, the range the 0.4 plan named. Against 3-3 it can grant 2 slots,
   which in the simulator lowers the p95 wait for the light mix (50 against 59 s) and costs time
   with declared memory.
4. 4-8 with headroom `auto` is demoted. It never lost to fixed 4 in the simulator, but its gain
   (7 s on 6 slots, 12 s on 16) is too small for a 6-slot run to see.

1-4 is not a candidate. It went over the budget without declared memory on every machine and
was the slowest configuration with declared memory on every machine.

Two ways to choose a default, for the maintainer to weigh:

* One default for every machine size. The only range the simulator never put behind fixed 4 is
  4-8, and on 6 slots it is fixed 4 in practice. A range with a MIN below 4 as the one default
  is behind fixed 4 on 8 and 16 slots in every case but one (2-4, 8 slots, light mix, no
  difference detected), and over budget without declared memory on 8 and 16 slots. So this
  option means keeping fixed 4, or 4-8. The gate needs only to confirm that 4-8 does no harm
  on 6 slots.
* A default by slot count: a range (3-3 with headroom 1, or 2-4) on machines of 6 slots or
  fewer, and fixed 4 above. This rests on the 6-slot rows the calibration does not support, so
  it needs the gate to show the gain on the real machine. That means several interleaved rounds
  of the control and the range, with the outside memory recorded, since one round each swung
  from 638 to 813 s. It also needs a run on a machine with 8 or more slots (16 would test the
  largest predicted loss) to confirm that the cut-off belongs where the simulator puts it.

## Limits

* One machine calibrated, a KVM guest with 8 vCPUs. The 8- and 16-slot machines in the study
  extrapolate its curve (half the CPUs as cores, the same SMT yield), its jobs (scaled to take
  the same time on every CPU of the machine, as `bench/run.py` scales them), and its point of
  swapping (5 GiB available).
* The calibration misses the measured run that went into swap by 29%, and the swap term does
  not fix it. Memory bandwidth, the page cache and disk are not modelled.
* Wakeup latency and `git status` time are not modelled. The share of time with at most one
  CPU idle is a proxy for them, and the fits leave it undetermined.
* The jobs are the benchmark's synthetic ones. Real builds and test suites scale worse and
  vary more.
* `mem` jobs use their memory from start to end; a real job's use grows and shrinks.
* The main fit uses the same runs it is compared with. Only the hold-out check is
  out-of-sample.
