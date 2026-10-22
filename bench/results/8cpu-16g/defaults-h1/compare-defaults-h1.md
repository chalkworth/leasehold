# Gate `defaults-h1` (confirmatory)

- Intent: Decide whether a hold --weight range, with its pool settings (kept CPUs, headroom), can replace fixed weight 4 as what leasehold recommends for test runs.
- Objective: Per candidate against fixed weight 4: one-sided 95% upper bounds on the geometric-mean ratios of makespan, mean job time, wake p99 and git status p95, on both workloads.
- Stop rule: The frozen plan's rounds, a whole number of Williams cycles (8, 10 or 12 for two arms; 12 for three). After 4 rounds, a look blind to which arm leads may add whole cycles, up to 12, and is fixed once taken. The verdict uses exactly that many valid rounds; no early stop for success or failure.
- Decision: If the candidate passes every metric on both workloads, with no hard rule broken, it becomes the recommended range and pool settings. If not, leasehold keeps fixed weight 4 as what it recommends.

Results: h1-full, h1-memheavy. Control `lh-enforced@w=4`.

Rounds the verdict uses: 8 (the blind look at the pooled SDs of the first 4 rounds, fixed when it was first taken).

Planned rounds and margins come from `defaults-h1.plan.toml`.

The A/A provenance was checked: e0-aa match the plan's SHA-256 and SDs.

Joint power at 8 rounds, at the frozen plan's A/A SDs: 1.00. This is the chance a candidate equal to the control passes every metric on every workload.

## `lh-enforced@w=3,h=1`: pass

Workload `full`: rounds 1, 2, 3, 4, 5, 6, 7, 8 count (8 of 8).

- hard rules: kept
- memory PSI max by round, control / candidate: r1 0.0 / 0.0, r2 0.0 / 0.0, r3 0.0 / 0.0, r4 0.0 / 0.0, r5 0.0 / 0.0, r6 0.0 / 0.0, r7 0.0 / 0.0, r8 0.0 / 0.0

| metric | n | ratio | upper bound | margin | t | permutation p | verdict |
|---|---|---|---|---|---|---|---|
| makespan_s | 8 | 0.857 | 0.877 | 1.05 | 1.895 | 0.004 | pass |
| job_mean_s | 8 | 0.795 | 0.804 | 1.05 | 1.895 | 0.004 | pass |
| wake_same_p99_ms | 8 | 1.125 | 1.125 | 1.5 | 1.895 | 0.004 | pass |
| git_p95_ms | 8 | 1.163 | 1.187 | 1.5 | 1.895 | 0.004 | pass |

Workload `memheavy`: rounds 1, 2, 3, 4, 5, 6, 7, 8 count (8 of 8).

- hard rules: kept
- memory PSI max by round, control / candidate: r1 0.0 / 0.0, r2 0.0 / 0.0, r3 0.0 / 0.0, r4 0.0 / 0.0, r5 0.0 / 0.0, r6 0.0 / 0.0, r7 0.0 / 0.0, r8 0.0 / 0.0

| metric | n | ratio | upper bound | margin | t | permutation p | verdict |
|---|---|---|---|---|---|---|---|
| makespan_s | 8 | 0.857 | 0.879 | 1.05 | 1.895 | 0.004 | pass |
| job_mean_s | 8 | 0.795 | 0.805 | 1.05 | 1.895 | 0.004 | pass |
| wake_same_p99_ms | 8 | 1.125 | 1.125 | 1.5 | 1.895 | 0.004 | pass |
| git_p95_ms | 8 | 1.140 | 1.190 | 1.5 | 1.895 | 0.004 | pass |

