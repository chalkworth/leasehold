# leasehold benchmark: 8cpu-16g, 8 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seed 1, 8 agents, planned over 240 s, mean think 30 s, 52 jobs (22 mem).
- Jobs: cpu 10 s alone on every CPU; mem 2.50 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 7.0 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4 (`lh-enforced-v020`: 0.2.0's config, weight 4); slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| unmanaged-r1 | 52 | 0 | 585.0 | 40.2 | 72.9 | 0.1 | 39.6 | 0.12 | 23.19 | 19.15 | 852.0 | 0 | 3829.0 | 31.6 | 3.7 | 5.4 | 70.0 | 0 |
| lh-enforced-v020-r1 | 52 | 0 | 841.0 | 63.4 | 110.3 | 49.4 | 13.9 | 0.12 | 3.56 | 3.64 | 101.0 | 0 | 532.0 | 0.2 | 0.0 | 0.2 | 47.0 | 52 |
| lh-enforced-r1 | 52 | 0 | 834.0 | 62.2 | 108.0 | 48.3 | 13.8 | 0.12 | 3.53 | 3.5 | 108.0 | 0 | 223.0 | 0.0 | 0.0 | 0.0 | 47.0 | 52 |
| lh-enforced-r2 | 52 | 0 | 892.0 | 69.6 | 117.9 | 54.6 | 14.9 | 0.12 | 9.19 | 9.2 | 236.0 | 0 | 284.0 | 5.6 | 0.1 | 5.6 | 61.0 | 52 |
| lh-enforced-v020-r2 | 52 | 0 | 842.0 | 63.3 | 111.6 | 49.2 | 14.0 | 0.12 | 6.28 | 5.97 | 141.0 | 0 | 19.0 | 0.0 | 0.0 | 0.0 | 50.0 | 52 |
| unmanaged-r2 | 52 | 0 | 729.0 | 57.9 | 85.1 | 0.2 | 57.0 | 0.14 | 26.61 | 21.59 | 1218.0 | 0 | 1681.0 | 27.3 | 3.7 | 4.3 | 79.0 | 0 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
