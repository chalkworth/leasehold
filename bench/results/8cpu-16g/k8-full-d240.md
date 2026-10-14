# leasehold benchmark: 8cpu-16g, 8 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seed 1, 8 agents, planned over 240 s, mean think 30 s, 52 jobs (22 mem).
- Jobs: cpu 10 s alone on every CPU; mem 3.65 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 8.9 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4; slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 52 | 0 | 566.0 | 37.8 | 71.1 | 0.2 | 37.2 | 0.12 | 25.36 | 19.19 | 981.0 | 0 | 2822.0 | 45.3 | 3.3 | 9.8 | 72.0 | 0 |
| nice | 52 | 5 | 581.0 | 41.5 | 70.8 | 0.2 | 41.1 | 0.12 | 26.91 | 19.19 | 1042.0 | 22 | 5020.0 | 68.5 | 9.3 | 35.9 | 74.0 | 0 |
| slice | 52 | 7 | 630.0 | 41.9 | 72.1 | 0.5 | 41.0 | 0.12 | 19.26 | 19.26 | 969.0 | 32 | 6076.0 | 73.0 | 9.0 | 54.1 | 70.0 | 0 |
| static-jobs | 52 | 9 | 579.0 | 42.3 | 65.8 | 0.2 | 41.9 | 0.13 | 27.1 | 19.38 | 1128.0 | 21 | 10015.0 | 47.8 | 8.4 | 23.4 | 76.0 | 0 |
| lh-advisory | 52 | 0 | 848.0 | 64.4 | 115.5 | 50.1 | 14.1 | 0.13 | 3.31 | 3.41 | 103.0 | 0 | 219.0 | 0.5 | 0.0 | 0.4 | 46.0 | 0 |
| lh-enforced | 52 | 0 | 840.0 | 62.7 | 107.1 | 48.7 | 13.9 | 0.13 | 3.33 | 3.44 | 113.0 | 0 | 97.0 | 0.5 | 0.0 | 0.4 | 46.0 | 52 |
| lh-enforced-greedy | 52 | 0 | 660.0 | 40.1 | 74.2 | 29.9 | 10.1 | 0.13 | 18.82 | 18.58 | 209.0 | 0 | 107.0 | 0.7 | 0.0 | 0.5 | 63.0 | 52 |
| lh-hardcpu-greedy | 52 | 0 | 789.0 | 56.8 | 103.0 | 43.8 | 12.9 | 0.12 | 5.09 | 4.69 | 125.0 | 0 | 61.0 | 0.2 | 0.0 | 0.0 | 46.0 | 52 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
