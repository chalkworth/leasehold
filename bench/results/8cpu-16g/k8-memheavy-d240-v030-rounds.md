# leasehold benchmark: 8cpu-16g, 8 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seed 1, 8 agents, planned over 240 s, mean think 30 s, 53 jobs (22 mem).
- Jobs: cpu 10 s alone on every CPU; mem 3.00 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 7.0 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4 (`lh-enforced-v020`: 0.2.0's config, weight 4); slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged-r1 | 53 | 7 | 584.0 | 39.5 | 70.4 | 0.4 | 38.6 | 0.14 | 27.98 | 22.68 | 1746.0 | 29 | 9301.0 | 50.3 | 10.4 | 22.7 | 76.0 | 0 |
| lh-enforced-r1 | 53 | 0 | 701.0 | 47.4 | 88.8 | 36.3 | 10.9 | 0.13 | 9.0 | 9.01 | 232.0 | 0 | 286.0 | 5.2 | 0.2 | 1.8 | 51.0 | 53 |
| lh-enforced-declared-r1 | 53 | 0 | 716.0 | 49.0 | 91.6 | 37.7 | 11.2 | 0.13 | 8.91 | 8.99 | 190.0 | 0 | 150.0 | 5.5 | 0.1 | 3.4 | 53.0 | 53 |
| lh-enforced-declared-r2 | 53 | 0 | 693.0 | 46.1 | 82.3 | 35.2 | 10.8 | 0.12 | 9.17 | 9.19 | 219.0 | 0 | 93.0 | 1.5 | 0.1 | 0.7 | 54.0 | 53 |
| lh-enforced-r2 | 53 | 0 | 707.0 | 48.2 | 88.8 | 37.0 | 11.0 | 0.12 | 9.15 | 9.24 | 232.0 | 0 | 368.0 | 6.0 | 0.2 | 4.9 | 53.0 | 53 |
| unmanaged-r2 | 53 | 7 | 527.0 | 31.1 | 61.3 | 0.2 | 30.5 | 0.12 | 27.25 | 19.32 | 1374.0 | 22 | 6580.0 | 50.1 | 9.1 | 14.6 | 72.0 | 0 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
