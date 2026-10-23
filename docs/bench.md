# Benchmark: several agents on one machine

What leasehold does for the case it is built for (a few coding agents on one Linux machine, each
starting test runs and builds that want every CPU) measured against what a skeptic would try
first: nothing, `nice`, one systemd slice with a low CPU weight, and a fixed `-j` everywhere.
The script is [`bench/run.py`](../bench/run.py); the summaries are under
[`bench/results/`](../bench/results/).

## Results

One machine, so far: a KVM guest with 8 vCPUs, 15.6 GiB memory and 8 GiB swap, Debian 13,
kernel 6.12, systemd user manager with cpu and memory delegated. It is a working machine:
other people's test runs were going on beside the benchmark (load around 1.5 to 2 before each
condition), which is noise in every row. In the 0.2 runs and the two-at-once runs each
condition ran once; the [0.3 defaults](#the-03-defaults) comparison ran every condition twice,
in interleaved rounds. All used plans of 240 s per agent (not the default 600 s, to keep the
whole run under three hours). Full tables, with more
columns, are in [`bench/results/8cpu-16g/`](../bench/results/8cpu-16g/), the raw per-job and
per-second rows beside them.

Columns: **failed** jobs (all killed by the OOM killer); **makespan**; a job's **mean** and
**p95** time from its agent asking to its end, and the mean **wait** for a lease within it;
**wake p99**, the 99th percentile of how late a 10 ms sleep woke, in the jobs' cgroup (`same`)
and in a separate login session (`out`); **git p95**, `git status` time; **swap-in** during the
condition; **PSI**, the highest memory pressure (`some` avg10) seen; **CPU**, mean busy.

### 4 agents (25 jobs, 10 of them `mem`)

| condition | failed | makespan s | job mean s | job p95 s | wait mean s | wake p99 same ms | wake p99 out ms | git p95 ms | swap-in MiB | PSI max % | CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 0 | 308 | 14.9 | 27.5 | 0.1 | 19.2 | 18.9 | 289 | 3 | 0.0 | 53 |
| nice | 0 | 303 | 14.0 | 26.2 | 0.1 | 19.3 | 19.2 | 136 | 0 | 1.2 | 52 |
| slice | 0 | 303 | 14.0 | 25.5 | 0.1 | 19.2 | 19.2 | 127 | 2 | 0.0 | 53 |
| lh-advisory | 0 | 361 | 20.5 | 37.6 | 8.5 | 2.9 | 3.3 | 100 | 42 | 0.0 | 44 |
| lh-enforced | 0 | 358 | 19.2 | 42.9 | 7.6 | 3.4 | 3.9 | 94 | 24 | 0.0 | 43 |
| lh-advisory-greedy | 0 | 301 | 13.0 | 28.7 | 4.7 | 16.1 | 15.8 | 249 | 2 | 0.0 | 55 |
| lh-enforced-greedy | 0 | 304 | 13.3 | 25.8 | 4.7 | 18.0 | 18.0 | 177 | 13 | 0.0 | 56 |
| lh-hardcpu-greedy | 0 | 352 | 19.0 | 32.8 | 8.2 | 5.2 | 5.0 | 127 | 0 | 0.0 | 42 |
| static-jobs | 0 | 318 | 16.8 | 26.3 | 0.1 | 16.8 | 16.6 | 244 | 47 | 6.7 | 52 |

### 8 agents (52 jobs, 22 of them `mem`)

| condition | failed | makespan s | job mean s | job p95 s | wait mean s | wake p99 same ms | wake p99 out ms | git p95 ms | swap-in MiB | PSI max % | CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 0 | 566 | 37.8 | 71.1 | 0.2 | 25.4 | 19.2 | 981 | 2822 | 45.3 | 72 |
| nice | 5 | 581 | 41.5 | 70.8 | 0.2 | 26.9 | 19.2 | 1042 | 5020 | 68.5 | 74 |
| slice | 7 | 630 | 41.9 | 72.1 | 0.5 | 19.3 | 19.3 | 969 | 6076 | 73.0 | 70 |
| static-jobs | 9 | 579 | 42.3 | 65.8 | 0.2 | 27.1 | 19.4 | 1128 | 10015 | 47.8 | 76 |
| lh-advisory | 0 | 848 | 64.4 | 115.5 | 50.1 | 3.3 | 3.4 | 103 | 219 | 0.5 | 46 |
| lh-enforced | 0 | 840 | 62.7 | 107.1 | 48.7 | 3.3 | 3.4 | 113 | 97 | 0.5 | 46 |
| lh-enforced-greedy | 0 | 660 | 40.1 | 74.2 | 29.9 | 18.8 | 18.6 | 209 | 107 | 0.7 | 63 |
| lh-hardcpu-greedy | 0 | 789 | 56.8 | 103.0 | 43.8 | 5.1 | 4.7 | 125 | 61 | 0.2 | 46 |

(`lh-advisory-greedy` was left out at 8 agents to save time. With nothing running, the wakeup
probe's p99 is about 0.85 ms and `git status` takes about 70 ms.)

### Why `nice` and CPU weights did not help the wakeup probe here

A side measurement on the same machine: N busy loops, and the probe beside them in the same
cgroup, 20 s each.

| busy loops | nice of the loops | wake p50 ms | wake p99 ms | wake p99.9 ms |
|---|---|---|---|---|
| 0 | | 0.13 | 0.85 | 3.7 |
| 4 | 0 | 0.13 | 1.7 | 7.1 |
| 4 | 19 | 0.12 | 9.0 | 10.0 |
| 6 | 0 | 0.12 | 8.6 | 20.1 |
| 6 | 19 | 0.12 | 9.0 | 9.4 |
| 7 | 0 | 0.11 | 19.3 | 32.2 |
| 7 | 19 | 0.12 | 17.9 | 21.7 |
| 8 | 0 | 0.10 | 15.5 | 19.4 |
| 8 | 19 | 0.11 | 19.2 | 20.3 |
| 16 | 0 | 0.10 | 23.6 | 33.3 |
| 16 | 19 | 0.10 | 29.3 | 35.5 |

```sh
for i in $(seq 7); do nice -n 19 python3 -c 'while 1: pass' & done
python3 bench/probe.py wakeup --out /var/tmp/w.csv.gz & sleep 20; kill %8; kill %1 %2 %3 %4 %5 %6 %7
```

On this guest the tail of a 10 ms wakeup depends on how many CPUs are busy, not on the busy
work's priority: once 7 of 8 vCPUs are busy (the machine's own background load takes about
one), the p99 is 15 to 30 ms whether the loops run at nice 0 or nice 19, and the same holds
across cgroups with different `CPUWeight`s. Weights and nice decide who gets the CPU over a
scheduling period; they do not make a busy vCPU let go at once. This may be a property of
virtual machines (a vCPU's wakeup goes through the host) and needs a bare-metal run to say
more.

## Reading

- Only the conditions that leave CPUs idle kept the person's wakeup tail low: `lh-advisory`,
  `lh-enforced` (p99 about 3 ms against 19 to 27 ms) and `lh-hardcpu-greedy` (about 5 ms). They
  do so because of the slot budget: 6 slots of 8 CPUs, weight 4 a job, so one job runs at a time
  with 4 workers and at least 3 CPUs stay free. `nice` and a `CPUWeight=20` slice did not move
  the p99 at all on this machine (see above), and neither did `CPUWeight` on leasehold's own
  scopes once jobs used every CPU (`lh-enforced-greedy`). The probe in a separate login session
  fared no better than the one beside the jobs, so on this machine a separate cgroup bought
  nothing for latency either.
- At 8 agents without admission, two or three 3.65 GiB jobs overlapped: memory pressure peaked
  at 45 to 73%, 2.8 to 10 GiB was swapped back in, `git status` p95 went from 70 ms to about 1
  s, and in three of the four unadmitted conditions the OOM killer took 5 to 9 `mem` jobs inside
  the benchmark's limit (unmanaged was spared in its run; which jobs overlap shifts from run to
  run). Every leasehold condition finished all 52 jobs with pressure under 1% and `git status`
  under 210 ms. That came from admission alone: weight 4 on 6 slots lets only one heavy job run,
  whatever its memory. The memory guard never had cause to act (pressure stayed under 1%, at
  least 6.3 GiB available), and `MemoryHigh` was not used (no `--memory`).
- With well-behaved jobs (they take the 4 workers they are given), leasehold ran the same work
  at 4 agents with a 17% longer makespan and 29 to 38% longer mean job time, and at 8 agents
  with a 50% longer makespan and a job time 1.7 times as long (64 s against 38 s), most of it
  waiting for a lease (50 s on average, p95 about 100 s). The CPUs were 44 to 46% busy against
  53 to 72% unmanaged: one weight-4 job at a time on 6 slots leaves 2 slots idle that no other
  weight-4 job fits, and that is a third of the machine.
- A tool that ignores the job count is the case containment is for. Under leasehold it still
  waits its turn, so `lh-enforced-greedy` kept the memory result (no failures, pressure under
  1%) at near-unmanaged speed (job mean 40 s against 38 s at 8 agents; at 4 agents faster than
  unmanaged, 13.3 s against 14.9 s, since jobs that run one after another each finish sooner).
  But it used every CPU, so the wakeup tail was as bad as unmanaged. `hard_cpu = true` caps each
  scope at its weight in CPUs: that restored the latency (p99 about 5 ms) at about the cost of
  the cooperative case. Without `hard_cpu`, enforcement changed little against advisory on this
  machine; with it, it is what holds a greedy tool back.
- `nice` and a low-weight slice, the usual advice, did nothing here for latency, memory or
  throughput: their rows are unmanaged's within noise, and they had OOM kills in their 8-agent
  runs. A fixed `-j4` everywhere (`static-jobs`) did not help either: it makes each job smaller
  but not fewer, and at 8 agents it had the most swap and failures. What helped was deciding how
  many heavy jobs run at once.
- leasehold loses on throughput and on the time an agent waits: if you want the batch done
  soonest and do not use the machine meanwhile, unmanaged or plain serialization with greedy
  tools (`lh-enforced-greedy`) finished first. 0.2's default budget (2 CPUs kept, every build
  weighing 4 of 6 slots) is cautious, and 0.3 keeps it; two at once is an opt-in (below). An
  agent behind the Claude Code hook, which waits 20 s for a lease by default and then refuses,
  would have been refused 38 times out of 52 at 8 agents (and 4 out of 25 at 4).

## The 0.3 defaults

The config `init --user` writes in 0.3 keeps 0.2's weights (builds and test runs 4, so one at a
time on 6 slots) and adds memory counting (`[memory]`: budget the machine's memory less 25%,
each slot assumed to use the budget over the slots) and the memory guard that counts memory
instead of capping pools. These runs set it against unmanaged and against the config 0.2
wrote (`lh-enforced-v020`), on the same machine, 8 agents, 240 s plans.

Each mix ran as one invocation on one plan, every condition twice, the second round in reverse
order (`--rounds 2`), so the conditions saw the same hours of the machine's other load. That
load was not steady: about 6 GiB of the memory was in use by other people's work (so the
benchmark's limit was 7 GiB, swap 2 GiB), the load average before each condition ranged from
1.0 to 1.9, and the wakeup probe outside the benchmark varied with it between rounds. Read
differences smaller than the spread between a condition's own two rounds as noise. Summaries:
[`k8-full-d240-v030-rounds`](../bench/results/8cpu-16g/k8-full-d240-v030-rounds.md) (2.5 GiB
`mem` jobs, 52 jobs) and
[`k8-memheavy-d240-v030-rounds`](../bench/results/8cpu-16g/k8-memheavy-d240-v030-rounds.md)
(3 GiB `mem` jobs, 53 jobs; `lh-enforced-declared` has each declare its size plus 512 MiB).

Each cell gives round 1 / round 2.

| condition | failed | makespan s | job mean s | wait mean s | wake p99 same ms | git p95 ms | swap-in MiB | PSI max % |
|---|---|---|---|---|---|---|---|---|
| unmanaged | 0 / 0 | 585 / 729 | 40.2 / 57.9 | 0.1 / 0.2 | 23.2 / 26.6 | 852 / 1218 | 3829 / 1681 | 31.6 / 27.3 |
| 0.2 config | 0 / 0 | 841 / 842 | 63.4 / 63.3 | 49.4 / 49.2 | 3.6 / 6.3 | 101 / 141 | 532 / 19 | 0.2 / 0.0 |
| 0.3 defaults | 0 / 0 | 834 / 892 | 62.2 / 69.6 | 48.3 / 54.6 | 3.5 / 9.2 | 108 / 236 | 223 / 284 | 0.0 / 5.6 |

Memory-heavy mix:

| condition | failed (OOM kills) | makespan s | job mean s | wait mean s | wake p99 same ms | git p95 ms | swap-in MiB | PSI max % |
|---|---|---|---|---|---|---|---|---|
| unmanaged | 7 (29) / 7 (22) | 584 / 527 | 39.5 / 31.1 | 0.4 / 0.2 | 28.0 / 27.3 | 1746 / 1374 | 9301 / 6580 | 50.3 / 50.1 |
| 0.3 defaults | 0 / 0 | 701 / 707 | 47.4 / 48.2 | 36.3 / 37.0 | 9.0 / 9.2 | 232 / 232 | 286 / 368 | 5.2 / 6.0 |
| 0.3 defaults, memory declared | 0 / 0 | 716 / 693 | 49.0 / 46.1 | 37.7 / 35.2 | 8.9 / 9.2 | 190 / 219 | 150 / 93 | 5.5 / 1.5 |

What these say:

- The 0.3 defaults behave as 0.2's did. With the same weights, one heavy job runs at a time
  either way, and the two configs' rows are within each other's spread: 834 and 892 s against
  841 and 842 s, no failures, a wakeup p99 of 3.5 to 9 ms against unmanaged's 23 to 27 ms. The
  0.3 defaults' second round met more outside pressure than the 0.2 config's (pressure up to
  5.6%, the outside probe at 9 ms too), and was slower for it; nothing in these runs separates
  the two configs.
- Memory counting does not bind here, declared or not. At weight 4 on 6 slots only one heavy job
  runs, and an undeclared one counts 4 × 2 GiB = 8 GiB of a 11.7 GiB budget, so memory could not
  have admitted a second one either; the declared rounds took 693 and 716 s against 701 and 707
  s. What counting adds is for declared sizes, `[[standing]]` memory and the opt-in below.
- Against unmanaged, as at 0.2, leasehold had no OOM kills where unmanaged lost 7 of 53 jobs in
  each round of the memory-heavy mix, a `git status` p95 under a third of unmanaged's, and a
  wakeup p99 under half of it, for a makespan 20 to 45% longer (round against round).
- Wakeup latency moved with the machine's other load. Every leasehold row of the memory-heavy
  mix, and the second round of the main mix, sat at about 9 ms, inside and outside the benchmark
  alike; the first round of the main mix at 3.5 ms. It is the 0.2 result (about 3.3 ms) on a
  quiet machine, and higher on a busier one.

The README's headline table still shows the 0.2 run, which these runs neither contradict nor
improve on.

## Two heavy jobs at once

`[commands] heavy_weight = 3` and `light_weight = 2` on 6 slots let two builds or test runs
run at once, with memory counted as above. An earlier draft of 0.3 generated this setting by
default; these runs measured it then and did not show it to be better, so 0.3 left it opt-in.
The [`defaults-h1` gate](#pre-registered-comparisons), with 8 rounds on fresh seeds, later found
makespan and mean job time faster; wake p99 and git status p95 rose but stayed within their
1.5 margins. 0.4.0's `init` set it in new configs. The
[`memtight` run](../bench/results/8cpu-16g/memtight/README.md), exploratory and not a gate,
then gave each memory job 3.75 GiB under a 7 GiB cap, so two did not fit together: undeclared,
weight 3 lost 25 jobs to the OOM killer over 6 rounds (weight 4 lost none), and with the memory
declared it lost none and ran 1.06 times as long as weight 4. A new config therefore offers the
setting as commented lines again. The runs below are one run per condition, in sessions
earlier the same day as the runs above.

Like those, they are not directly comparable with the 0.2 tables at the top. Other people's work
was using about 6 GiB of the machine's memory that day, so the benchmark's limit was lowered to
7 GiB (swap 2 GiB) and the `mem` jobs to 2.5 GiB, which keeps the same shape: two overlapping
`mem` jobs fit, three do not. `lh-enforced-v020` is the 0.2 config and weight 4, run in the same
session for the comparison. The `budget 7g` row and the second greedy row ran later, in another
and quieter session, each as its own invocation (53 jobs: the plan is sized by a calibration
that came out slightly differently); the `7g` row set `[memory] budget = "7g"`, the memory the
jobs had. Summaries:
[`k8-full-d240-two-at-once`](../bench/results/8cpu-16g/k8-full-d240-two-at-once.md),
[`k8-full-d240-two-at-once-budget7g`](../bench/results/8cpu-16g/k8-full-d240-two-at-once-budget7g.md),
[`k8-memheavy-d240-two-at-once`](../bench/results/8cpu-16g/k8-memheavy-d240-two-at-once.md).

| condition | failed | makespan s | job mean s | job p95 s | wait mean s | wake p99 same ms | wake p99 out ms | git p95 ms | swap-in MiB | PSI max % | CPU % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 4 | 649 | 48.9 | 74.1 | 0.3 | 36.7 | 25.5 | 1638 | 2766 | 30.4 | 78 |
| lh-enforced, 0.2 config | 0 | 714 | 48.8 | 90.9 | 37.3 | 4.5 | 4.5 | 111 | 14 | 3.8 | 49 |
| lh-enforced, two at once | 0 | 813 | 62.9 | 131.7 | 39.6 | 19.0 | 19.3 | 454 | 3370 | 22.9 | 67 |
| lh-enforced, two at once, budget 7g | 0 | 638 | 39.3 | 72.7 | 20.7 | 9.1 | 9.1 | 164 | 25 | 0.0 | 60 |
| lh-enforced-greedy, two at once | 0 | 550 | 30.9 | 57.7 | 15.2 | (lost) | 19.1 | 276 | 162 | 1.1 | 71 |
| lh-enforced-greedy, two at once, again | 0 | 604 | 36.8 | 75.1 | 19.8 | 19.2 | 19.1 | 209 | 10 | 0.2 | 69 |

The first greedy run's in-cgroup wakeup probe was lost (a second stop signal interrupted its
write; `bench/probe.py` now ignores it), so it was run again on its own.

Memory-heavy jobs (52 jobs): 3 GiB each, `[memory] budget = "7g"`, weights cpu 3 and mem 2, so
a `mem` job is assumed to use 2.3 GiB unless it declares more. `lh-enforced-declared` has each `mem` job
declare its size plus 512 MiB (`hold --memory`).

| condition | failed | OOM kills | makespan s | job mean s | wait mean s | wake p99 same ms | git p95 ms | swap-in MiB | PSI max % |
|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 5 | 19 | 579 | 44.6 | 0.2 | 27.3 | 1007 | 3726 | 35.8 |
| lh-enforced (assumed memory) | 3 | 3 | 754 | 53.5 | 30.0 | 8.9 | 145 | 3016 | 36.0 |
| lh-enforced-declared | 0 | 0 | 697 | 48.3 | 26.7 | 8.8 | 125 | 43 | 0.9 |

What these say:

- In the one like-for-like comparison, two at once did worse than one at a time on every
  measure. The `0.2 config` and `two at once` rows ran in the same session, minutes apart, under
  the same outside load (about 1.9 before each). Against 0.2's one-at-a-time, two at once took
  813 s against 714 s, a mean job of 63 s against 49 s, read 3.4 GiB back from swap against 14
  MiB, and had a `git status` p95 of 454 ms against 111 ms, a wakeup p99 of 19 ms against 4.5 ms
  and memory pressure up to 23% against 4%. No job failed in either. Two 2.5 GiB jobs at once
  doubled the benchmark's own memory (5.0 GiB at its peak, against 2.5), and with what other
  people had in use the machine went into swap; the guard then held new work back (blocked items
  said "memory is short"), which is where the waits and the makespan came from. The run does not
  separate how much of the pressure was the benchmark's and how much others'.
- The `budget 7g` and second greedy rows do not show what the setting does. They ran later, in a
  quieter session (load 0.8 and 1.4 before them, against 1.9), with 53 jobs instead of 52, so
  their 638 s and 604 s cannot be set against the 714 s of the 0.2 config. Nor is the 7g row a
  test of the budget: with `assumed_per_slot = "auto"`, two weight-3 jobs count exactly the
  budget whatever it is, so `7g` and `auto` admit the same work here. What the row does show is
  that two at a time on a quiet machine ran without pressure, at a wakeup p99 of 9.1 ms.
- With nothing declared, the budget does not bind. `assumed_per_slot = "auto"` counts a request
  at its weight times the budget over the slots, which is never more than the budget while the
  weights fit the slots. So memory admission holds back only declared and `[[standing]]` memory;
  undeclared work is held back by its slots and, under pressure, by the guard. With two at once,
  declare memory for memory-heavy work.
- Declared memory is what protects. In the memory-heavy run a `mem` job used 3 GiB but was
  assumed to use 2.3, so three could run at once in 7 GiB, and 3 were OOM-killed (against 19
  unmanaged). Declaring the size (`hold --memory 3584m`, or a rule's `memory`) removed every
  failure and nearly all the swap, at a makespan 20% above unmanaged.
- Greedy tools are still the CPU problem. A tool that ignores the job count fills every CPU
  whatever its weight, so its wakeup tail is unmanaged's (19 ms) unless `hard_cpu` is on, as at
  0.2.

These runs did not show two at once to be better on this machine. The `defaults-h1` gate, with
more rounds and a pre-registered verdict, did.

## Simulator

[`bench/sim/`](../bench/sim/) replays these runs with leasehold's own admission code and a
fitted model of the jobs and the machine, to choose which weight defaults the next benchmark
run should compare. It reproduces 23 of the 24 leasehold conditions above within 8% on
makespan. The one it misses, by 29%, is the two-at-once run that went into swap, so it puts two
at once 19% ahead of the 0.2 config where the measured run was 14% behind. [Simulator](sim.md)
has the method, the calibration, a study of range defaults and its limits.

## Caveats

- One machine, a virtual one, and one run per condition except in the 0.3-defaults comparison
  (two interleaved rounds). Run-to-run variance is large for the
  memory results (whether three `mem` jobs overlap is a matter of seconds), smaller for the
  latency ones. The machine had other work on it throughout.
- Synthetic jobs. They scale perfectly with workers and cost little to oversubscribe, which
  favours the unmanaged conditions; real builds and test suites also contend for memory
  bandwidth, disk and the page cache.
- The cooperative result assumes jobs size themselves from the job variables `hold` sets.
  Many tools do (cargo, make with the jobserver, `pytest -n auto`, Go, OpenMP); one that does
  not behaves like the `-greedy` rows.
- A failed job's time is left out of the mean, which flatters the conditions with failures.
- Every condition ran under the benchmark's own memory limit, so running out of memory here
  meant an OOM kill inside the limit, not a frozen machine; without the limit the same overlap
  would have pushed the whole machine into swap.
- A second machine, ideally bare metal with more cores (16 or more CPUs, 32 GiB or more), would
  say whether the latency results are a property of virtual CPUs, and how the slot budget
  behaves when 2 kept CPUs are a smaller share. That run needs only the commands below and
  about three hours; `--publish` files it under its own machine class.

## Method

**Agents.** K simulated agents (4, then 8) each work through a list of steps fixed by a seed:
think for a random time (exponential, mean 30 s, cut at 120 s), then run one job and wait for
it, as an agent waits for its tool call. Each list runs until its planned time (thinking plus
each job's length alone on the machine) passes the duration. Every condition runs the same lists,
so every condition does the same work; the number of jobs is the same in every row.

**Jobs** ([`bench/job.py`](../bench/job.py)), 65% `cpu`, 35% `mem`:

- `cpu`: a fixed amount of SHA-256 hashing over an in-cache buffer, split over as many worker
  processes as the job may use: about 10 s alone on every CPU. The stand-in for `cargo test` or
  `pytest -n auto`.
- `mem`: touches 0.7 × RAM / 3 of private memory (3.65 GiB here: three at once pass 70% of RAM),
  then its workers hash that memory, about 6 s alone on every CPU. Its footprint does not shrink
  with fewer workers, like one large test process or a linker.

A job uses `LEASEHOLD_JOBS` workers when that is set, as a well-behaved tool sizes itself from
`CARGO_BUILD_JOBS`, `PYTEST_XDIST_AUTO_NUM_WORKERS`, `GOMAXPROCS` and the like (`hold` sets them
all from the lease); otherwise it uses every CPU. The `-greedy` conditions use every CPU
regardless, as a tool that reads none of them does.

The jobs are synthetic. They scale perfectly with workers and pay little for running more
workers than CPUs, which real builds do not; that favours the conditions that oversubscribe.

**Conditions**, one after another, each after the machine has settled (see Washout below):

| Condition | How each job starts |
|---|---|
| `unmanaged` | as it is, in the same cgroup as the person's shell |
| `nice` | `nice -n 19`, same cgroup |
| `slice` | `systemd-run --user --scope --slice=…` into one slice with `CPUWeight=20` |
| `static-jobs` | unmanaged, but told `LEASEHOLD_JOBS=4` (a fixed `-j4` everywhere, no coordination) |
| `lh-advisory` | `leasehold hold --weight 4 --session agentN`, `LEASEHOLD_CONTAIN=off`: queued and sized, nothing enforced |
| `lh-enforced` | the same with containment: each lease in its own scope, `CPUWeight` 100 per slot |
| `lh-advisory-greedy` | as `lh-advisory`, jobs ignore the job count |
| `lh-enforced-greedy` | as `lh-enforced`, jobs ignore the job count |
| `lh-hardcpu-greedy` | as `lh-enforced-greedy`, with `[contain] hard_cpu = true` (`CPUQuota` = weight CPUs) |
| `lh-enforced-declared` | as `lh-enforced`, each `mem` job declaring its size plus 512 MiB (`hold --memory`) |
| `lh-enforced-v020` | as `lh-enforced`, on the config leasehold 0.2.0's `init --user` wrote, weight 4 |
| `lh-jobserver` | as `lh-enforced`, through `hold --jobserver`; each job is a client of its lease's jobserver (`job.py --jobserver`); a range stays the size it was granted (`hold --no-elastic`, where the installed leasehold has elastic leases) |
| `lh-elastic` | as `lh-jobserver`, with elastic leases (`hold --elastic`); stops before anything runs if the installed leasehold has none |

The leasehold conditions (all `lh-` ones but `lh-enforced-v020`) take parameters after an `@`,
so one condition can be run at several settings, for example `lh-enforced@w=2-4,h=1`:

| Parameter | What it sets |
|---|---|
| `w=N` or `w=MIN-MAX` | what every job asks `hold --weight` for; `w=N` also sets the config's `[commands] heavy_weight` |
| `h=N` | `[pools.work] headroom` |
| `grow=MODE` | `[elastic] headroom`, `lh-elastic` only: how much of the pools' headroom a growing lease leaves free, `keep` (all of it, the default), `leave-one` (one slot) or `use` (none) |
| `kept=N` | CPUs kept back: the config is rendered for `CPUs - N` slots, which sets the ceiling, both pools' `max` and the work pool's reservation as `init` would |
| `mem=declared` | each `mem` job declares its size plus 512 MiB, as in `lh-enforced-declared` |
| `budget=SIZE` | `[memory] budget` (otherwise `--memory-budget`, else `auto`) |
| `arm=TAG` | nothing; it tells two arms of one condition apart, as in an A/A run |

In `--conditions`, a part with `=` and no `@` belongs to the condition before it:
`unmanaged,lh-enforced@w=3-3,h=1,kept=3` is two conditions. A range with and without elastic
leases, and an elastic lease at each `grow`, are `lh-jobserver@w=2-8`, `lh-elastic@w=2-8`,
`lh-elastic@w=2-8,grow=leave-one` and `lh-elastic@w=2-8,grow=use`. Each condition has one spelling,
with its parameters in the order above and `w=3-3` written `w=3`, and a directory name such as
`lh-enforced_w3_h1_kept3-r2`. The jobserver client cuts its work into 4 tasks per CPU. It
always runs one task on the token it holds by being started, and runs each further task only
on a token read from the jobserver, which it writes back when that task ends. How many tasks run
at once then follows the tokens the jobserver holds, so a lease that grows or shrinks shows up
in the job.

leasehold runs as a private user-mode daemon on a socket of its own, with the config `leasehold
init --user` writes for this machine: 6 slots of 8 CPUs (2 kept for the person), pool `work`
reserving 3, the memory guard on. Every job asks for weight 4, what the built-in `[commands]`
rules give `pytest`, `cargo test`, `make` and the other test and build runners, so a lease gives
`LEASEHOLD_JOBS=4`. The enforced conditions put the lease scopes in one session slice under
`leasehold.slice` (`LEASEHOLD_SLICE`), as `leasehold run` would for one session; the jobs call
`hold` directly, as the PATH shims do.

**Measured**, per condition:

- makespan (first agent start to last job end); each job's completion time from the moment its
  agent asked; under leasehold the wait for the lease, and the run time after it;
- wakeup latency: a process that sleeps 10 ms in a loop and records how late it wakes. `same`
  runs in the jobs' own cgroup when they have no other (a shell in the terminal the agents run
  in, or an editor that runs the agent); `outside` runs in the login session the benchmark was
  started from, a cgroup of its own. A late wakeup is what makes typing and scrolling stutter.
- `git status` time in a 50 000-file repository every 5 s, from the jobs' cgroup;
- OOM kills (`memory.events` of the benchmark's cgroups), swap-in (`pswpin`), memory pressure
  (PSI `some` and `full` avg10, sampled each second), CPU busy and steal.

**Safety.** Everything the benchmark runs is held to one memory limit, `MemoryMax` of
min(65% of RAM, available − 2 GiB) with `MemorySwapMax` 2 GiB: the harness's own scope for the
conditions whose jobs run in it, and the jobs' slice for `slice` and the enforced conditions.
Running out of it makes the kernel kill a job (jobs raise their OOM score), not the person's
session. The harness also stops every job if the machine has under 512 MiB available or a
memory `full` pressure over 60% for 10 s.

**Rounds and order.** A round runs every condition once on one plan. Its seed is
`--seed-base` plus the round number, the same for every condition in the round and new each
round (without `--seed-base`, round 1 has `--seed`). The order within a round follows a
Williams design: with two conditions AB, then BA; with n, a cycle of n orders (2n when n is
odd) in which each condition runs in each position equally often and follows each other
condition equally often. Whatever one run leaves behind (a warm page cache, pages still coming
back from swap) then falls on every condition alike. Round r takes the order at position r - 1
in that cycle (modulo its length), so a round's schedule never depends on how many rounds
there are. The order is fully balanced only over a whole number of cycles: 2 rounds for two
conditions, 6 for three, 10 for five. `compare` says so when the rounds it reads are not a
whole number of cycles.

`--round N --dir DIR` runs round N alone into DIR. The first invocation calibrates and writes
`spec.json`. Under `args` go the run's sizes: the preset and duration, agents, `--mem-gib`,
`--cap-gib`, `--swap-gib`, `--weights`, `--memory-budget`, `--slice-weight`, and whether
`--git-repo` was given. Under `harness` go the settings that change what a run measures:
`--no-scope`, `--no-wait`, `--drop-caches` and `--allow-dirty`. The file also records the
commit, whether tracked files had uncommitted changes, the leasehold version and the machine.

Later invocations reuse `spec.json`, and refuse to run when anything recorded there differs:
another schedule (conditions, seed base, agents, preset or workload), other sizes or harness
settings, another commit, checkout state or leasehold version, or another machine (its class,
CPUs, memory, swap, kernel or virtualisation). A runner can run one round per invocation and
lose at most a round when it stops. A run that is run again moves its old directory to
`DIR/superseded/`.

A run refuses to start when tracked files have uncommitted changes, since the recorded commit
would not be the code that ran. `--allow-dirty` runs anyway and is recorded. `--dry-run`
skips the check, and so does a `--tiny` smoke run that names neither `--dir` nor `--round`.

A run that ran is still invalid if its inner process failed, if a thread of the harness (an
agent or the sampler) raised, or if the number of jobs that ran is not the number its plan
has. A failing thread aborts the run at once, so a partial run is never recorded as a whole one.

**Washout.** Before each run, after `--settle` seconds (20), the harness waits until the
machine is quiet:

- load at most a quarter of the CPUs (at least 2);
- CPU pressure under 10%;
- memory pressure under 0.5% (in 0.3 it was 1%);
- at most 16 pages read back from swap over the last 30 s;
- dirty and writeback pages under 64 MiB together.

It reads these every 5 s. A source it cannot read counts as not quiet, with the reason.
Before the first run, the harness checks that it can read all of them (pressure needs a kernel
with PSI), and stops if it cannot; `--no-wait` skips the wait and the check.

`--drop-caches` also writes back dirty pages and empties the page cache before each wait. That
needs root or passwordless sudo for `tee /proc/sys/vm/drop_caches`, and the harness checks for
it before it starts. If the wait reaches
`--max-wait` (600 s) first, that attempt is marked invalid and the harness waits again, once;
if the machine is still not quiet, the run is left invalid and does not run. Invalid attempts
go to `invalid.jsonl` and the summary.

**Manifest.** Each run's directory has a `manifest.json` with:

- the git commit of the checkout and whether its tracked files had changes, and the leasehold
  version;
- the condition, its parameters, and the config its daemon ran on (also in `config.toml`, with
  the ledger named only `ledger`);
- what its jobs asked `hold` for;
- the round, the seed and the run's place in its round;
- the kernel release, CPU count, memory, swap, virtualisation and Python version;
- the machine class: `--machine-class` (e.g. `kvm-8cpu-16g`) or `<CPUs>cpu-<GiB>g`. It also
  names the results directory and the `--publish` directory;
- the washout attempts, whether the run is valid, and each `--note` (pinning, clock boost).

Neither the manifest nor `spec.json` records the host's name, the CPU model or a path on the
machine: the config is named `bench`, and `spec.json` says only whether `git status` was timed.

## Pre-registered comparisons

A comparison that decides something is written down before it runs, as a gate file in
[`bench/gates/`](../bench/gates/), and judged afterwards by `compare`, which needs only the
standard library:

```sh
uv run python bench/run.py compare DIR [DIR ...] --gate bench/gates/defaults.toml
uv run python bench/run.py compare DIR [DIR ...] --gate bench/gates/defaults.toml --blind-sd
uv run python bench/run.py compare AA-DIR --gate bench/gates/aa.toml --plan bench/gates/defaults.toml
```

A gate file has:

- `[gate]`: `name`, `kind` (`confirmatory`, `screen` or `aa`), and `intent`, `objective`,
  `stop` and `decision`, which head the report;
- `[arms]`: the `control` and the `candidates`, as conditions;
- `[[workloads]]`: each with a `name` (a run's `--workload`, or its preset) and the `args` it
  runs with, and optional `params` added to every arm in it;
- `[rounds]`: `planned`, the `seed_base` the runs must use, and for a confirmatory gate
  `look_after` and `extend_to`, the blind look;
- `[plan]`: the rule that sets the rounds and margins from an A/A run (below);
- `[test] alpha` (one-sided), `[[metrics]]` (each a summary column and its `margin`), `[hard]`
  (`failed_jobs`, `oom_kills`, `psi_max`, `psi_over_control`) and `[order] sequential`.

**What `compare` refuses.** `compare` judges only results it can pool, and says why it refuses
the rest:

- a results directory whose seed base is not the gate's, so a pilot's rounds are never pooled
  with a gate's;
- a workload the gate does not name, or recorded `args` that differ from that workload's;
- two runs of one condition in one round of one workload, as when a directory is passed twice;
- runs from more than one commit, checkout state, leasehold version, machine class or machine
  (CPUs, memory, swap, kernel, virtualisation), or directories with different harness settings;
- a round whose control and candidate ran different seeds.

**Which rounds count.** The unit is the round. A round counts for a candidate when its control
run and its candidate run are both valid and the control broke no hard rule. A round that does
not count is listed in the report with the reason. The verdict uses exactly the first N rounds
that count, where N is what the gate asks for. Rounds after those are listed as extra and never
enter the verdict. N is fixed once the blind look has been taken (below), and from then on
running more rounds cannot change the verdict.

A run that was run again keeps its old directory under `superseded/`. The report lists each
superseded run in the rounds it judges, with the reason it was replaced. Its hard-rule breaks
still count: a superseded candidate run with an OOM kill fails the candidate, and a superseded
control run with one makes its round not count. A rerun cannot hide a break.

**The test.** In each of the N rounds, `compare` takes d = ln(candidate / control) for each
metric. Over the rounds it gives the mean and SD of d, and a one-sided upper bound,
mean + t(1 - alpha, N - 1) × SD / √N, as a ratio. A metric passes when that bound is under its
margin. A candidate passes when every metric passes on every workload. This is an
intersection-union test: each metric is tested at alpha and all must pass, so the metrics need
no correction for their number. With `sequential = true`, a second candidate is tested only if
the first passed.

**Hard rules.** These are checked on every candidate run up to the last of the N rounds, whether
its round counts or not. A failed job, an OOM kill or an abort breaks one. So does memory
pressure: PSI `some` max above `psi_max` (10%) and above the control's in the same round plus
`psi_over_control` (5 points), both at once. The report gives both arms' PSI max per round. A
broken hard rule fails a candidate at once. A control run with a failed job, an OOM kill or an
abort makes its round not count.

Memory pressure needs both arms' PSI. A round where the candidate's PSI is missing does not
count, and neither does one where the candidate is above `psi_max` and the control's PSI is
missing; neither is a break. Without a broken rule, fewer than N rounds, or a metric missing in
any of the N, makes the verdict `incomplete`.

**The permutation check.** Beside each bound, `compare` reports a sign-permutation test of the
same hypothesis, exact over all 2^N sign flips up to 12 rounds. With fewer than 5 rounds it
cannot reach 0.05. The verdict does not depend on it.

**Screens and A/A runs.** A `screen` gate only drops arms. An arm goes when its geometric-mean
wake or makespan ratio is above the gate's limit, or it breaks a hard rule. At most
`max_survivors` stay. With fewer than `planned` rounds that count, an arm is `incomplete`. An
`aa` gate runs one condition as two arms (`arm=a`, `arm=b`) over `planned` rounds and reports
the paired SD of every metric. It passes when the two-sided interval of `aa.metric`'s mean log
ratio contains 0 and that metric's paired SD is at most `max_paired_sd`.

**The plan rule.** A gate's `[plan]` says, before the A/A run, how its SDs set the gate's rounds
and margins. A candidate passes only if it passes every metric on every workload, so the rule
looks at joint power: the chance a candidate equal to the control (a true ratio of 1) passes all
of them. The metrics and workloads are taken as independent, so joint power is the product of
each metric's power to pass its margin, raised to the number of workloads. Power comes from the
noncentral t distribution, integrated numerically in the standard library.

The rule only considers round counts that are a whole number of Williams cycles of the gate's
arms, so the order stays balanced: 8, 10 or 12 for two arms (the control and one candidate), and
only 12 for three, which is why `max_rounds` is 12. The gate runs the fewest of those, from
`min_rounds` (8) up, whose joint power is at least `joint_power` (0.8). If none reaches it, the
throughput margins (makespan and job mean) become 1.10 and the search runs again. The latency
margins never widen. If the largest count still falls short, the gate runs it, and the plan
prints the joint power it has as a known limit.

The A/A runs only the `full` workload. Its SDs stand for `memheavy` too, and the plan says so.

With latency SDs of 0.29 on both workloads, the plan gives:

| throughput SD | two arms: rounds, throughput margins, joint power | three arms |
|---|---|---|
| 0.03 | 8, 1.05, 0.86 | 12, 1.05, 0.99 |
| 0.06 | 8, 1.10, 0.86 | 12, 1.10, 0.99 |
| 0.08 | 10, 1.10, 0.84 | 12, 1.10, 0.94 |

At an SD of 0.06 and a margin of 1.05, each throughput metric has a power of 0.84 at 12
rounds. Over four metrics and two workloads that leaves a joint power near 0.5, so the margins
widen. A fail is evidence against a candidate only when the plan showed power for it.

**The frozen plan.** The plan binds the gate. Once the pilot has named the candidates, and
before any round of the gate runs, `compare --plan --write` writes it to a file next to the
gate:

```sh
uv run python bench/run.py compare bench-results/aa --gate bench/gates/aa.toml \
  --plan bench/gates/defaults.toml --arms 3 --write bench/gates/defaults.plan.toml
```

`--arms` is the control and each candidate. The file holds the arm count and its Williams
cycle, the rounds, every metric's margin after any widening, the A/A's SDs and joint power,
and the SHA-256 of the A/A's results and the A/A's commit. It is written only from an A/A that
passed. The gate names the file in `[plan] frozen`. `compare` gives no verdict on the gate
without it, or with a plan for another arm count. It takes the gate's planned rounds and
margins from the file, in place of those written in the gate. The report names the file and
gives the joint power at the rounds the verdict used.

Before any verdict or blind look, `compare` checks the file. Its cycle, alpha, workload count
and target must be the gate's. The plan recomputed from its recorded SDs under the gate's
`[plan]` rule must give the same rounds, margins and joint power. A hand-edited plan fails
this. The file also records where the A/A results were, relative to the checkout. When they
are there, or `--aa DIR` names them, their SHA-256 and SDs must match the plan's. When they
are not found, the report says the A/A provenance was not checked.

**The blind look.** A confirmatory gate's runs are scheduled for `extend_to` rounds from the
start (`--rounds 12`) and stop after the rounds the gate needs. After `look_after` rounds,
`--blind-sd` prints the pooled paired SD of each metric, pooled over candidates and workloads,
and the rounds the plan rule asks for at those SDs. Those are the frozen plan's rounds, or more
by whole Williams cycles up to `extend_to`, with no margin widened. Nothing in it says which arm
is ahead, so the look cannot stop a run early for success.

The look is fixed the first time it is ready, when every candidate has `look_after` rounds that
count. `compare` records it then, as `blind-<gate>.json` in the first results directory, and
every later `--blind-sd` and verdict uses the recorded look. The verdict uses that many rounds.

**The report.** Without `--blind-sd`, `compare` prints a markdown report. It writes the report,
with the same figures as JSON, to `DIR/compare-<gate>.md` and `.json` (`--out` for another
place). With `--plan GATE` on an A/A gate's results, the report ends with GATE's plan at the
A/A's SDs.

The gates so far:

- [`aa.toml`](../bench/gates/aa.toml): fixed weight 4 against itself, 4 rounds, on the `full`
  workload. It passes if the interval of the makespan ratio contains 1 and the paired SD is at
  most 0.06. Its SDs set the defaults gate's frozen plan, for both of that gate's workloads.
- [`pilot.toml`](../bench/gates/pilot.toml): a screen of fixed 4 against `w=3-3,h=1`, `w=2-4`,
  `w=2-3,kept=3` and `w=4-8` over 3 rounds (seeds 101 to 103), on the `full` workload. It drops
  an arm whose wake p99 ratio is above 2.0 or makespan ratio above 1.15, or that breaks a hard
  rule, and keeps at most two. Five arms have a Williams cycle of 10 rounds, so in 3 rounds the
  arms run in unbalanced positions. `compare` says so, and the screen's thresholds are coarse
  for that reason.
- [`defaults.toml`](../bench/gates/defaults.toml): the confirmatory gate for what leasehold
  recommends for test runs, on the `full` and `memheavy` workloads. The bench measures a
  `hold --weight` range together with its pool settings (kept CPUs, headroom). It does not test
  a generated heavy rule: `[commands] heavy_weight` takes one number, not a range. Its rounds
  and margins come from its frozen plan, `bench/gates/defaults.plan.toml`. Before widening, the
  margins are 1.05 for makespan and job mean (1.10 if the plan widens them) and 1.5 for wake p99
  and `git status` p95. The pilot kept `w=2-4` and `w=3-3,h=1`, which are its candidates in
  that order. The A/A run froze its plan at 12 rounds, two Williams cycles of its three arms.
- [`defaults-h1.toml`](../bench/gates/defaults-h1.toml): the same gate with `w=3-3,h=1` as its
  only candidate, on new seeds (2000 on). In the defaults gate `w=2-4` broke the hard rules
  with OOM kills, so by that gate's order `w=3-3,h=1` was never tested. The same A/A run froze
  this gate's plan, `bench/gates/defaults-h1.plan.toml`, at 8 rounds, four Williams cycles of
  its two arms. If the candidate fails, leasehold keeps fixed weight 4.

  It passed: every metric on both workloads, no hard rule broken, over all 8 rounds
  ([verdict](../bench/results/8cpu-16g/defaults-h1/compare-defaults-h1.md)). Against fixed 4,
  the geometric-mean ratios were 0.857 for makespan, 0.795 for job mean, 1.125 for wake p99
  and 1.14 to 1.16 for `git status` p95. On this machine's 6 slots, `w=3-3` is a fixed weight
  of 3 and `h=1` is the headroom `auto` already gives, so the setting it supports is
  `[commands] heavy_weight = 3`, which a new config offers as comments on 6 or 7 slots (2 on 4
or 5); [`memtight`](../bench/results/8cpu-16g/memtight/README.md) is why it is not set. Memory
  pressure stayed at 0 in every round of both workloads, so `memheavy` did not test behaviour
  under memory pressure. The guest booted afresh before the last round reported 12 KiB more
  memory than before; `bench/run.py` and `compare` now accept a difference of up to 1%.

The workloads are pinned in the gate files and `compare` checks them against each run's
`spec.json`:

| workload | args |
|---|---|
| `full` | `--agents 8 --duration 240 --mem-gib 2.5 --cap-gib 7 --git-repo /var/tmp/bigrepo` |
| `memheavy` | `--agents 8 --duration 240 --mem-gib 3 --cap-gib 7 --git-repo /var/tmp/bigrepo` |

`git status` p95 is a co-primary metric, so both time it. In `memheavy` no arm declares its
memory: it tests what a user who sets only a weight range gets.

## Reproducing

On a Linux machine with a systemd user manager (cgroup v2, cpu and memory delegated, which
`leasehold doctor` checks):

```sh
uv sync
uv run python bench/run.py --dry-run                       # the seeded plan, nothing run
uv run python bench/run.py --quick                         # about 2 minutes a condition
mkdir -p /var/tmp/bigrepo && cd /var/tmp/bigrepo && git init -q && python3 -c "
import os
for d in range(200):
    os.makedirs(f'd{d}', exist_ok=True)
    for f in range(250): open(f'd{d}/f{f}.txt', 'w').write(f'{d} {f}\n')
" && git add -A && git commit -qm init && cd -
uv run python bench/run.py --agents 4 --duration 240 --git-repo /var/tmp/bigrepo --publish
uv run python bench/run.py --agents 8 --duration 240 --git-repo /var/tmp/bigrepo --publish \
  --conditions unmanaged,nice,slice,static-jobs,lh-advisory,lh-enforced,lh-enforced-greedy,lh-hardcpu-greedy
# the 0.3 defaults, two rounds each (the runs above had --cap-gib 7, for the memory in use)
uv run python bench/run.py --agents 8 --duration 240 --git-repo /var/tmp/bigrepo \
  --mem-gib 2.5 --cap-gib 7 --rounds 2 --conditions unmanaged,lh-enforced-v020,lh-enforced
uv run python bench/run.py --agents 8 --duration 240 --git-repo /var/tmp/bigrepo \
  --mem-gib 3 --cap-gib 7 --rounds 2 --conditions unmanaged,lh-enforced,lh-enforced-declared
# two at once (opt-in): --weights cpu=3,mem=3 (and --memory-budget 7g for a matched budget)
```

The 0.3 runs above ran both rounds on one plan. Rounds now each have a seed of their own, so
these commands run new plans in their second round.

The pre-registered runs, one round per invocation. Each workload of a gate runs into its
own directory. First the A/A run of `bench/gates/aa.toml`, with the plan it gives the defaults
gate:

```sh
FULL="--agents 8 --duration 240 --mem-gib 2.5 --cap-gib 7 --git-repo /var/tmp/bigrepo"
HEAVY="--agents 8 --duration 240 --mem-gib 3 --cap-gib 7 --git-repo /var/tmp/bigrepo"
for r in 1 2 3 4; do
  uv run python bench/run.py $FULL --workload full --machine-class kvm-8cpu-16g \
    --conditions lh-enforced@w=4,arm=a,lh-enforced@w=4,arm=b --rounds 4 --seed-base 200 \
    --dir bench-results/aa --round $r
done
uv run python bench/run.py compare bench-results/aa --gate bench/gates/aa.toml \
  --plan bench/gates/defaults.toml --arms 3
```

The pilot, `bench/gates/pilot.toml`:

```sh
for r in 1 2 3; do
  uv run python bench/run.py $FULL --workload full --machine-class kvm-8cpu-16g \
    --conditions 'lh-enforced@w=4,lh-enforced@w=3-3,h=1,lh-enforced@w=2-4,lh-enforced@w=2-3,kept=3,lh-enforced@w=4-8' \
    --rounds 3 --seed-base 100 --dir bench-results/pilot --round $r
done
uv run python bench/run.py compare bench-results/pilot --gate bench/gates/pilot.toml
```

The defaults gate, `bench/gates/defaults.toml`, once its candidates are in it. First freeze
its plan from the A/A results, with `--arms` the control and each candidate:

```sh
uv run python bench/run.py compare bench-results/aa --gate bench/gates/aa.toml \
  --plan bench/gates/defaults.toml --arms 3 --write bench/gates/defaults.plan.toml
```

Both workloads are scheduled for 12 rounds, and run round by round. After round 4 of both
comes the blind look, which fixes how many rounds the verdict uses: the frozen plan's, or more
by whole cycles:

```sh
ARMS='lh-enforced@w=4,CANDIDATE-1,CANDIDATE-2'
for r in 1 2 3 4; do
  uv run python bench/run.py $FULL --workload full --machine-class kvm-8cpu-16g \
    --conditions "$ARMS" --rounds 12 --seed-base 1000 --dir bench-results/defaults-full --round $r
  uv run python bench/run.py $HEAVY --workload memheavy --machine-class kvm-8cpu-16g \
    --conditions "$ARMS" --rounds 12 --seed-base 1000 --dir bench-results/defaults-memheavy --round $r
done
uv run python bench/run.py compare bench-results/defaults-full bench-results/defaults-memheavy \
  --gate bench/gates/defaults.toml --blind-sd
# then the rounds up to the count the look gave, the same way, and the verdict:
uv run python bench/run.py compare bench-results/defaults-full bench-results/defaults-memheavy \
  --gate bench/gates/defaults.toml
```

A smoke run of the whole path takes seconds and needs no systemd scope:

```sh
uv run python bench/run.py --tiny --no-scope --agents 1 --rounds 2 \
  --conditions unmanaged,lh-advisory@w=2-4,h=1 --dir /tmp/lhbench-smoke --round 1
```

`--out DIR` (default `./bench-results`) receives a directory per invocation, named after the
machine class, the agents, the preset and the time; `--dir DIR` names it instead. The machine
class in that name is `--machine-class` when given, so the name changes with it. It holds
`spec.json` (machine, sizes, limits, the schedule, the config `init` wrote, named `bench`),
`log.txt`, `summary.md` and `summary.csv`, `invalid.jsonl` if a washout timed out, and per run
`manifest.json`, `jobs.csv` (one row per job: agent, kind, when asked, started, ended, exit
code, workers, cgroup), `samples.csv` (the machine once a second), the probes' raw samples
(`wakeup_same.csv.gz`, `wakeup_outside.csv.gz`, `git_same.csv.gz`) and, for leasehold's
conditions, `config.toml` and the daemon's log. `--publish` copies `summary.md` and
`summary.csv`, and nothing else, to `bench/results/<machine class>/`. The logs and `jobs.csv`
name runtime and cgroup paths, so they stay in the results directory. `bench/run.py summarize
DIR` rebuilds a summary from the raw files.
The run takes about an hour for K=4 and an hour and a half for K=8 on the machine below.
