# leasehold benchmark: kvm-8cpu-16g, 8 agents

- Machine: 8 CPUs, 15.6 GiB memory, 8.0 GiB swap; virtualisation: kvm; kernel 6.12.113+deb13-amd64.
- Plan: seeds 3001 to 3006, 8 agents, planned over 240 s, mean think 30 s (seed 3001: 52 jobs, 15 mem).
- Jobs: cpu 10 s alone on every CPU; mem 3.75 GiB touched, then 6 s of hashing alone on every CPU.
- Memory limit on everything the benchmark runs: 7.0 GiB (swap 2.0 GiB).
- leasehold: `leasehold init --user` config, `hold --weight` cpu 4, mem 4 unless the condition sets `w` (`lh-enforced-v020`: 0.2.0's config, weight 4); slice condition CPUWeight 20.

| condition | jobs | failed | makespan s | job mean s | job p95 s | wait mean s | run mean s | wake same p50 ms | wake same p99 ms | wake outside p99 ms | git status p95 ms | OOM kills | swap-in MiB | mem PSI some max | mem PSI some mean | mem PSI full max | mem PSI some max in the cap | mem PSI some mean in the cap | mem PSI full max in the cap | CPU busy % | in lease scope |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| lh-enforced_w4-r1 | 52 | 0 | 766.0 | 62.4 | 93.9 | 49.1 | 13.3 | 0.08 | 0.08 | 0.08 | 47.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 46.0 | 52 |
| lh-enforced_w3-r1 | 52 | 4 | 673.0 | 50.9 | 80.2 | 28.1 | 22.7 | 0.07 | 0.09 | 0.09 | 53.0 | 4 | 9245.0 | 8.7 | 0.8 | 8.6 | 8.5 | 0.8 | 8.5 | 59.0 | 52 |
| lh-enforced_w3_memdeclared-r1 | 52 | 0 | 784.0 | 65.3 | 101.0 | 44.9 | 20.4 | 0.07 | 0.09 | 0.09 | 50.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 50.0 | 52 |
| lh-enforced_w3-r2 | 48 | 5 | 612.0 | 47.7 | 76.2 | 25.7 | 22.0 | 0.07 | 0.09 | 0.09 | 55.0 | 5 | 9061.0 | 12.8 | 1.1 | 12.7 | 13.2 | 1.1 | 13.2 | 56.0 | 48 |
| lh-enforced_w3_memdeclared-r2 | 48 | 0 | 690.0 | 54.6 | 96.6 | 35.1 | 19.5 | 0.07 | 0.09 | 0.09 | 51.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 50.0 | 48 |
| lh-enforced_w4-r2 | 48 | 0 | 698.0 | 54.3 | 105.7 | 41.6 | 12.7 | 0.07 | 0.08 | 0.08 | 48.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 45.0 | 48 |
| lh-enforced_w3_memdeclared-r3 | 56 | 0 | 800.0 | 74.0 | 120.0 | 55.6 | 18.4 | 0.07 | 0.09 | 0.09 | 50.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 48.0 | 56 |
| lh-enforced_w4-r3 | 56 | 0 | 713.0 | 63.7 | 98.3 | 51.2 | 12.5 | 0.07 | 0.08 | 0.08 | 49.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 50.0 | 56 |
| lh-enforced_w3-r3 | 56 | 3 | 637.0 | 49.4 | 82.8 | 28.0 | 21.3 | 0.07 | 0.09 | 0.09 | 52.0 | 3 | 8471.0 | 9.3 | 0.8 | 9.1 | 9.1 | 0.8 | 9.1 | 64.0 | 56 |
| lh-enforced_w3_memdeclared-r4 | 61 | 0 | 878.0 | 73.3 | 109.3 | 53.9 | 19.4 | 0.08 | 0.09 | 0.09 | 50.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 50.0 | 61 |
| lh-enforced_w3-r4 | 61 | 3 | 691.0 | 53.0 | 83.1 | 31.0 | 21.9 | 0.07 | 0.09 | 0.09 | 56.0 | 3 | 5287.0 | 9.8 | 0.5 | 9.8 | 9.0 | 0.5 | 9.0 | 66.0 | 61 |
| lh-enforced_w4-r4 | 61 | 0 | 818.0 | 67.3 | 104.5 | 54.3 | 13.0 | 0.07 | 0.08 | 0.08 | 47.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 50.0 | 61 |
| lh-enforced_w4-r5 | 48 | 0 | 630.0 | 50.2 | 92.7 | 37.2 | 12.9 | 0.07 | 0.08 | 0.08 | 48.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 51.0 | 48 |
| lh-enforced_w3_memdeclared-r5 | 48 | 0 | 652.0 | 54.5 | 99.8 | 34.7 | 19.7 | 0.07 | 0.09 | 0.09 | 51.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 53.0 | 48 |
| lh-enforced_w3-r5 | 48 | 2 | 527.0 | 38.6 | 76.5 | 16.8 | 21.7 | 0.07 | 0.09 | 0.09 | 52.0 | 2 | 3972.0 | 12.5 | 0.6 | 12.2 | 11.8 | 0.6 | 11.8 | 69.0 | 48 |
| lh-enforced_w3-r6 | 48 | 8 | 557.0 | 44.2 | 78.5 | 22.4 | 21.8 | 0.07 | 0.09 | 0.09 | 49.0 | 8 | 13987.0 | 11.4 | 1.8 | 11.4 | 11.1 | 1.8 | 11.1 | 59.0 | 48 |
| lh-enforced_w4-r6 | 48 | 0 | 650.0 | 52.7 | 99.3 | 40.5 | 12.2 | 0.08 | 0.08 | 0.08 | 48.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 46.0 | 48 |
| lh-enforced_w3_memdeclared-r6 | 48 | 0 | 724.0 | 60.5 | 102.8 | 42.6 | 17.9 | 0.08 | 0.09 | 0.09 | 51.0 | 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 44.0 | 48 |

Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the harness's scope), `outside` in the session the benchmark was started from.
