# Simulator: comparing weight defaults before the benchmark

[`bench/sim/`](../bench/sim/) is a discrete-event model of the [benchmark](bench.md): simulated
agents start jobs on one simulated machine, and leasehold's own admission code decides when
each job starts and with how many slots. It is for narrowing down which sizing rules and range
defaults are worth a real benchmark run. The benchmark decides; the simulator only ranks
candidates for it.

```sh
uv sync                                                    # SimPy and SciPy, the bench group
uv run python -m bench.sim run --weights 2-4 --reps 30     # one condition, 30 replications
uv run python -m bench.sim run --from-run bench/results/8cpu-16g/raw-k8-d240 --reps 10
uv run python -m bench.sim calibrate                       # against bench/results/8cpu-16g
uv run python -m bench.sim calibrate --fit                 # refit the parameters (about 3 min)
uv run python -m bench.sim study --reps 30                 # the policy study (about 2 min)
```

SimPy and SciPy are in the `bench` dependency group, which `uv sync` installs by default. The
leasehold package does not depend on them and never imports the simulator.

## Method

Agents and jobs. Each agent runs the steps [`bench/run.py`](../bench/run.py) plans for it
(`make_plan`: exponential think times, then a `cpu` or `mem` job, until its planned time passes
the duration), or the plan a measured run recorded in its `spec.json`, or a measured run's
`jobs.csv` replayed (`--replay`). Every agent is its own session; `--workload` mixes and the
study's machines are below. A job asks for a weight or a range MIN-MAX, declares no memory, a
total, or a size per slot (undeclared memory counts at `assumed_per_slot`, as in the daemon),
and has an amount of work in CPU-seconds.

Run time. On `n` workers alone, a job runs at Downey's speedup `S(n)` for its kind
(parameters `A`, the average parallelism, and `sigma`, its variance). When several jobs run at
once, the machine shares out its CPUs: `k` runnable threads (the jobs' busy threads plus
`background` threads of other work) get `capacity(k) / k` of a core each, where `capacity`
counts the first half of the CPUs as full cores and each CPU past them as `smt_yield` of a
core. That curve stands for SMT siblings, a host's oversubscribed vCPUs and the machine's
other load together; on the measured guest it is what makes two jobs of 3 workers take longer
than 4/3 of one job of 4. A job's work varies from job to job by a fitted lognormal spread.

Admission. On every request, every lease's end, and every tick (`tick_s`, 2 s), the simulator
builds the `State` the daemon's ledger would hold (held leases, the line, whose turn was when)
and the policy `MachineConfig.policy` gives the daemon, for the config `leasehold init --user`
writes for the machine. It then starts what `leasehold.admission.grants` admits, at the size
granted. The memory guard is the library's `Guard`, fed the machine's simulated available
memory (its memory, less other work's, less what the running jobs use); the allowance it
leaves (`memory_free`) and the overdue items are worked out as `Agent.policy` works them out.
`hold`'s own time (0.40 s from the call to the request, 0.11 s from the job's end to the
lease's end, medians of the measured runs) is added to every job.

Elastic leases. When `leasehold.admission.targets` exists, the `lh-elastic` condition calls it
at every decision and moves each lease to its target after a settle time. The library does not
have it yet, so that condition is skipped with a message.

Measures, per run: makespan; each job's completion time and wait (mean, p95); slot and CPU
utilisation; the share of the slots left idle while something waited; the share of the time
the machine had at most one CPU idle; Jain's index of the sessions' slot-seconds and the ratio
of the longest mean wait of a session to the shortest; the peak memory the leases counted and
the peak the jobs used. Replications with different seeds give a mean and a 95% interval
(Student's t over the replications). `run` writes `runs.csv`, `summary.json` and `timeline.csv`
(slots held, the line and memory over time, for the first replication).

The wakeup latency the benchmark measures is not modelled. The share of time with at most one
CPU idle stands in for it: on the measured guest, the wakeup p99 went from about 3 to 9 ms to
15 to 30 ms once 7 of 8 vCPUs were busy ([benchmark](bench.md), "Why `nice` and CPU weights
did not help").

## Calibration

`calibrate` replays all 34 measured conditions under
[`bench/results/8cpu-16g/`](../bench/results/8cpu-16g/) with each run's plan, the bytes its
jobs hashed and touched, its weights and memory budget, its load before the condition started
as background threads, and the memory other people's work used (the machine's memory less the
benchmark's limit and 2 GiB). `--fit` chooses eight parameters (the SMT yield, the hashing
rates of the two job kinds, the touch time per GiB, and `A` and `sigma` per kind) by
Nelder-Mead on the squared log error of each condition's median run time per job kind, over
the 24 leasehold conditions. Makespan, completion time and wait are not fitted.

The fit: `smt_yield` 0.44; `cpu` jobs `A` 7.7, `sigma` 0.01 (close to perfectly parallel up
to 8 workers, as the synthetic job is); `mem` jobs `A` 5.7, `sigma` 0.43 (their touch of
memory is serial); a job-to-job spread of 5.7%. The full table, 10 replications per
condition, is [`sim/calibration.md`](../bench/results/8cpu-16g/sim/calibration.md). In short:

| measured runs | conditions | makespan error, simulated against measured |
|---|---|---|
| 0.2 bench, 4 and 8 agents (`k4-d240`, `k8-d240`), leasehold | 9 | -6% to +4% |
| 0.3 defaults, two rounds each (`v030-rounds`), leasehold | 8 | -7% to +2% |
| two at once (`two-at-once`), the other leasehold conditions | 6 | -7% to +7% |
| two at once, `lh-enforced`, 3.4 GiB swapped in | 1 | -29% |
| without leasehold, 4 agents | 2 | 0% to +2% |
| without leasehold, 8 agents (1.7 to 10 GiB swapped in) | 8 | -29% to -1% |

23 of the 24 leasehold conditions are within 7% on makespan, and their mean wait is within 6
s. The one that is not is the two-at-once `lh-enforced` run, 29% off: two 2.5 GiB jobs beside
6 GiB of other people's work pushed the machine into swap, and the guard then held new work
back on memory pressure. The simulator models neither swap nor pressure (its guard sees
available memory only), so it ran that condition as if the memory had fitted. The same goes
for the conditions without leasehold at 8 agents, 17 to 29% off where they swapped most. So
the simulator reproduces the measured runs where memory stayed out of swap, and is optimistic
wherever more memory is in flight than the machine has. Results for such configurations below
are a ranking to test, and not predictions.

A check on runs the fit did not see: fitted on the 0.2 runs alone (`--fit-on
k4-d240,k8-d240`), it reproduced the 15 held-out leasehold conditions within 11% on makespan,
except the same swapped run (-32%)
([`sim/holdout/calibration.md`](../bench/results/8cpu-16g/sim/holdout/calibration.md)). Its
parameters came out different, though: the 0.2 runs only have jobs on 4 and 8 workers, which
cannot separate the speedup curve from the SMT yield, and two of the parameters ended at their
bounds. Makespans and waits are well determined by the data; how many threads a job keeps busy
at 1 to 3 workers is not, and that is what the at-most-one-CPU-idle measure depends on.

## Policy study

`study` compares weight defaults on three simulated machines with the fitted parameters: 6, 8
and 16 slots (8, 10 and 18 CPUs with 2 kept back, 2 GiB of memory a CPU, one thread of other
work), 8 agents with the bench's 240 s plans, and three workloads: `light` (the bench's main
mix: 35% `mem` jobs of 16% of the memory), `heavy` (60% `mem` jobs of 23%, the 0.2 bench's
size), and `heavy-declared` (the same jobs, each declaring its size plus 512 MiB). Every
configuration ran the same 30 plans (common random numbers). It has two parts:

* named: fixed weight 4 (today's default), the ranges 2-4 and 1-4 each with the pool's headroom
  at `auto`, 0 and `auto` + 1, and 4-8;
* screen: a 32-point Latin hypercube over the range's MIN (1 to 4), its MAX (MIN to the
  slots), the headroom (0 to 3) and the CPUs kept back (0 to 3).

A configuration is dropped when, in any replication, the memory the jobs really used passed
the budget. (What the leases count can never pass it; admission sees to that. Undeclared
memory counts at `assumed_per_slot` per slot, which can be less than a job uses.) The
remaining configurations are compared on makespan, p95 wait, the share of slots idle while
something waited, and the share of time with at most one CPU idle; the non-dominated set is
those no other configuration beats on all four. All tables, with 95% intervals:
[`sim/study/study.md`](../bench/results/8cpu-16g/sim/study/study.md).

Named configurations, mean of 30 runs (headroom `auto` unless given; the intervals on makespan
are about ±25 s, on p95 wait ±2 s):

| slots | workload | weights | makespan s | wait p95 s | slots idle while waiting % | at most 1 CPU idle % | memory past budget |
|---|---|---|---|---|---|---|---|
| 6 | light | 4 | 810 | 93 | 29 | 0 | no |
| 6 | light | 2-4 | 652 | 50 | 0 | 60 | no |
| 6 | light | 1-4 | 696 | 18 | 0 | 44 | in 23% of runs |
| 6 | light | 4-8 | 802 | 93 | 29 | 0 | no |
| 6 | heavy | 4 | 818 | 96 | 29 | 0 | no |
| 6 | heavy | 2-4 | 652 | 51 | 0 | 31 | no |
| 6 | heavy | 1-4 | 707 | 25 | 3 | 37 | in every run |
| 6 | heavy-declared | 4 | 818 | 96 | 29 | 0 | no |
| 6 | heavy-declared | 2-4 | 782 | 78 | 25 | 6 | no |
| 6 | heavy-declared | 1-4 | 967 | 99 | 40 | 1 | no |
| 8 | light | 4 | 587 | 53 | 0 | 37 | no |
| 8 | light | 2-4 | 597 | 30 | 0 | 43 | no |
| 8 | heavy | 4 | 611 | 58 | 0 | 12 | no |
| 8 | heavy | 2-4 | 612 | 33 | 0 | 12 | in every run |
| 8 | heavy-declared | 2-4 | 728 | 67 | 23 | 2 | no |
| 16 | light | 4 | 449 | 17 | 0 | 8 | no |
| 16 | light | 2-4 | 506 | 1.5 | 0 | 2 | in 20% of runs |
| 16 | light | 4-8 | 442 | 17 | 0 | 7 | no |
| 16 | heavy | 4 | 479 | 24 | 0 | 1 | in every run |
| 16 | heavy-declared | 4 | 534 | 42 | 16 | 1 | no |
| 16 | heavy-declared | 2-4 | 735 | 69 | 39 | 0 | no |
| 16 | heavy-declared | 4-8 | 526 | 41 | 15 | 1 | no |

What the simulator says, with the calibration's limits in mind:

* On the measured machine (6 slots), 2-4 is the one large change. Against fixed 4 it cut the
  makespan by about 20% (652 against 810 s) and the p95 wait nearly in half, and left no slot
  idle while something waited (29% with fixed 4, the two slots no second weight-4 job fits).
  It pays for that by filling all 6 slots: with one thread of other work, the machine then had
  at most one CPU idle for 31 to 64% of the time, the state in which the measured wakeup p99
  was 15 to 30 ms. The measured two-at-once runs, which also filled the 6 slots, had a wakeup
  p99 of 19 ms on a busy day and 9 ms on a quiet one.
* With declared memory, small ranges lose. A declared total does not shrink with the size
  granted, so a lease granted 1 or 2 slots holds as much of the budget as one granted 4, and
  fewer slots get done with it: 1-4 was 13 to 137% slower than fixed 4 on every machine, and
  2-4 17 to 38% slower on 8 and 16 slots (4 to 5% faster on 6).
* Undeclared, small ranges let more memory run than is counted: a weight-1 or weight-2 lease
  counts 1 or 2 times `assumed_per_slot`. 1-4 passed the budget in some or all runs on every
  machine for both undeclared mixes, 2-4 in 97 to 100% of the `heavy` runs on 8 and 16 slots.
* On 8 and 16 slots, fixed 4 already runs two or four jobs at once, and ranges cut the p95
  wait (53 to 30 s on 8 slots, 17 to 1.5 s on 16) without making the batch finish sooner: on
  16 slots 2-4 took 10 to 17% longer. A lease keeps the size it was granted, so jobs admitted
  narrow while others waited stay narrow after the line empties. Elastic leases are aimed at
  this; the simulator can test them once `admission.targets` exists.
* 4-8 (today's 4 as MIN) was never worse than fixed 4 within the intervals and was 1 to 2%
  faster on 16 slots, with the same memory and latency. On 6 slots it changes almost nothing,
  since a lone job is granted 5 at most there (headroom `auto` keeps 1).
* Headroom made little difference. 0 against `auto` against `auto` + 1 moved 2-4's makespan
  by under 2% on 6 and 8 slots without declared memory. Elsewhere 0 was 1 to 4% faster than
  `auto` and `auto` + 1 up to 2% slower, with the p95 wait moving the other way.
* The non-dominated sets are wide. They hold the configurations with 0 or 1 CPU kept back,
  which finish soonest with about 70 to 85% of the time at most one CPU idle, and, with 2
  kept, 2-4 and its neighbours (2-5, 2-6) and 3-3 on 6 slots, and fixed 4, 4-8 and other
  MIN-4 ranges on 8 and 16 slots. No MIN-1 range is in any set. On 16 slots with the
  undeclared `heavy` mix, no configuration kept its jobs within the budget.
* Jain's index of the sessions' slot-seconds was 0.89 to 0.95 in every configuration; the
  weight default changed little in how evenly the agents were served.

The same named comparison with the parameters fitted on the 0.2 runs only
([`sim/study-holdout-params/study.md`](../bench/results/8cpu-16g/sim/study-holdout-params/study.md))
gives the same order everywhere (2-4 ahead of 4 on 6 slots by 25 to 28%, behind on 16 slots
and with declared memory on 8 and 16; 1-4 last with declared memory), with faster jobs throughout. It gives a much
lower share of time with at most one CPU idle for 2-4 on 6 slots (1% against 60%), which is
the measure the data pins down least.

### Candidates for the benchmark gate

1. Fixed 4, today's default, as the control.
2. 2-4 with headroom `auto`, the one candidate the simulator expects to change the 6-slot
   result: about 20% shorter makespan and half the p95 wait. The benchmark has to show whether
   the wakeup latency stays acceptable with all 6 slots busy (the simulator cannot say, and the
   two-at-once runs suggest it may not), and whether the memory-heavy mix stays out of swap
   (where the simulator is optimistic). On 8 and 16 slots, and for work that declares memory,
   the simulator expects it to be no better or worse than fixed 4.
3. 4-8 with headroom `auto`, the safe range: never worse than fixed 4 in the simulator, the
   same memory counted and the same CPUs left idle, and a small gain where slots are free.

1-4 is not a candidate: it lost with declared memory and let undeclared memory past the budget
on every machine.

## Limits

* One machine calibrated, a KVM guest with 8 vCPUs. The 8- and 16-slot machines in the study
  extrapolate its curve (half the CPUs as cores, the same SMT yield) and its jobs, scaled to
  take the same time on every CPU of the machine as `bench/run.py` scales them.
* Swap, memory pressure (PSI), the page cache, memory bandwidth and disk are not modelled. The
  guard acts on available memory only. A configuration that puts more memory in flight than the
  machine has runs faster in the simulator than it would.
* Wakeup latency and `git status` time are not modelled; the share of time with at most one
  CPU idle is a proxy, and the least well determined measure.
* The jobs are the benchmark's synthetic ones. Real builds and test suites scale worse and
  vary more.
* `mem` jobs use their memory from start to end; a real job's use grows and shrinks.
* The calibration fit used the same runs it is compared with, apart from the hold-out check.
