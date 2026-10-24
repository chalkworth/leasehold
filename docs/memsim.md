# Simulator: memory policies

[`bench/sim/memory.py`](../bench/sim/memory.py) models what happens to memory when agents share
one machine under a cgroup memory limit: how jobs grow into the limit, spill into swap, slow
down, and get killed. [`bench/sim/memstudy.py`](../bench/sim/memstudy.py) checks the model
against the benchmark's memory runs and then compares memory policies over a range of
workloads. Like the [CPU simulator](sim.md), it ranks candidates for the benchmark. It does not
replace the benchmark.

```sh
uv sync --group bench
uv run python -m bench.sim.memstudy validate                 # the model beside the measurements
uv run python -m bench.sim.memstudy fit                      # refit the swap terms (2 to 5 min)
uv run python -m bench.sim.memstudy sweep --out OUT          # every policy, 56 workloads (~30 min)
uv run python -m bench.sim.memstudy report OUT               # regret, Pareto sets
uv run python -m bench.sim.memstudy sensitivity --policy NAME --rivals A,B --out OUT2
```

## Goals and how they are scored

The owner's goals, in order: use the machine fully, be confident nothing is OOM-killed, and
protect the most important jobs. Swap is slack for mistakes. Nothing should be scheduled into
it.

Policies are ranked in this order:

1. OOM kills. A policy may kill at most 2e-4 of its heavy (`mem`) jobs, pooled over every
   workload and seed. The sweep runs a killed job again, so a kill also costs the time it
   wasted.
2. Throughput, as makespan. Regret at a workload is a policy's makespan over that of the
   fastest policy that killed nothing there.
3. Swap written, in GiB per run. On an SSD or NVMe disk each GiB a run counts as 2% of
   throughput, on a spinning disk as 0.2%.

A safety net that acts (a park or a brake) costs something but is not a failure. Nets are
counted per run, and among policies that kill nothing the one that needs the net less is
better, as long as it gives up little throughput. Pressure seconds (time the cap overflowed,
plus time parked) stand in for how slow the machine feels to the person at it.

## The model

Agents run `bench/run.py`'s plans (8 agents, exponential think time, `cpu` and `mem` jobs).
Admission is leasehold's own `admission.grants`: one `work` pool on 6 slots with a memory
budget. A policy only decides what each lease counts.

The machine is stepped every 0.1 s:

* **CPU.** A job keeps `weight` threads busy. 8 CPUs give 4 cores' worth plus 0.44 of a core
  for each further busy CPU. That alone reproduces the measured run times: a `cpu` job takes
  15.0 s at weight 4 and 24.6 s at weight 3 beside another job, and a `mem` job 9.1 s and 15.0 s.
* **Memory.** A job's footprint grows with its progress over the first `ramp` of its work. With
  `late_share`, part of it grows again late, from 60% of the work, as a link step does. The
  cap holds the footprints plus 0.25 GiB.
* **Overflow.** What the cap cannot hold is in swap. Past the cap, footprints grow no faster
  than 0.5 GiB/s. Every `mem` job runs `1 + 0.9 × X` times slower for an overflow of X GiB, and
  0.1 GiB/s of swap is written while it lasts, on top of the overflow itself.
* **Swap slots and the OOM kill.** In the measured runs the kills did not come from memory
  running out. The cap's swap filled up: pages read back from swap keep their slot. The model
  grows the slots at 0.3 GiB/s while the cap overflows, from X up to X + 2.3 GiB, and frees
  them when a job ends. When the slots reach the cap's swap limit (or, under parking, a job's
  own 1 GiB), the kernel kills the largest job. So a two-job overlap kills when
  `X + 2.3 GiB ≥ swap limit` and lasts long enough. That one rule accounts for `memtight`
  (0.8 GiB over, 2 GiB swap: kills), `net-memheavy` (0.8 over, 5 swap: none), the 1 GiB
  per-job swap limit (kills), and `overcommit` (2.8 over, 5 swap: kills).
* **Parking,** as [`bench/park.py`](../bench/park.py) does it: freeze the youngest `mem` job
  0.3 s after the cap spills, push its memory out at 1.4 GiB/s, thaw it when it fits under
  80% of the cap or runs alone, and page it back in at 0.3 GiB/s. No park follows within 2 s
  of a thaw. A job still reading its pages back cannot take more swap.
* **Hold** (`park_hold`): heavy work waits in line while a job is parked or settling, as a
  daemon would if it counted parked memory. **Plateau**: heavy work waits while a running job's
  memory still grows (growth averaged over 1 s above 16 MiB/s or 1% a second, then a 5 s
  cooldown). **Settled counting**: a settled job counts 1.1 × what it uses, not its estimate.
* **Learned estimates,** as the 0.5 plan describes them: per kind, `max(weighted p95, max of
  the last 2) × 1.15`, floored at 256 MiB, trusted after 3 clean runs. A kill records
  `max(peak × 1.2, peak + 256 MiB)`. Before a kind is trusted, it counts the whole budget
  (`cold = alone`) or the assumed figure (`cold = assumed`). No figure counts more than the
  budget, so a job too big for it runs alone instead of being refused.

## Calibration

`validate` replays every measured arm on its own plan seeds. The swap terms were fitted on two
arms (plain weight 3 in `net-memheavy` and `overcommit`); everything else is held out.

| experiment | arm | makespan / w4, measured / sim | kills (6 rounds) | swap GiB / round |
|---|---|---|---|---|
| memtight | w3 | 0.864 / 0.853 | 25 / 27 | 8.4 / 6.8 |
| memtight | w3, declared | 1.059 / 1.059 | 0 / 0 | 0 / 0 |
| net-memheavy | w3 (fit) | 0.890 / 0.892 | 0 / 0 | 10.0 / 10.8 |
| net-memheavy | w3, 1 GiB swap per job | 0.854 / 0.846 | 29 / 24 | 9.3 / 6.4 |
| net-memheavy | w3, park | 0.892 / 0.887 | 0 / 0 | 13.8 / 17.7 |
| net-full | every w3 arm | 0.85 / 0.849 | 0 / 0 | 0 / 0 |
| net-hold | w3, park, hold | 0.887 / 0.870 | 0 / 0 | 11.6 / 12.1 |
| overcommit | w3 (fit) | 0.876 / 0.874 | 25 / 20 | 20.0 / 15.1 |
| overcommit | w3, park | 0.867 / 0.865 | 6 / 4 | 6.1 / 7.0 |
| overcommit | w3, park, hold | 0.872 / 0.869 | 3 / 0 | 7.8 / 5.5 |

Makespan ratios agree within 0.02 and the plain arms' kills within about 20%. The model is
weakest where the net acts. It under-predicts kills in the park arms, most of all with the
harness's hold, where all 3 measured kills were a thawed job paging back in beside a job just
granted. It also over-predicts their swap by up to 40%.

## Results

56 workloads from a Latin hypercube over a heavy job's size (0.3 to 0.8 of a 7 GiB cap), the
share of heavy jobs (0.1 to 0.7), think time (10 to 60 s), size spread (cv 0 to 0.4), growth
ramp (3% to 60% of the job), swap (0.3 to 0.9 of the cap) and a late link step (0 to 50% of the
size). Each policy ran 10 seeds per workload, about 15,000 heavy jobs; the finalists ran 40 more,
about 60,000. The constraint is at most 2e-4 kills per heavy job, pooled over the workloads.

| policy | makespan / w4 | kills / heavy jobs | swap GiB / run | parks / run |
|---|---|---|---|---|
| exact sizes, budget = cap (ceiling, not achievable) | 0.876 | 0 / 59,958 | 0.1 | 0 |
| learned median × 1.15, budget = cap, net, ledger warm | 0.882 | 0 / 59,958 | 0.6 | 0.18 |
| weight 3, net, no estimate | 0.885 | 0 / 59,958 | 9.1 | 2.9 |
| **learned median × 1.15, budget = cap, net, every run cold** | **0.892** | **0 / 59,958** | **1.1** | **0.34** |
| learned p80 × 1.15, budget = cap, no net, warm | 0.895 | 7 / 59,965 | 0.0 | 0 |
| plain weight 3 (today's default) | 0.897 | 1,755 / 16,661 | 15.3 | 0 |
| learned p95 × 1.15 (the 0.5 plan), budget = cap, warm | 0.924 | 0 / 15,235 | 0.0 | 0 |
| learned p95 × 1.15, budget 75%, cold counts the budget | 1.024 | 0 / 15,235 | 0.0 | 0 |
| weight 4 | 1.000 | 0 / 15,235 | 0.0 | 0 |

"Net" is parking with a daemon-level hold: when the cap overflows, the youngest heavy job is
parked in swap, and admission counts a parked or settling job's memory, so nothing heavy
starts beside a job that is paging back in. "Cold" means a kind with fewer than 3 clean runs
counts weight × budget / slots, as undeclared work does today.

### The recommendation

Count each heavy job's learned figure (the weighted median of its past
peaks × 1.15, from 3 clean runs, keyed as the 0.5 plan says), against a budget equal to the
memory limit, with parking and the daemon-level hold as the net. It is about 11% faster than
weight 4. In the simulation it had no kill in 59,958 heavy jobs (95% bound 5e-5), wrote 1.1 GiB
of swap a run, and parked once every three runs. With a warm ledger it comes within 0.7% of
exact sizes.

### Why it holds up

* The estimate keeps swap low and the net makes kills rare. Without the net,
  a tighter estimate (p80) gets near the limit (1.2e-4, bound 2.2e-4). With it, even no
  estimate at all (weight 3 plus net) kills nothing, but parks three times a run and writes
  9 GiB.
* The 0.5 plan's p95 × 1.15 is safe without a net but costs 4 to 10%, because every job is
  counted at the top of its range, so two jobs that would fit are rarely run together. With a
  net behind it, the median is enough.
* A budget of 75% of the cap costs another 6%, and counting the whole budget while cold another
  5%. The margin covers what the budget kept back.

### Disks and zswap

On an SSD or NVMe disk (`/sys/block/*/queue/rotational` is 0), count each GiB
written a run as 2% of throughput. The recommendation wins there, ahead of weight 3 with the
net, which writes 9 GiB a run. On a spinning disk (0.2% a GiB) the two are close. zswap would cut device writes, most of all for parking, but a zswap entry still
holds a swap slot, so the slot-exhaustion kill is unchanged. The bench's pages are trivially
compressible (one byte repeated), so measuring zswap needs jobs that fill memory with random
data.

### Sensitivity

The recommendation with one thing changed at a time (16 workloads, 6 seeds,
about 2,600 heavy jobs each): no change caused a kill. Budget 85% cost 3%, margin 1.3 cost 3%,
p95 cost 2%, cold "alone" cost 5%. Margin 1.0 doubled swap. The settle cooldown and floor
barely matter, because a thawed job usually ends while it is still paging back in. The model's
swap and thrash terms do not move the result either, because parking acts within a second of
an overflow. That also means the result rests on the park itself working as modelled.

## What 2e-4 rests on, and what a bench run can show

The rate is a claim about the model. By the rule of three, zero kills in n heavy jobs bounds
the rate at about 3/n, so confirming 2e-4 would take some 15,000 heavy jobs without a kill. A
bench round has 15 to 30, so a bench run can refute the claim but cannot confirm it.

The rate depends on the tails:

* Estimate error. Sizes are lognormal around a stable mean. A heavier tail (a test suite
  that doubles), or a mean that drifts (a dependency upgrade), is not in the sweep, apart from
  the outlier variants (2% and 10% of jobs at 1.5×), which caused no kill with the net and one
  each without.
* Growth. A job grows over a ramp, optionally with one late step. Faster growth past the
  cap than 0.5 GiB/s, or growth from several jobs at once, gives the parker less time.
* The park. The result needs the park to act in time and the hold to keep new heavy work
  out. The model under-predicts the measured park-arm kills, so the 2e-4 figure is optimistic
  in the part of the model the recommendation leans on.

## Limits

* One machine and one kind of job, which sweeps all of its memory again and again. Real builds
  touch a smaller hot set and thrash less.
* The page cache, memory bandwidth, the guard's PSI readings and the person's own work in the
  same cap are not modelled. `base_gib` stands in for them.
* The swap-slot rule (`X + 2.3 GiB`) is fitted to three experiments with one job size each.
* Each sweep run starts with an empty ledger, so "cold" here is pessimistic for a machine
  that has been running a while and optimistic for one that sees new repositories all day.
* Wakeup latency and `git status` time are not modelled. Pressure seconds stand in for them.
