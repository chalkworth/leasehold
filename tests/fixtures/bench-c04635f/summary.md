# leasehold benchmark: kvm-test, 1 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.101+deb13-amd64.
- Plan: seeds 201 to 202, 1 agents, planned over 1 s, mean think 0.1 s (seed 201: 8 jobs, 5 mem).
- Jobs: cpu 0.05 s alone on every CPU; mem 0.02 GiB touched, then 0.05 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 8.0 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4 unless the condition sets `w` (`lh-enforced-v020`: 0.2.0's config, weight 4); slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lh-enforced_w4_arma-r1 | 8 | 0 | 5.0 | 0.5 | 0.6 | 0.3 | 0.1 | 0.1 | 1.86 | 2.81 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 23.0 | 8 |
| lh-enforced_w4_armb-r1 | 8 | 0 | 4.0 | 0.5 | 0.6 | 0.3 | 0.1 | 0.1 | 1.64 | 1.53 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 23.0 | 8 |
| lh-enforced_w4_armb-r2 | 5 | 0 | 3.0 | 0.5 | 0.5 | 0.3 | 0.1 | 0.1 | 1.94 | 1.51 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 15.0 | 5 |
| lh-enforced_w4_arma-r2 | 5 | 0 | 3.0 | 0.5 | 0.5 | 0.3 | 0.1 | 0.1 | 0.35 | 0.36 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 15.0 | 5 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
