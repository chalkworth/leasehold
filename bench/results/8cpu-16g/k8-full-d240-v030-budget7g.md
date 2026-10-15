# leasehold benchmark: 8cpu-16g, 8 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seed 1, 8 agents, planned over 240 s, mean think 30 s, 53 jobs (22 mem).
- Jobs: cpu 10 s alone on every CPU; mem 2.50 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 7.0 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config with `[memory] budget = '7g'`, `hold --weight` cpu 3, mem 3 (`lh-enforced-v020`: 0.2.0's config, weight 4); slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lh-enforced | 53 | 0 | 638.0 | 39.3 | 72.7 | 20.7 | 18.5 | 0.12 | 9.13 | 9.13 | 164.0 | 0 | 25.0 | 0.0 | 0.0 | 0.0 | 60.0 | 53 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
