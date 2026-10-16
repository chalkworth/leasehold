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

## Two heavy jobs at once (opt-in)

`[commands] heavy_weight = 3` and `light_weight = 2` on 6 slots let two builds or test runs
run at once, with memory counted as above. An earlier draft of 0.3 generated this setting by
default; these runs measured it then, and are why it is opt-in. One run per condition, in
sessions earlier the same day as the runs above.

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

These runs did not show two at once to be better on this machine, which is why it is not the
default.

## Simulator

[`bench/sim/`](../bench/sim/) replays these runs with leasehold's own admission code and a
fitted model of the jobs and the machine, to choose which weight defaults the next benchmark
run should compare. It reproduces 23 of the 24 leasehold conditions above within 7% on
makespan; the one it misses (by 29%) is the two-at-once run that went into swap, which it does
not model. [Simulator](sim.md) has the method, the calibration, a study of range defaults and
its limits.

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

**Conditions**, one after another, each after the machine has settled (load at most 2, CPU
pressure under 10%):

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

`--out DIR` (default `./bench-results`) receives, per run, `spec.json` (machine, plan, sizes,
limits), `log.txt`, `summary.md` and `summary.csv`, and per condition `jobs.csv` (one row per
job: agent, kind, when asked, started, ended, exit code, workers, cgroup), `samples.csv` (the
machine once a second), the probes' raw samples (`wakeup_same.csv.gz`, `wakeup_outside.csv.gz`,
`git_same.csv.gz`) and the leasehold daemon's log. `--publish` copies the summary to
`bench/results/<CPUs>cpu-<GiB>g/`. `bench/run.py summarize DIR` rebuilds a summary from the raw
files. The run takes about an hour for K=4 and an hour and a half for K=8 on the machine below.
