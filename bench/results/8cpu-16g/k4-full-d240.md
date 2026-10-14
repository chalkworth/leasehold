# leasehold benchmark: 8cpu-16g, 4 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seed 1, 4 agents, planned over 240 s, mean think 30 s, 25 jobs (10 mem).
- Jobs: cpu 10 s alone on every CPU; mem 3.65 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 8.7 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4; slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged | 25 | 0 | 308.0 | 14.9 | 27.5 | 0.1 | 14.8 | 0.12 | 19.19 | 18.86 | 289.0 | 0 | 3.0 | 0.0 | 0.0 | 0.0 | 53.0 | 0 |
| nice | 25 | 0 | 303.0 | 14.0 | 26.2 | 0.1 | 13.9 | 0.12 | 19.34 | 19.21 | 136.0 | 0 | 0.0 | 1.2 | 0.0 | 0.9 | 52.0 | 0 |
| slice | 25 | 0 | 303.0 | 14.0 | 25.5 | 0.1 | 13.9 | 0.12 | 19.17 | 19.21 | 127.0 | 0 | 2.0 | 0.0 | 0.0 | 0.0 | 53.0 | 0 |
| lh-advisory | 25 | 0 | 361.0 | 20.5 | 37.6 | 8.5 | 11.9 | 0.13 | 2.89 | 3.25 | 100.0 | 0 | 42.0 | 0.0 | 0.0 | 0.0 | 44.0 | 0 |
| lh-enforced | 25 | 0 | 358.0 | 19.2 | 42.9 | 7.6 | 11.5 | 0.13 | 3.39 | 3.92 | 94.0 | 0 | 24.0 | 0.0 | 0.0 | 0.0 | 43.0 | 25 |
| lh-advisory-greedy | 25 | 0 | 301.0 | 13.0 | 28.7 | 4.7 | 8.2 | 0.12 | 16.07 | 15.8 | 249.0 | 0 | 2.0 | 0.0 | 0.0 | 0.0 | 55.0 | 0 |
| lh-enforced-greedy | 25 | 0 | 304.0 | 13.3 | 25.8 | 4.7 | 8.4 | 0.12 | 17.99 | 18.0 | 177.0 | 0 | 13.0 | 0.0 | 0.0 | 0.0 | 56.0 | 25 |
| lh-hardcpu-greedy | 25 | 0 | 352.0 | 19.0 | 32.8 | 8.2 | 10.7 | 0.11 | 5.21 | 5.02 | 127.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 42.0 | 25 |
| static-jobs | 25 | 0 | 318.0 | 16.8 | 26.3 | 0.1 | 16.7 | 0.12 | 16.77 | 16.61 | 244.0 | 0 | 47.0 | 6.7 | 0.3 | 3.6 | 52.0 | 0 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
