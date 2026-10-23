# `memtight`: two heavy jobs that do not fit in memory

An exploratory run, not a pre-registered gate: no plan, margins or stop rule were fixed before
it ran, and its numbers come with no confidence bounds. It asks what the two-at-once weights that
leasehold 0.4.0's `init` wrote on 4 to 7 slots do when two memory-heavy jobs cannot fit in memory
together. The [`defaults-h1` gate](../defaults-h1/compare-defaults-h1.md) measured the same
weights on a workload where they fit.

## Setup

- Machine: a KVM guest with 8 CPUs and 16 GiB of memory (class `kvm-8cpu-16g`), kernel 6.12.
- Workload: `memheavy`, 8 agents over a planned 240 s, 6 rounds on seeds 3001 to 3006. Each
  memory job touches 3.75 GiB, then hashes for 6 s on every CPU.
- Everything the benchmark runs shares one 7 GiB `MemoryMax` (2 GiB of swap), so two memory
  jobs (7.5 GiB) do not fit under it.
- leasehold at commit f41cc06. `spec.json` records 6af92f3, the head of that pull request's
  branch, which differs from it only in the summary's column names.
- The config is what `leasehold init` wrote for the machine: 6 slots, `[memory] budget =
  "auto"`, which under the 7 GiB limit is 5.25 GiB, so 0.875 GiB a slot. Every job runs under
  `hold --weight W`, contained.

```sh
bench/run.py --agents 8 --duration 240 --cap-gib 7 --mem-gib 3.75 --workload memheavy \
  --git-repo ... --machine-class kvm-8cpu-16g --drop-caches --rounds 6 --seed-base 3000
```

## Arms

- `lh-enforced_w4`: weight 4, the built-in heavy weight. One heavy job runs at a time.
- `lh-enforced_w3`: weight 3 with no memory declared, as 0.4.0's `init` set it. Each job counts
  3 x 0.875 = 2.625 GiB, so two of them count exactly the 5.25 GiB budget and start together.
- `lh-enforced_w3_memdeclared`: weight 3, with each memory job declaring 4.25 GiB, so only one
  of them is admitted at a time.

## Results

| arm | failed jobs | OOM kills | rounds with kills | memory PSI `some` max in the cap, by round | makespan vs weight 4 |
|---|---|---|---|---|---|
| weight 4 | 0 | 0 | 0 of 6 | 0.0% | 1 |
| weight 3, memory undeclared | 25 | 25 | 6 of 6 | 8.5% to 13.2% | 0.86x (0.84 to 0.89) |
| weight 3, memory declared | 0 | 0 | 0 of 6 | 0.0% | 1.06x (0.99 to 1.12) |

Makespan ratios are each round's ratio to the weight 4 run on the same seed. The figure is
their geometric mean, with the lowest and highest round in brackets. The undeclared arm
finished sooner partly because its killed jobs ended early. Machine-wide memory PSI peaked at
12.8% in the same rounds. A guard that turns on at 20% stays off on either reading.
[`summary.md`](summary.md) has every round, and each `<arm>-r<round>/` directory has the
round's jobs, samples and config. `log.txt` and `logs/` are the run's own logs.

## What changed because of it

Two-at-once weights are safe when the heavy commands declare memory, or when two of them fit
in memory together. Undeclared, they are counted at the budget over the slots, which says
nothing about what they use. So the next release writes those weights as a commented opt-in
again, and `leasehold doctor` warns when a config sets them while the heavy rules declare no
memory. The memory guard now reads pressure inside the cgroup the leases run in and turns on
at 10%, and `hold` says when the OOM killer ended its command and what `--memory` would have
counted enough.
