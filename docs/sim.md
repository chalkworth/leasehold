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

The wakeup latency the benchmark measures is not modelled. On the measured guest, the wakeup
p99 was 3 to 9 ms while at least two vCPUs were idle, and 15 to 30 ms once 7 of the 8 were busy
([benchmark](bench.md), "Why `nice` and CPU weights did not help"). Two measures stand in for
it. One is the busy-thread level the machine was at or under for 95% of the run (the jobs' busy
threads plus the background thread). The other is the share of time with at most one CPU idle,
which counts the time that level was at or above the CPUs less 1.1. That share turns on a hard
cutoff: two jobs of 3 workers on 8 CPUs keep about 2 × 2.82 + 1 = 6.64 threads busy with the
fitted curve, just under the 6.9 line, so it reads 0% for them, and a small change in the fit
moves it to 100%. Read it as unreliable, and the busy-thread level as the better guide.

## Calibration

The simulator gets the one like-for-like comparison in the measured runs backwards. In
`k8-full-d240-two-at-once`, the 0.2 config (weight 4, one job at a time on 6 slots) and two
jobs at once (weight 3) ran the same plan in the same session, minutes apart:

| | 0.2 config s | two at once s | two at once against 0.2 |
|---|---|---|---|
| measured | 714 | 813 | +14% |
| simulated, main fit | 714 | 581 | -19% |
| simulated, main fit with the swap term | 714 | 581 | -19% |
| simulated, fitted on the 0.2 runs only | 710 | 542 | -24% |

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
| 0.2 bench, 4 and 8 agents (`k4-d240`, `k8-d240`), leasehold | 9 | -6% to +5% | the same |
| 0.3 defaults, two rounds each (`v030-rounds`), leasehold | 8 | -7% to +3% | the same |
| two at once, the other leasehold conditions | 6 | -7% to +8% | -7% to +8% |
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
the unmanaged runs within 6% and moves the memory-heavy `lh-enforced` run from -2% to +8%. It
also puts four others 32 to 89% too slow, and it does not fix the reversal, so the study
leaves it off.

The same weights ran again later (`k8-full-d240-two-at-once-budget7g`, weight 3 with a 7 GiB
budget, which admits the same work here) in a quieter session, without swapping. That run took
638 s and the simulator says 663 (+4%). Swap alone does not explain the miss: the memory-heavy
two-at-once `lh-enforced` run read 2.9 GiB back from swap and simulates at -2%. The calibration
has one large miss among the leasehold conditions, and it is the comparison the gate repeats.

A check on runs the fit did not see. Fitted on the 0.2 runs alone (`--fit-on
k4-d240,k8-d240`), with `hold`'s times, the job-to-job spread and the swap term also taken from
those runs only, it reproduces 14 of the 15 held-out leasehold conditions within 14% on
makespan (-13.6% to +4.7%). The fifteenth is the swapped two-at-once run, at -33%
([`sim/holdout/calibration.md`](../bench/results/8cpu-16g/sim/holdout/calibration.md)). That
fit did not converge in 2000 evaluations, and two of its parameters ended at their bounds (SMT
yield 1.0, touch time 0.05 s a GiB): the 0.2 runs, with jobs on 4 and 8 workers only, cannot
separate the speedup curve from the SMT yield.

The misses lean one way. The hold-out fit puts both greedy two-at-once runs 12 to 14% too fast
(-13.6% and -11.6%) and the swapped one 33% too fast, and the main fit puts all three too fast
as well. On runs it did not see as on runs it did, the simulator is optimistic about jobs that
run at once.

How many threads a job keeps busy at 1 to 3 workers is the least determined part of the model,
and the share of time with at most one CPU idle depends on it. The earlier fit had `cpu` jobs
at `sigma` 0.01 and put 2-4 on 6 slots at 31 to 60% of the time with at most one CPU idle; the
converged fit has `sigma` 0.52, at about the same loss, and puts it at 0%. Two jobs of 3 workers
then keep about 6.6 to 6.8 threads busy at their p95 (with the background thread), against
the 6.9 the cutoff asks for. The busy-thread level is reported beside the share for that
reason.

## Policy study

On the 6-slot machine, every range that finishes much sooner than fixed 4 does it by running
two jobs at once, and those are the results the calibration gets backwards. On 8 and 16 slots
no range with a MIN below 4 finishes sooner than fixed 4. On 16 slots, undeclared memory-heavy
work goes over the budget under every weight, fixed 4 included. 4-8 is never behind fixed 4
and at most 10 s ahead.

`study` compares weight defaults on three simulated machines with the main fit and no swap
term: 6, 8 and 16 slots (8, 10 and 18 CPUs with 2 kept back, 2 GiB of memory a CPU, 6 GiB of
it used by other work, one busy thread of other work). There are 8 agents with the bench's
240 s plans, and three workloads:

* `light`: the bench's main mix, 35% `mem` jobs of 16% of the memory.
* `heavy`: 60% `mem` jobs of 23%, the 0.2 bench's size.
* `heavy-declared`: the same jobs, each declaring its size plus 512 MiB.

It has two parts:

* named: fixed weight 4 (today's default, the control); the ranges 2-4 and 1-4, each with the
  pool's headroom at `auto`, 0 and `auto` + 1; 3-3 and 3-4 with headroom 1; 2-2 and 2-3 with 3
  CPUs kept back (one slot fewer), which leave CPUs idle while two jobs run; and 4-8;
* screen: a 32-point Latin hypercube over the range's MIN (1 to 4), its MAX (MIN to the
  slots), the headroom (0 to 3) and the CPUs kept back (0 to 3).

Every configuration ran the same 30 plans (seeds 1 to 30). Each job's share of the job-to-job
spread comes from its seed and its place in the plan, so a job varies the same way under every
policy. Each named configuration is compared with the control seed by seed: the tables give the
mean of the configuration less the control on the same seed, with its 95% interval. Where that
interval includes 0, the study detected no difference. The study set no equivalence bound, so
such a result does not show that the two are the same.

A configuration is over budget when, in any replication, the memory its jobs really used
passed the budget. What the leases count never passes it; admission sees to that. Undeclared
memory counts at `assumed_per_slot` per slot, which can be less than a job uses. A replication
is untrusted when every slot was held for 10% of the run or more, or when the jobs' peak memory
left less than 5 GiB available (more than 5, 9 and 25 GiB on the three machines). Both are
where the measured two-at-once run went wrong. A configuration is untrusted when at least 10%
of its replications are. Over-budget configurations are dropped from the non-dominated sets.

All tables: [`sim/study/study.md`](../bench/results/8cpu-16g/sim/study/study.md); per-seed
results for the named configurations: `sim/study/runs.csv`.

Makespan, mean of 30 runs, and the paired difference from fixed 4 (headroom `auto` unless
given). The busy-thread level is the p95 of the jobs' busy threads plus the background thread,
out of the machine's CPUs.

| slots | workload | weights | makespan s | against fixed 4, s | wait p95 s | busy threads p95 | peak memory GiB | flags |
|---|---|---|---|---|---|---|---|---|
| 6 | light | 4 | 802 | control | 91 | 4.7 of 8 | 2.6 | |
| 6 | light | 2-4 | 641 | -162 ± 11 | 48 | 6.8 of 8 | 7.3 | untrusted |
| 6 | light | 3-3, headroom 1 | 645 | -158 ± 10 | 59 | 6.7 of 8 | 5.2 | untrusted |
| 6 | light | 3-4, headroom 1 | 646 | -157 ± 9 | 58 | 6.7 of 8 | 5.2 | untrusted |
| 6 | light | 2-3, 3 kept (5 slots) | 789 | -13 ± 4 | 76 | 5.0 of 8 | 5.2 | untrusted |
| 6 | light | 4-8 | 795 | -8 ± 2 | 91 | 5.4 of 8 | 2.6 | |
| 6 | heavy | 4 | 808 | control | 94 | 4.7 of 8 | 3.8 | |
| 6 | heavy | 2-4 | 690 | -118 ± 11 | 62 | 6.9 of 8 | 11.3 | untrusted |
| 6 | heavy | 3-3, headroom 1 | 648 | -160 ± 8 | 60 | 6.7 of 8 | 7.6 | untrusted |
| 6 | heavy | 3-4, headroom 1 | 650 | -157 ± 8 | 60 | 6.7 of 8 | 7.6 | untrusted |
| 6 | heavy | 2-3, 3 kept (5 slots) | 794 | -14 ± 3 | 77 | 5.0 of 8 | 7.6 | untrusted |
| 6 | heavy | 1-4 | 800 | -8 ± 13 | 60 | 7.0 of 8 | 19.4 | untrusted, over budget in 100% |
| 6 | heavy-declared | 4 | 808 | control | 94 | 4.7 of 8 | 3.8 | |
| 6 | heavy-declared | 2-4 | 770 | -38 ± 5 | 77 | 6.3 of 8 | 7.6 | untrusted |
| 6 | heavy-declared | 3-3, headroom 1 | 648 | -160 ± 8 | 60 | 6.7 of 8 | 7.6 | untrusted |
| 6 | heavy-declared | 1-4 | 951 | +143 ± 16 | 99 | 5.6 of 8 | 7.6 | untrusted |
| 8 | light | 4 | 588 | control | 53 | 8.4 of 10 | 6.5 | untrusted |
| 8 | light | 2-4 | 588 | -0 ± 7 | 30 | 8.8 of 10 | 10.7 | untrusted |
| 8 | light | 3-4, headroom 1 | 666 | +78 ± 5 | 63 | 7.5 of 10 | 6.5 | |
| 8 | light | 2-3, 3 kept (7 slots) | 673 | +85 ± 5 | 54 | 6.9 of 10 | 9.3 | untrusted |
| 8 | heavy | 4 | 607 | control | 57 | 8.4 of 10 | 9.5 | untrusted |
| 8 | heavy | 2-4 | 678 | +71 ± 7 | 53 | 8.8 of 10 | 17.3 | untrusted, over budget in 67% |
| 8 | heavy-declared | 4 | 607 | control | 57 | 8.4 of 10 | 9.5 | untrusted |
| 8 | heavy-declared | 2-4 | 715 | +108 ± 8 | 65 | 7.3 of 10 | 9.5 | untrusted |
| 8 | heavy-declared | 3-4, headroom 1 | 688 | +81 ± 5 | 67 | 7.5 of 10 | 9.4 | untrusted |
| 16 | light | 4 | 473 | control | 20 | 15.8 of 18 | 19.6 | untrusted |
| 16 | light | 2-4 | 529 | +56 ± 6 | 2.9 | 15.9 of 18 | 24.5 | untrusted, over budget in 37% |
| 16 | light | 3-4, headroom 1 | 494 | +21 ± 5 | 17 | 15.6 of 18 | 20.4 | untrusted, over budget in 7% |
| 16 | light | 4-8 | 463 | -10 ± 3 | 20 | 15.8 of 18 | 18.5 | untrusted |
| 16 | heavy | 4 | 504 | control | 33 | 15.9 of 18 | 33.6 | untrusted, over budget in 97% |
| 16 | heavy | 2-4 | 588 | +84 ± 9 | 29 | 15.2 of 18 | 44.3 | untrusted, over budget in 100% |
| 16 | heavy-declared | 4 | 539 | control | 42 | 12.4 of 18 | 25.4 | untrusted |
| 16 | heavy-declared | 2-4 | 733 | +194 ± 12 | 68 | 10.4 of 18 | 25.4 | untrusted |
| 16 | heavy-declared | 3-4, headroom 1 | 590 | +51 ± 8 | 50 | 12.4 of 18 | 25.4 | untrusted |
| 16 | heavy-declared | 4-8 | 530 | -9 ± 5 | 42 | 13.1 of 18 | 25.4 | untrusted |

The rows in detail:

* On 6 slots, every configuration that beat fixed 4 by much did it by running two jobs at once,
  with all 6 slots held for 60 to 86% of the run. These are the rows the calibration does not
  support.
* 3-3 with headroom 1 is the policy the measured two-at-once run used (weight 3, and `auto`
  headroom is 1 on 6 slots). The simulator puts it 158 s ahead of fixed 4; measured, it was 99 s
  behind.
* 3-3 and 3-4 with headroom 1 tie 2-4 on 6 slots for the light mix (645 and 646 s against 641).
  For the heavy mix they finish in 648 to 650 s against 690, and with declared memory in 648 s
  against 770. Their memory peaks at 5.2 and 7.6 GiB, against 7.3 and 11.3 for 2-4.
* With declared memory, a range that can grant 2 slots admits fewer jobs, and narrower ones: a
  declared total takes the same share of the budget whatever the size granted.
* 2-2 and 2-3 with 3 CPUs kept back hold the busy-thread level at about 5 of 8, so two CPUs or
  more stay idle. They gain little for it: 2-3 is 13 ± 4 s and 14 ± 3 s ahead of fixed 4 on the
  6-slot machine, and 74 to 197 s behind on the larger two. On 6 slots they are untrusted only
  for their memory peak (5.2 to 7.6 GiB, against the 5 GiB line).
* On 8 and 16 slots no range with a MIN below 4 finished sooner than fixed 4. 2-4 was 0 ± 7 s
  apart on 8 slots with the light mix (no difference detected) and 56 to 194 s behind in the
  other five cases. 3-3 and 3-4 were 21 to 94 s behind.
* Fixed 4 already runs two jobs at once on 8 slots and four on 16. A lease keeps the size it
  was granted, so jobs admitted narrow while others waited stay narrow after the line empties.
  Elastic leases are aimed at this, and the simulator can test them once `admission.targets`
  exists.
* Ranges cut the p95 wait where they did not cut the makespan: on 8 slots with the light mix
  from 53 to 30 s with 2-4, on 16 slots from 20 to 2.9 s.
* Without declared memory, the control itself went over the budget in 97% of the `heavy` runs
  on 16 slots, and 4-8 in all of them. 2-4 went over in 67% of the `heavy` runs on 8 slots,
  100% on 16, and 37% of the `light` runs on 16. 1-4 went over in 7 to 100% of the undeclared
  runs on every machine.
* A weight-2 lease of undeclared work counts twice `assumed_per_slot`, which is less than a
  `mem` job uses, so the small ranges go over more often than fixed 4. On 16 slots no weight
  tried keeps undeclared memory-heavy work within the budget; that work needs to declare its
  memory whatever the default.
* 1-4 with declared memory and headroom `auto` was 143 to 681 s slower than fixed 4 on every
  machine.
* 4-8 (today's 4 as MIN) was ahead of fixed 4 by 6 to 8 s on 6 slots for every mix. On 16 slots
  it was ahead by 10 ± 3 s for the light mix and 9 ± 5 s with declared memory. No difference
  was detected on 8 slots or for the undeclared heavy mix on 16.
* On 6 slots 4-8 changes almost nothing, since a lone job is granted 5 at most there (`auto`
  headroom keeps 1). The 6-slot gate machine cannot show its gain.
* Headroom moved 2-4's makespan by 7 s or less on 6 and 8 slots without declared memory.
  Elsewhere headroom 0 was faster than `auto` by 4 to 25 s.
* A configuration is non-dominated when no other one, within the budget, is at least as good on
  makespan, p95 wait, slots idle while something waited and the busy-thread level, and better on
  one. Because a lower busy-thread level counts, configurations that keep fewer threads busy stay
  in the sets however slow they are, and the sets are large (up to 31 configurations).
* On 6 slots the trusted members are fixed 4 (and 4-6 with headroom 2, which runs the same way)
  and two slower ranges with 3 CPUs kept back: 3-4 with headroom 1 and 3-5 with headroom 3, at
  929 to 949 s and a busy-thread level of 3.9 to 4.6. Every faster member is untrusted: 3-3 with
  headroom 1, 2-4 and 2-3 with 3 kept for some mixes, and configurations with 0 or 1 CPU kept
  back.
* On 8 slots the trusted members are, for the light mix, 3-3 and 3-4 with headroom 1 and MIN-3
  and MIN-4 ranges with 0 to 3 CPUs kept back (578 to 843 s); for the heavy mixes, only 4-6
  with 3 kept, at 868 s. On 16 slots they are MIN-4 ranges with 0, 1 or 3 kept, for the light
  mix only (461 to 506 s).
* MIN-1 ranges are in the sets for the light mix on 6 slots (1-4 with 3 kept, 718 s) and with
  declared memory on 8 and 16 slots, where they are the slowest members (1032 to 1364 s), kept
  for their low busy-thread level. All of them are untrusted.

The same named comparison with the hold-out parameters
([`sim/study-holdout-params/study.md`](../bench/results/8cpu-16g/sim/study-holdout-params/study.md))
gives the same sign wherever the main study detected a difference, with faster jobs
throughout. It puts 3-3 with headroom 1 on 6 slots 201 to 234 s ahead of fixed 4, and 2-4 71 to
239 s ahead.

## Candidates for the benchmark gate

The gate machine has 6 slots. There the simulator's ranking runs against the one measured
comparison: every range it puts ahead runs two jobs at once, and that policy was 14% slower
than one at a time in the session where the machine went into swap. The simulator does not show
whether 2-4 or 3-3 beats fixed 4 on 6 slots. It does show which runs would settle it.

Latency decides the gate, and the measurements already in hand predict that both ranges fail
it on 6 slots. The gate asks for a wakeup p99 under 1.5 times the control's. 3-3 with headroom
1 is the measured two-at-once policy, and in the same session as the control it had a wakeup
p99 of 19.0 ms against 4.5 ms (4.2 times) and a `git status` p95 of 454 ms against 111. 2-4
holds all 6 slots for 60 to 82% of the run. The simulator's latency measures do not contradict
this: the busy-thread level for both is 6.7 to 6.9 of 8, just under the 7 busy vCPUs at which
the measured wakeup p99 went to 15 to 30 ms. Their share of time with at most one CPU idle reads
0% only because of the cutoff described under Method.

Configurations worth running:

1. Fixed 4 with headroom `auto`, today's default, as the control.
2. 3-3 with headroom 1, which is the measured two-at-once policy. In the simulator it ties 2-4
   for the light mix and beats it for the heavy and declared mixes, with a lower memory peak
   (7.6 GiB against 11.3 for the heavy mix). The gate result for it is a direct retest of the
   813 s run, and on the latency already measured it is expected to fail.
3. 2-4 with headroom `auto`, the range the 0.4 plan named. Against 3-3 it can grant 2 slots,
   which in the simulator lowers the p95 wait for the light mix (48 against 59 s) and costs time
   with declared memory. It is expected to fail on latency for the same reason as 3-3.
4. 2-3 with 3 CPUs kept back (5 slots), aimed at the latency risk. It runs two narrow jobs and
   keeps at least two CPUs idle (busy-thread level 5.0 of 8). The simulator puts it only 13 to
   14 s ahead of fixed 4, so it tests whether a range can pass on latency, and it would not be
   chosen for speed.
5. 4-8 with headroom `auto` is demoted. It never lost to fixed 4 in the simulator, but its gain
   (6 to 8 s on 6 slots, 10 s on 16) is too small for a 6-slot run to see.

1-4 is not a candidate. It went over the budget without declared memory on every machine and
was the slowest configuration with declared memory on every machine.

Two ways to choose a default, for the maintainer to weigh:

* One default for every machine size. The only range the simulator never put behind fixed 4 is
  4-8, and on 6 slots it is fixed 4 in practice. A range with a MIN below 4 as the one default
  is behind fixed 4 on 8 and 16 slots in every case but one (2-4, 8 slots, light mix, no
  difference detected), and goes over budget without declared memory more often than fixed 4.
  So this option means keeping fixed 4, or 4-8. The gate needs only to confirm that 4-8 does no
  harm on 6 slots.
* A default by slot count: a range on machines of 6 slots or fewer, and fixed 4 above. This
  rests on the 6-slot rows the calibration does not support, and the latency measured so far
  predicts a failure for 3-3 and 2-4. The gate would have to show both the time gain and an
  acceptable wakeup p99 on the real machine, in several interleaved rounds of the control and
  the range with the outside memory recorded (one round each swung from 638 to 813 s). If only a
  range that keeps CPUs idle, such as 2-3 with 3 kept, passes on latency, the time it saves is
  small. This option also needs a run on a machine with 8 or more slots (16 would test the
  largest predicted loss) to confirm that the cut-off belongs where the simulator puts it.

## Limits

* One machine calibrated, a KVM guest with 8 vCPUs. The 8- and 16-slot machines in the study
  extrapolate its curve (half the CPUs as cores, the same SMT yield), its jobs (scaled to take
  the same time on every CPU of the machine, as `bench/run.py` scales them), and its point of
  swapping (5 GiB available).
* The calibration misses the measured run that went into swap by 29%, and the swap term does
  not fix it. Memory bandwidth, the page cache and disk are not modelled.
* Wakeup latency and `git status` time are not modelled. The busy-thread level stands in for
  them; the share of time with at most one CPU idle turns on a cutoff and is unreliable.
* The jobs are the benchmark's synthetic ones. Real builds and test suites scale worse and
  vary more.
* `mem` jobs use their memory from start to end; a real job's use grows and shrinks.
* The main fit uses the same runs it is compared with. Only the hold-out check is
  out-of-sample.
