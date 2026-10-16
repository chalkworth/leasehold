# Policy study: 30 replications a configuration, 32 screening points

Background 1 busy thread; memory 2 GiB a CPU, 6 GiB of it used by other work; no swap term. Means over the replications. A difference from the control (weight 4, headroom auto, 2 kept) is the mean over seeds of the configuration less the control on the same seed, ± its 95% interval; where the interval holds 0, no difference was detected.

## 6 slots (8 CPUs), heavy workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 808 | control | 94.1 | control | 29.4 | control | 4.72 of 8 | control | 0.0 | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 690 | -118 ± 11 | 62.4 | -31.7 ± 2.7 | 8.7 | -20.6 ± 1.1 | 6.85 of 8 | +2.13 ± 0.00 | 0.0 | 60 | 11.3 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 687 | -120 ± 10 | 61.3 | -32.9 ± 2.4 | 8.6 | -20.8 ± 1.1 | 6.85 of 8 | +2.13 ± 0.00 | 0.0 | 64 | 11.3 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 692 | -115 ± 10 | 62.2 | -32.0 ± 2.9 | 9.0 | -20.4 ± 1.1 | 6.85 of 8 | +2.13 ± 0.00 | 0.0 | 61 | 11.3 | no | 100% of runs |  |
| 1-4 | auto | 2 | 800 | -8 ± 13 (none detected) | 60.3 | -33.8 ± 4.3 | 20.9 | -8.4 ± 1.7 | 7.00 of 8 | +2.28 ± 0.00 | 18.0 | 22 | 19.4 | 100% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 783 | -25 ± 14 | 60.8 | -33.3 ± 4.4 | 20.4 | -9.0 ± 1.7 | 7.00 of 8 | +2.28 ± 0.00 | 20.0 | 34 | 18.1 | 93% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 806 | -2 ± 11 (none detected) | 62.0 | -32.1 ± 4.1 | 21.7 | -7.7 ± 1.5 | 7.00 of 8 | +2.28 ± 0.00 | 18.7 | 20 | 18.5 | 97% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 648 | -160 ± 8 | 60.1 | -34.0 ± 1.0 | 0.0 | -29.4 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 86 | 7.6 | no | 100% of runs | yes |
| 3-4 | 1 | 2 | 650 | -157 ± 8 | 60.4 | -33.8 ± 0.9 | 1.0 | -28.4 ± 0.8 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 83 | 7.6 | no | 100% of runs |  |
| 2-2 | auto | 3 | 803 | -5 ± 4 | 77.8 | -16.4 ± 0.6 | 16.2 | -13.1 ± 0.3 | 4.90 of 8 | +0.18 ± 0.00 | 0.0 | 0 | 7.6 | no | 100% of runs |  |
| 2-3 | auto | 3 | 794 | -14 ± 3 | 77.1 | -17.0 ± 0.5 | 16.0 | -13.4 ± 0.3 | 4.96 of 8 | +0.24 ± 0.08 | 0.0 | 3 | 7.6 | no | 100% of runs |  |
| 4-8 | auto | 2 | 802 | -6 ± 1 | 94.1 | -0.1 ± 0.1 (none detected) | 28.8 | -0.5 ± 0.2 | 5.42 of 8 | +0.70 ± 0.09 | 0.0 | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 570 | -237 ± 10 | 51.4 | -42.8 ± 1.0 | 0.3 | -29.0 ± 0.7 | 8.44 of 8 | +3.72 ± 0.00 | 80.2 | 81 | 7.6 | no | 100% of runs |
| 4-4 | 0 | 0 | 575 | -232 ± 10 | 51.4 | -42.7 ± 1.2 | 0.0 | -29.4 ± 0.7 | 8.44 of 8 | +3.72 ± 0.00 | 82.3 | 83 | 7.6 | no | 100% of runs |
| 3-8 | 1 | 0 | 634 | -174 ± 7 | 60.0 | -34.1 ± 1.2 | 18.8 | -10.6 ± 0.5 | 7.48 of 8 | +2.76 ± 0.08 | 12.8 | 0 | 7.6 | no | 100% of runs |
| 3-8 | 3 | 0 | 643 | -164 ± 9 | 60.0 | -34.2 ± 0.8 | 19.2 | -10.2 ± 0.4 | 6.77 of 8 | +2.05 ± 0.11 | 2.8 | 3 | 7.6 | no | 100% of runs |
| 3-4 | 0 | 1 | 645 | -162 ± 8 | 60.5 | -33.6 ± 0.8 | 10.9 | -18.5 ± 0.4 | 6.79 of 8 | +2.07 ± 0.09 | 3.3 | 3 | 7.6 | no | 100% of runs |
| 3-5 | 1 | 2 | 647 | -161 ± 9 | 60.4 | -33.7 ± 0.8 | 0.4 | -29.0 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 83 | 7.6 | no | 100% of runs |
| 3-3 | 1 | 2 | 648 | -160 ± 8 | 60.1 | -34.0 ± 1.0 | 0.0 | -29.4 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 86 | 7.6 | no | 100% of runs |

## 6 slots (8 CPUs), heavy-declared workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 808 | control | 94.1 | control | 29.4 | control | 4.72 of 8 | control | 0.0 | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 770 | -38 ± 5 | 76.5 | -17.7 ± 0.9 | 24.7 | -4.7 ± 0.6 | 6.33 of 8 | +1.61 ± 0.19 | 0.0 | 7 | 7.6 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 763 | -45 ± 6 | 76.1 | -18.1 ± 1.0 | 23.9 | -5.5 ± 0.7 | 6.74 of 8 | +2.02 ± 0.03 | 0.0 | 15 | 7.6 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 778 | -30 ± 5 | 76.3 | -17.8 ± 0.9 | 25.1 | -4.3 ± 0.5 | 6.04 of 8 | +1.32 ± 0.35 | 0.0 | 7 | 7.6 | no | 100% of runs |  |
| 1-4 | auto | 2 | 951 | +143 ± 16 | 99.0 | +4.8 ± 5.5 (none detected) | 39.9 | +10.6 ± 1.6 | 5.55 of 8 | +0.83 ± 0.21 | 0.5 | 2 | 7.6 | no | 100% of runs |  |
| 1-4 | 0 | 2 | 909 | +101 ± 17 | 98.2 | +4.0 ± 5.7 (none detected) | 38.8 | +9.4 ± 1.3 | 6.56 of 8 | +1.83 ± 0.13 | 0.2 | 9 | 7.6 | no | 100% of runs |  |
| 1-4 | auto+1 | 2 | 971 | +164 ± 17 | 98.5 | +4.4 ± 5.0 (none detected) | 40.6 | +11.3 ± 1.4 | 5.10 of 8 | +0.38 ± 0.19 | 0.3 | 1 | 7.6 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 648 | -160 ± 8 | 60.1 | -34.0 ± 1.0 | 0.0 | -29.4 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 86 | 7.6 | no | 100% of runs | yes |
| 3-4 | 1 | 2 | 650 | -157 ± 8 | 60.4 | -33.8 ± 0.9 | 1.0 | -28.4 ± 0.8 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 83 | 7.6 | no | 100% of runs |  |
| 2-2 | auto | 3 | 803 | -5 ± 4 | 77.8 | -16.4 ± 0.6 | 16.2 | -13.1 ± 0.3 | 4.90 of 8 | +0.18 ± 0.00 | 0.0 | 0 | 7.6 | no | 100% of runs |  |
| 2-3 | auto | 3 | 794 | -14 ± 3 | 77.1 | -17.0 ± 0.5 | 16.0 | -13.4 ± 0.3 | 4.96 of 8 | +0.24 ± 0.08 | 0.0 | 3 | 7.6 | no | 100% of runs |  |
| 4-8 | auto | 2 | 802 | -6 ± 1 | 94.1 | -0.1 ± 0.1 (none detected) | 28.8 | -0.5 ± 0.2 | 5.42 of 8 | +0.70 ± 0.09 | 0.0 | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 570 | -237 ± 10 | 51.4 | -42.8 ± 1.0 | 0.3 | -29.0 ± 0.7 | 8.44 of 8 | +3.72 ± 0.00 | 80.2 | 81 | 7.6 | no | 100% of runs |
| 4-4 | 0 | 0 | 575 | -232 ± 10 | 51.4 | -42.7 ± 1.2 | 0.0 | -29.4 ± 0.7 | 8.44 of 8 | +3.72 ± 0.00 | 82.3 | 83 | 7.6 | no | 100% of runs |
| 3-8 | 1 | 0 | 634 | -174 ± 7 | 60.0 | -34.1 ± 1.2 | 18.8 | -10.6 ± 0.5 | 7.48 of 8 | +2.76 ± 0.08 | 12.8 | 0 | 7.6 | no | 100% of runs |
| 3-8 | 3 | 0 | 643 | -164 ± 9 | 60.0 | -34.2 ± 0.8 | 19.2 | -10.2 ± 0.4 | 6.77 of 8 | +2.05 ± 0.11 | 2.8 | 3 | 7.6 | no | 100% of runs |
| 3-4 | 0 | 1 | 645 | -162 ± 8 | 60.5 | -33.6 ± 0.8 | 10.9 | -18.5 ± 0.4 | 6.79 of 8 | +2.07 ± 0.09 | 3.3 | 3 | 7.6 | no | 100% of runs |
| 3-5 | 1 | 2 | 647 | -161 ± 9 | 60.4 | -33.7 ± 0.8 | 0.4 | -29.0 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 83 | 7.6 | no | 100% of runs |
| 3-3 | 1 | 2 | 648 | -160 ± 8 | 60.1 | -34.0 ± 1.0 | 0.0 | -29.4 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 86 | 7.6 | no | 100% of runs |
| 2-6 | 2 | 0 | 663 | -145 ± 10 | 58.0 | -36.1 ± 2.4 | 21.7 | -7.7 ± 1.0 | 7.54 of 8 | +2.82 ± 0.30 | 4.9 | 5 | 7.6 | no | 100% of runs |
| 2-3 | 2 | 0 | 666 | -142 ± 9 | 57.2 | -37.0 ± 2.7 | 21.4 | -8.0 ± 1.1 | 7.67 of 8 | +2.95 ± 0.32 | 5.5 | 4 | 7.6 | no | 100% of runs |
| 2-3 | 0 | 1 | 667 | -141 ± 8 | 57.4 | -36.7 ± 2.2 | 15.3 | -14.1 ± 0.9 | 6.84 of 8 | +2.12 ± 0.00 | 1.3 | 1 | 7.6 | no | 100% of runs |

## 6 slots (8 CPUs), light workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 802 | control | 91.2 | control | 29.1 | control | 4.72 of 8 | control | 0.0 | 0 | 2.6 | no |  |  |
| 2-4 | auto | 2 | 641 | -162 ± 11 | 48.2 | -43.0 ± 1.6 | 0.5 | -28.6 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 77 | 7.3 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 638 | -164 ± 11 | 48.3 | -42.9 ± 1.5 | 0.0 | -29.1 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 82 | 7.2 | no | 100% of runs | yes |
| 2-4 | auto+1 | 2 | 639 | -163 ± 11 | 47.6 | -43.6 ± 1.5 | 0.0 | -29.1 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 80 | 7.3 | no | 100% of runs | yes |
| 1-4 | auto | 2 | 685 | -117 ± 11 | 21.1 | -70.1 ± 2.7 | 1.8 | -27.3 ± 1.0 | 7.00 of 8 | +2.28 ± 0.00 | 34.3 | 41 | 10.0 | 7% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 660 | -142 ± 12 | 23.5 | -67.7 ± 2.7 | 1.6 | -27.5 ± 1.0 | 7.00 of 8 | +2.27 ± 0.01 | 35.6 | 63 | 9.8 | 13% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 697 | -105 ± 12 | 21.3 | -69.9 ± 2.6 | 2.0 | -27.1 ± 0.9 | 7.00 of 8 | +2.28 ± 0.00 | 34.5 | 37 | 10.5 | 7% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 645 | -158 ± 10 | 58.5 | -32.7 ± 1.4 | 0.0 | -29.1 ± 0.7 | 6.71 of 8 | +1.99 ± 0.00 | 0.0 | 85 | 5.2 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 646 | -157 ± 9 | 57.9 | -33.3 ± 1.2 | 1.0 | -28.1 ± 0.7 | 6.70 of 8 | +1.98 ± 0.01 | 0.0 | 82 | 5.2 | no | 100% of runs |  |
| 2-2 | auto | 3 | 798 | -4 ± 5 (none detected) | 75.9 | -15.3 ± 1.2 | 16.1 | -13.0 ± 0.3 | 4.90 of 8 | +0.18 ± 0.00 | 0.0 | 0 | 5.2 | no | 100% of runs |  |
| 2-3 | auto | 3 | 789 | -13 ± 4 | 75.9 | -15.2 ± 1.2 | 15.8 | -13.3 ± 0.3 | 4.99 of 8 | +0.27 ± 0.10 | 0.0 | 3 | 5.2 | no | 100% of runs |  |
| 4-8 | auto | 2 | 795 | -8 ± 2 | 91.2 | -0.0 ± 0.0 (none detected) | 28.6 | -0.5 ± 0.2 | 5.38 of 8 | +0.65 ± 0.08 | 0.0 | 0 | 2.6 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 567 | -235 ± 12 | 50.0 | -41.2 ± 1.4 | 0.4 | -28.7 ± 0.7 | 8.43 of 8 | +3.71 ± 0.01 | 78.7 | 80 | 5.2 | no | 100% of runs |
| 4-4 | 0 | 0 | 569 | -233 ± 12 | 49.5 | -41.7 ± 1.7 | 0.0 | -29.1 ± 0.7 | 8.43 of 8 | +3.71 ± 0.01 | 81.9 | 83 | 5.2 | no | 100% of runs |
| 2-6 | 2 | 0 | 576 | -227 ± 13 | 29.5 | -61.6 ± 2.5 | 0.5 | -28.6 ± 0.7 | 8.79 of 8 | +4.06 ± 0.00 | 66.2 | 67 | 8.5 | no | 100% of runs |
| 2-3 | 2 | 0 | 580 | -222 ± 13 | 29.1 | -62.1 ± 1.8 | 0.5 | -28.6 ± 0.7 | 8.79 of 8 | +4.07 ± 0.00 | 65.8 | 62 | 8.0 | no | 100% of runs |
| 3-8 | 1 | 0 | 628 | -174 ± 9 | 58.3 | -32.9 ± 1.3 | 18.4 | -10.7 ± 0.4 | 7.51 of 8 | +2.78 ± 0.01 | 13.6 | 0 | 5.1 | no | 97% of runs |
| 2-3 | 0 | 1 | 636 | -166 ± 11 | 48.9 | -42.3 ± 2.0 | 9.5 | -19.5 ± 0.5 | 6.87 of 8 | +2.15 ± 0.06 | 2.0 | 2 | 7.0 | no | 100% of runs |
| 2-3 | 0 | 2 | 637 | -165 ± 11 | 49.1 | -42.0 ± 1.6 | 0.4 | -28.7 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 80 | 7.1 | no | 100% of runs |
| 2-4 | 0 | 2 | 638 | -164 ± 11 | 48.3 | -42.9 ± 1.5 | 0.0 | -29.1 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 82 | 7.2 | no | 100% of runs |
| 2-4 | auto+1 | 2 | 639 | -163 ± 11 | 47.6 | -43.6 ± 1.5 | 0.0 | -29.1 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 80 | 7.3 | no | 100% of runs |
| 2-2 | 2 | 2 | 654 | -149 ± 10 | 47.2 | -44.0 ± 1.9 | 0.0 | -29.1 ± 0.7 | 6.84 of 8 | +2.12 ± 0.00 | 0.0 | 77 | 7.3 | no | 100% of runs |
| 1-4 | 0 | 3 | 718 | -85 ± 9 | 37.0 | -54.2 ± 2.9 | 1.3 | -27.8 ± 0.9 | 6.00 of 8 | +1.28 ± 0.00 | 0.0 | 71 | 9.3 | no | 100% of runs |

## 8 slots (10 CPUs), heavy workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 607 | control | 56.9 | control | 0.0 | control | 8.44 of 10 | control | 0.0 | 85 | 9.5 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 678 | +71 ± 7 | 53.4 | -3.5 ± 2.7 | 13.9 | +13.9 ± 1.1 | 8.80 of 10 | +0.35 ± 0.00 | 0.0 | 41 | 17.3 | 67% of runs | 100% of runs |  |
| 2-4 | 0 | 2 | 671 | +64 ± 7 | 57.2 | +0.4 ± 2.6 (none detected) | 14.8 | +14.8 ± 1.1 | 8.79 of 10 | +0.35 ± 0.00 | 0.0 | 35 | 16.5 | 50% of runs | 100% of runs |  |
| 2-4 | auto+1 | 2 | 678 | +71 ± 8 | 53.3 | -3.5 ± 2.7 | 13.8 | +13.8 ± 1.1 | 8.79 of 10 | +0.35 ± 0.00 | 0.0 | 40 | 17.3 | 67% of runs | 100% of runs |  |
| 1-4 | auto | 2 | 819 | +212 ± 13 | 55.2 | -1.7 ± 3.9 (none detected) | 25.4 | +25.4 ± 2.1 | 8.11 of 10 | -0.33 ± 0.27 | 2.2 | 4 | 22.2 | 97% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 793 | +186 ± 14 | 54.0 | -2.8 ± 2.8 | 25.0 | +25.0 ± 2.0 | 8.50 of 10 | +0.06 ± 0.15 (none detected) | 1.9 | 8 | 21.3 | 97% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 842 | +235 ± 12 | 57.3 | +0.4 ± 3.1 (none detected) | 26.7 | +26.7 ± 1.8 | 7.70 of 10 | -0.74 ± 0.22 | 1.2 | 2 | 21.1 | 97% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 701 | +94 ± 4 | 67.3 | +10.5 ± 0.6 | 19.9 | +19.9 ± 0.7 | 6.71 of 10 | -1.73 ± 0.00 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 688 | +81 ± 5 | 67.4 | +10.5 ± 0.4 | 19.2 | +19.2 ± 0.7 | 7.49 of 10 | -0.95 ± 0.08 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 2-2 | auto | 3 | 771 | +163 ± 11 | 74.3 | +17.5 ± 3.4 | 18.0 | +18.0 ± 1.2 | 6.85 of 10 | -1.59 ± 0.00 | 0.0 | 0 | 14.1 | no | 100% of runs |  |
| 2-3 | auto | 3 | 751 | +144 ± 10 | 73.8 | +16.9 ± 3.9 | 17.8 | +17.8 ± 1.4 | 6.85 of 10 | -1.59 ± 0.00 | 0.0 | 1 | 14.1 | no | 100% of runs |  |
| 4-8 | auto | 2 | 608 | +1 ± 3 (none detected) | 57.4 | +0.5 ± 0.7 (none detected) | 0.8 | +0.8 ± 0.2 | 8.44 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 82 | 9.5 | no | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-5 | 0 | 0 | 598 | -9 ± 3 | 56.5 | -0.3 ± 0.9 (none detected) | 14.5 | +14.5 ± 0.7 | 9.24 of 10 | +0.80 ± 0.02 | 16.8 | 0 | 9.5 | no | 100% of runs |
| 4-9 | 0 | 0 | 599 | -8 ± 3 | 56.6 | -0.2 ± 1.0 (none detected) | 14.7 | +14.7 ± 0.6 | 9.11 of 10 | +0.67 ± 0.10 | 12.7 | 0 | 9.5 | no | 100% of runs |
| 3-10 | 1 | 0 | 605 | -2 ± 7 (none detected) | 54.7 | -2.2 ± 2.9 (none detected) | 15.6 | +15.6 ± 1.4 | 9.57 of 10 | +1.12 ± 0.00 | 54.5 | 0 | 14.2 | no | 100% of runs |
| 4-9 | 1 | 1 | 605 | -2 ± 3 (none detected) | 57.3 | +0.5 ± 0.7 (none detected) | 8.8 | +8.8 ± 0.3 | 8.44 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 0 | 9.5 | no | 100% of runs |
| 4 | auto | 2 | 607 | control | 56.9 | control | 0.0 | control | 8.44 of 10 | control | 0.0 | 85 | 9.5 | no | 100% of runs |
| 3-5 | 0 | 1 | 609 | +2 ± 8 (none detected) | 53.9 | -3.0 ± 2.8 | 9.6 | +9.6 ± 1.2 | 9.56 of 10 | +1.12 ± 0.01 | 52.2 | 53 | 14.2 | no | 100% of runs |

## 8 slots (10 CPUs), heavy-declared workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 607 | control | 56.9 | control | 0.0 | control | 8.44 of 10 | control | 0.0 | 85 | 9.5 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 715 | +108 ± 8 | 64.9 | +8.1 ± 2.6 | 22.9 | +22.9 ± 1.1 | 7.26 of 10 | -1.19 ± 0.29 | 0.0 | 3 | 9.5 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 711 | +104 ± 8 | 65.4 | +8.5 ± 2.2 | 22.6 | +22.6 ± 1.1 | 7.62 of 10 | -0.82 ± 0.14 | 0.0 | 2 | 9.5 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 723 | +116 ± 7 | 64.6 | +7.7 ± 2.5 | 23.2 | +23.2 ± 1.2 | 7.14 of 10 | -1.31 ± 0.24 | 0.0 | 2 | 9.5 | no | 100% of runs |  |
| 1-4 | auto | 2 | 1034 | +426 ± 28 | 120.8 | +63.9 ± 7.4 | 46.8 | +46.8 ± 2.3 | 6.02 of 10 | -2.42 ± 0.24 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 1-4 | 0 | 2 | 994 | +387 ± 29 | 121.9 | +65.0 ± 7.9 | 46.2 | +46.2 ± 2.0 | 7.03 of 10 | -1.41 ± 0.20 | 0.0 | 1 | 9.5 | no | 100% of runs |  |
| 1-4 | auto+1 | 2 | 1070 | +463 ± 32 | 121.4 | +64.5 ± 7.6 | 46.8 | +46.8 ± 2.2 | 5.57 of 10 | -2.87 ± 0.25 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 701 | +94 ± 4 | 67.3 | +10.5 ± 0.6 | 19.9 | +19.9 ± 0.7 | 6.71 of 10 | -1.73 ± 0.00 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 688 | +81 ± 5 | 67.4 | +10.5 ± 0.4 | 19.2 | +19.2 ± 0.7 | 7.49 of 10 | -0.95 ± 0.08 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 2-2 | auto | 3 | 743 | +136 ± 7 | 65.3 | +8.4 ± 2.3 | 16.1 | +16.1 ± 1.0 | 6.84 of 10 | -1.60 ± 0.00 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 2-3 | auto | 3 | 725 | +118 ± 6 | 65.2 | +8.3 ± 2.4 | 15.9 | +15.9 ± 1.0 | 6.90 of 10 | -1.54 ± 0.09 | 0.0 | 1 | 9.5 | no | 100% of runs |  |
| 4-8 | auto | 2 | 608 | +1 ± 3 (none detected) | 57.4 | +0.5 ± 0.7 (none detected) | 0.8 | +0.8 ± 0.2 | 8.44 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 82 | 9.5 | no | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-9 | 3 | 0 | 585 | -22 ± 6 | 49.0 | -7.9 ± 2.6 | 12.0 | +12.0 ± 1.1 | 9.73 of 10 | +1.28 ± 0.12 | 59.8 | 6 | 9.5 | no | 100% of runs |
| 3-10 | 1 | 0 | 585 | -22 ± 6 | 48.6 | -8.3 ± 2.2 | 12.1 | +12.1 ± 1.1 | 9.54 of 10 | +1.09 ± 0.00 | 57.5 | 0 | 9.5 | no | 100% of runs |
| 4-5 | 0 | 0 | 598 | -9 ± 3 | 56.5 | -0.3 ± 0.9 (none detected) | 14.5 | +14.5 ± 0.7 | 9.24 of 10 | +0.80 ± 0.02 | 16.8 | 0 | 9.5 | no | 100% of runs |
| 4-9 | 0 | 0 | 599 | -8 ± 3 | 56.6 | -0.2 ± 1.0 (none detected) | 14.7 | +14.7 ± 0.6 | 9.11 of 10 | +0.67 ± 0.10 | 12.7 | 0 | 9.5 | no | 100% of runs |
| 4-9 | 1 | 1 | 605 | -2 ± 3 (none detected) | 57.3 | +0.5 ± 0.7 (none detected) | 8.8 | +8.8 ± 0.3 | 8.44 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 0 | 9.5 | no | 100% of runs |
| 4 | auto | 2 | 607 | control | 56.9 | control | 0.0 | control | 8.44 of 10 | control | 0.0 | 85 | 9.5 | no | 100% of runs |

## 8 slots (10 CPUs), light workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 588 | control | 52.7 | control | 0.0 | control | 8.44 of 10 | control | 0.0 | 85 | 6.5 | no | 100% of runs |  |
| 2-4 | auto | 2 | 588 | -0 ± 7 (none detected) | 29.6 | -23.1 ± 1.9 | 0.0 | +0.0 ± 0.0 (none detected) | 8.79 of 10 | +0.35 ± 0.01 | 0.0 | 69 | 10.7 | no | 100% of runs | yes |
| 2-4 | 0 | 2 | 591 | +3 ± 7 (none detected) | 31.3 | -21.4 ± 1.8 | 1.5 | +1.5 ± 0.3 | 8.79 of 10 | +0.35 ± 0.01 | 0.0 | 56 | 10.4 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 590 | +2 ± 7 (none detected) | 29.9 | -22.8 ± 2.0 | 0.2 | +0.2 ± 0.1 | 8.79 of 10 | +0.35 ± 0.01 | 0.0 | 66 | 10.6 | no | 100% of runs |  |
| 1-4 | auto | 2 | 675 | +87 ± 9 | 10.1 | -42.6 ± 2.6 | 1.7 | +1.7 ± 0.7 | 8.83 of 10 | +0.39 ± 0.09 | 5.2 | 11 | 13.4 | 13% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 645 | +57 ± 11 | 12.9 | -39.8 ± 2.0 | 1.4 | +1.4 ± 0.6 | 8.91 of 10 | +0.48 ± 0.02 | 7.5 | 25 | 13.3 | 27% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 704 | +116 ± 9 | 7.5 | -45.2 ± 2.8 | 1.8 | +1.8 ± 0.6 | 8.48 of 10 | +0.04 ± 0.18 (none detected) | 4.3 | 6 | 13.2 | 10% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 678 | +90 ± 6 | 63.5 | +10.8 ± 1.0 | 19.7 | +19.7 ± 0.7 | 6.71 of 10 | -1.72 ± 0.01 | 0.0 | 0 | 6.5 | no |  |  |
| 3-4 | 1 | 2 | 666 | +78 ± 5 | 63.1 | +10.4 ± 0.7 | 18.7 | +18.7 ± 0.7 | 7.49 of 10 | -0.94 ± 0.06 | 0.0 | 0 | 6.5 | no |  |  |
| 2-2 | auto | 3 | 688 | +100 ± 5 | 52.9 | +0.2 ± 1.3 (none detected) | 9.9 | +9.9 ± 0.4 | 6.84 of 10 | -1.59 ± 0.01 | 0.0 | 0 | 9.2 | no | 83% of runs |  |
| 2-3 | auto | 3 | 673 | +85 ± 5 | 54.0 | +1.3 ± 1.3 | 9.8 | +9.8 ± 0.4 | 6.93 of 10 | -1.50 ± 0.10 | 0.0 | 2 | 9.3 | no | 87% of runs |  |
| 4-8 | auto | 2 | 587 | -1 ± 2 (none detected) | 52.9 | +0.2 ± 0.9 (none detected) | 0.7 | +0.7 ± 0.1 | 8.43 of 10 | -0.01 ± 0.01 | 0.0 | 82 | 6.5 | no | 100% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-10 | 1 | 0 | 544 | -44 ± 6 | 38.6 | -14.1 ± 1.8 | 6.6 | +6.6 ± 0.3 | 9.55 of 10 | +1.11 ± 0.01 | 74.8 | 0 | 9.0 | no | 77% of runs |
| 3-9 | 3 | 0 | 547 | -41 ± 5 | 37.6 | -15.1 ± 1.7 | 5.8 | +5.8 ± 0.4 | 9.87 of 10 | +1.44 ± 0.13 | 76.7 | 7 | 9.0 | no | 80% of runs |
| 2-7 | 2 | 0 | 549 | -39 ± 9 | 20.3 | -32.4 ± 1.9 | 1.2 | +1.2 ± 0.6 | 10.73 of 10 | +2.29 ± 0.01 | 47.8 | 48 | 11.5 | no | 100% of runs |
| 3-5 | 0 | 1 | 550 | -38 ± 6 | 37.9 | -14.8 ± 1.7 | 0.5 | +0.5 ± 0.1 | 9.54 of 10 | +1.11 ± 0.01 | 70.5 | 71 | 9.3 | no | 100% of runs |
| 2-4 | 2 | 0 | 555 | -33 ± 7 | 19.0 | -33.7 ± 2.3 | 1.3 | +1.3 ± 0.6 | 10.73 of 10 | +2.29 ± 0.01 | 47.0 | 48 | 11.9 | no | 100% of runs |
| 4-9 | 0 | 0 | 578 | -10 ± 2 | 53.2 | +0.5 ± 0.9 (none detected) | 14.3 | +14.3 ± 0.5 | 9.11 of 10 | +0.68 ± 0.07 | 15.8 | 0 | 6.5 | no |  |
| 4-5 | 0 | 0 | 580 | -8 ± 3 | 52.5 | -0.2 ± 0.7 (none detected) | 14.1 | +14.1 ± 0.6 | 9.18 of 10 | +0.74 ± 0.02 | 18.8 | 0 | 6.5 | no |  |
| 2-8 | 1 | 2 | 583 | -5 ± 6 (none detected) | 32.3 | -20.4 ± 1.3 | 1.6 | +1.6 ± 0.3 | 8.79 of 10 | +0.35 ± 0.01 | 0.0 | 58 | 10.9 | no | 100% of runs |
| 4-8 | auto | 2 | 587 | -1 ± 2 (none detected) | 52.9 | +0.2 ± 0.9 (none detected) | 0.7 | +0.7 ± 0.1 | 8.43 of 10 | -0.01 ± 0.01 | 0.0 | 82 | 6.5 | no | 100% of runs |
| 4-8 | 2 | 2 | 587 | -1 ± 2 (none detected) | 52.9 | +0.2 ± 0.9 (none detected) | 0.7 | +0.7 ± 0.1 | 8.43 of 10 | -0.01 ± 0.01 | 0.0 | 82 | 6.5 | no | 100% of runs |
| 2-4 | auto | 2 | 588 | -0 ± 7 (none detected) | 29.6 | -23.1 ± 1.9 | 0.0 | +0.0 ± 0.0 (none detected) | 8.79 of 10 | +0.35 ± 0.01 | 0.0 | 69 | 10.7 | no | 100% of runs |
| 2-3 | 0 | 1 | 590 | +2 ± 6 (none detected) | 29.0 | -23.7 ± 1.6 | 5.0 | +5.0 ± 0.4 | 9.25 of 10 | +0.82 ± 0.16 | 6.0 | 6 | 10.9 | no | 90% of runs |

## 16 slots (18 CPUs), heavy workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 504 | control | 32.6 | control | 6.0 | control | 15.85 of 18 | control | 0.0 | 50 | 33.6 | 97% of runs | 100% of runs |  |
| 2-4 | auto | 2 | 588 | +84 ± 9 | 29.2 | -3.4 ± 2.9 | 13.6 | +7.6 ± 1.4 | 15.20 of 18 | -0.66 ± 0.30 | 0.0 | 5 | 44.3 | 100% of runs | 100% of runs |  |
| 2-4 | 0 | 2 | 575 | +71 ± 9 | 29.9 | -2.7 ± 2.4 | 13.4 | +7.4 ± 1.3 | 15.71 of 18 | -0.15 ± 0.30 (none detected) | 0.0 | 9 | 42.6 | 100% of runs | 100% of runs |  |
| 2-4 | auto+1 | 2 | 599 | +95 ± 11 | 30.9 | -1.7 ± 2.0 (none detected) | 15.0 | +8.9 ± 1.4 | 14.61 of 18 | -1.24 ± 0.24 | 0.0 | 1 | 43.4 | 100% of runs | 100% of runs |  |
| 1-4 | auto | 2 | 775 | +271 ± 23 | 50.0 | +17.4 ± 7.4 | 20.8 | +14.7 ± 2.9 | 12.70 of 18 | -3.16 ± 0.18 | 0.0 | 0 | 42.0 | 100% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 745 | +241 ± 25 | 49.5 | +16.9 ± 7.5 | 20.2 | +14.1 ± 2.7 | 14.29 of 18 | -1.56 ± 0.24 | 0.0 | 1 | 44.0 | 100% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 784 | +280 ± 25 | 50.3 | +17.7 ± 7.5 | 21.1 | +15.1 ± 2.8 | 12.00 of 18 | -3.86 ± 0.19 | 0.0 | 0 | 42.3 | 100% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 557 | +53 ± 7 | 33.8 | +1.2 ± 2.8 (none detected) | 12.0 | +6.0 ± 0.9 | 15.24 of 18 | -0.62 ± 0.01 | 0.0 | 0 | 36.1 | 100% of runs | 100% of runs |  |
| 3-4 | 1 | 2 | 535 | +31 ± 7 | 34.2 | +1.6 ± 2.5 (none detected) | 11.8 | +5.7 ± 1.1 | 15.59 of 18 | -0.26 ± 0.16 | 0.0 | 5 | 36.4 | 100% of runs | 100% of runs |  |
| 2-2 | auto | 3 | 654 | +150 ± 7 | 33.8 | +1.2 ± 2.0 (none detected) | 14.3 | +8.3 ± 1.0 | 13.97 of 18 | -1.89 ± 0.35 | 0.0 | 0 | 43.7 | 100% of runs | 100% of runs |  |
| 2-3 | auto | 3 | 610 | +106 ± 10 | 32.0 | -0.6 ± 2.3 (none detected) | 14.1 | +8.1 ± 1.3 | 14.34 of 18 | -1.51 ± 0.21 | 0.0 | 1 | 44.5 | 100% of runs | 100% of runs |  |
| 4-8 | auto | 2 | 496 | -8 ± 7 | 32.9 | +0.3 ± 2.0 (none detected) | 5.7 | -0.3 ± 0.9 (none detected) | 15.84 of 18 | -0.01 ± 0.01 (none detected) | 0.0 | 54 | 33.8 | 100% of runs | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-11 | 0 | 3 | 526 | +22 ± 9 | 42.8 | +10.2 ± 2.9 | 11.8 | +5.7 ± 1.1 | 12.95 of 18 | -2.90 ± 0.02 | 0.0 | 3 | 25.4 | no | 100% of runs |

## 16 slots (18 CPUs), heavy-declared workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 539 | control | 41.7 | control | 15.7 | control | 12.39 of 18 | control | 0.0 | 2 | 25.4 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 733 | +194 ± 12 | 67.8 | +26.1 ± 3.2 | 38.1 | +22.4 ± 1.6 | 10.35 of 18 | -2.05 ± 0.48 | 0.0 | 0 | 25.4 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 708 | +169 ± 17 | 67.3 | +25.6 ± 3.6 | 37.4 | +21.6 ± 1.6 | 11.11 of 18 | -1.28 ± 0.50 | 0.0 | 0 | 25.4 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 732 | +193 ± 14 | 67.6 | +26.0 ± 3.5 | 38.2 | +22.4 ± 1.5 | 10.12 of 18 | -2.27 ± 0.49 | 0.0 | 0 | 25.4 | no | 100% of runs |  |
| 1-4 | auto | 2 | 1220 | +681 ± 41 | 146.9 | +105.2 ± 7.7 | 56.9 | +41.2 ± 2.3 | 7.39 of 18 | -5.00 ± 0.32 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 1-4 | 0 | 2 | 1123 | +584 ± 50 | 148.2 | +106.6 ± 7.0 | 55.8 | +40.0 ± 2.7 | 9.13 of 18 | -3.27 ± 0.54 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 1-4 | auto+1 | 2 | 1249 | +710 ± 40 | 150.3 | +108.6 ± 8.0 | 57.2 | +41.5 ± 2.0 | 7.17 of 18 | -5.23 ± 0.46 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 605 | +66 ± 8 | 50.0 | +8.3 ± 2.2 | 26.9 | +11.1 ± 0.9 | 11.77 of 18 | -0.62 ± 0.56 | 0.0 | 0 | 25.4 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 590 | +51 ± 8 | 49.6 | +7.9 ± 1.7 | 26.2 | +10.4 ± 0.9 | 12.37 of 18 | -0.03 ± 0.33 (none detected) | 0.0 | 0 | 25.4 | no | 100% of runs |  |
| 2-2 | auto | 3 | 779 | +240 ± 14 | 69.1 | +27.4 ± 2.9 | 35.1 | +19.4 ± 1.6 | 9.87 of 18 | -2.52 ± 0.57 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 2-3 | auto | 3 | 736 | +197 ± 16 | 69.5 | +27.9 ± 3.4 | 35.4 | +19.6 ± 1.6 | 10.23 of 18 | -2.16 ± 0.47 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 4-8 | auto | 2 | 530 | -9 ± 5 | 42.1 | +0.4 ± 1.7 (none detected) | 15.6 | -0.2 ± 0.3 (none detected) | 13.05 of 18 | +0.65 ± 0.48 | 0.0 | 5 | 25.4 | no | 100% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-6 | 0 | 0 | 513 | -26 ± 6 | 40.3 | -1.4 ± 1.7 (none detected) | 17.1 | +1.4 ± 0.9 | 15.41 of 18 | +3.02 ± 0.33 | 0.5 | 0 | 25.4 | no | 100% of runs |
| 4-16 | 0 | 0 | 519 | -20 ± 7 | 43.7 | +2.1 ± 1.5 | 17.5 | +1.7 ± 0.9 | 15.18 of 18 | +2.78 ± 0.44 | 0.0 | 0 | 25.4 | no | 100% of runs |
| 4-10 | 2 | 1 | 528 | -11 ± 8 | 43.2 | +1.5 ± 2.0 (none detected) | 18.2 | +2.5 ± 0.6 | 13.14 of 18 | +0.74 ± 0.44 | 0.0 | 0 | 25.4 | no | 100% of runs |
| 4-17 | 1 | 1 | 529 | -10 ± 7 | 43.9 | +2.3 ± 1.9 | 18.2 | +2.5 ± 0.6 | 13.39 of 18 | +1.00 ± 0.42 | 0.0 | 0 | 25.4 | no | 100% of runs |
| 4-8 | auto | 2 | 530 | -9 ± 5 | 42.1 | +0.4 ± 1.7 (none detected) | 15.6 | -0.2 ± 0.3 (none detected) | 13.05 of 18 | +0.65 ± 0.48 | 0.0 | 5 | 25.4 | no | 100% of runs |
| 4-5 | 3 | 1 | 531 | -8 ± 4 | 41.8 | +0.1 ± 1.4 (none detected) | 18.1 | +2.4 ± 0.3 | 13.30 of 18 | +0.91 ± 0.29 | 0.0 | 0 | 25.4 | no | 100% of runs |
| 4-11 | 0 | 3 | 536 | -3 ± 6 (none detected) | 44.6 | +2.9 ± 1.7 | 12.5 | -3.2 ± 0.5 | 12.52 of 18 | +0.12 ± 0.32 (none detected) | 0.0 | 4 | 25.4 | no | 100% of runs |
| 4 | auto | 2 | 539 | control | 41.7 | control | 15.7 | control | 12.39 of 18 | control | 0.0 | 2 | 25.4 | no | 100% of runs |

## 16 slots (18 CPUs), light workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 473 | control | 19.7 | control | 0.0 | control | 15.79 of 18 | control | 0.0 | 55 | 19.6 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 529 | +56 ± 6 | 2.9 | -16.8 ± 1.8 | 0.5 | +0.5 ± 0.4 | 15.94 of 18 | +0.15 ± 0.21 (none detected) | 0.0 | 9 | 24.5 | 37% of runs | 57% of runs |  |
| 2-4 | 0 | 2 | 520 | +47 ± 9 | 6.0 | -13.7 ± 1.8 | 0.2 | +0.2 ± 0.3 (none detected) | 16.16 of 18 | +0.37 ± 0.04 | 0.0 | 16 | 23.5 | 30% of runs | 93% of runs |  |
| 2-4 | auto+1 | 2 | 535 | +61 ± 6 | 3.5 | -16.2 ± 2.2 | 0.6 | +0.6 ± 0.4 | 15.04 of 18 | -0.75 ± 0.21 | 0.0 | 2 | 24.3 | 30% of runs | 30% of runs |  |
| 1-4 | auto | 2 | 609 | +136 ± 14 | 1.0 | -18.7 ± 1.9 | 0.5 | +0.5 ± 0.5 (none detected) | 13.55 of 18 | -2.24 ± 0.14 | 0.0 | 0 | 23.7 | 30% of runs | 30% of runs |  |
| 1-4 | 0 | 2 | 580 | +107 ± 15 | 1.9 | -17.8 ± 2.2 | 0.6 | +0.6 ± 0.6 (none detected) | 15.14 of 18 | -0.65 ± 0.14 | 0.0 | 3 | 24.1 | 33% of runs | 33% of runs |  |
| 1-4 | auto+1 | 2 | 614 | +141 ± 13 | 1.0 | -18.7 ± 1.8 | 0.5 | +0.5 ± 0.4 | 12.81 of 18 | -2.98 ± 0.16 | 0.0 | 0 | 23.9 | 30% of runs | 30% of runs |  |
| 3-3 | 1 | 2 | 511 | +38 ± 4 | 12.5 | -7.2 ± 1.3 | 1.2 | +1.2 ± 0.3 | 15.21 of 18 | -0.58 ± 0.01 | 0.0 | 0 | 20.8 | 7% of runs |  |  |
| 3-4 | 1 | 2 | 494 | +21 ± 5 | 16.5 | -3.2 ± 1.1 | 2.1 | +2.1 ± 0.3 | 15.64 of 18 | -0.15 ± 0.16 (none detected) | 0.0 | 6 | 20.4 | 7% of runs | 27% of runs |  |
| 2-2 | auto | 3 | 596 | +123 ± 7 | 3.3 | -16.4 ± 2.4 | 0.7 | +0.7 ± 0.4 | 14.28 of 18 | -1.51 ± 0.28 | 0.0 | 0 | 24.3 | 37% of runs | 37% of runs |  |
| 2-3 | auto | 3 | 547 | +74 ± 6 | 6.2 | -13.5 ± 1.8 | 0.6 | +0.6 ± 0.3 | 14.79 of 18 | -1.00 ± 0.16 | 0.0 | 4 | 23.1 | 17% of runs | 17% of runs |  |
| 4-8 | auto | 2 | 463 | -10 ± 3 | 19.9 | +0.2 ± 1.2 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 15.80 of 18 | +0.01 ± 0.02 (none detected) | 0.0 | 58 | 18.5 | no | 100% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-6 | 0 | 0 | 460 | -13 ± 4 | 19.1 | -0.6 ± 1.3 (none detected) | 3.6 | +3.6 ± 0.4 | 16.93 of 18 | +1.14 ± 0.19 | 7.6 | 8 | 18.9 | no | 23% of runs |
| 4-16 | 0 | 0 | 461 | -13 ± 5 | 21.8 | +2.1 ± 1.5 | 5.1 | +5.1 ± 0.3 | 15.78 of 18 | -0.01 ± 0.02 (none detected) | 0.3 | 0 | 18.3 | no |  |
| 4-10 | 2 | 1 | 462 | -12 ± 6 | 21.3 | +1.6 ± 1.3 | 3.9 | +3.9 ± 0.3 | 15.84 of 18 | +0.05 ± 0.06 (none detected) | 0.0 | 2 | 19.1 | no |  |
| 4-8 | auto | 2 | 463 | -10 ± 3 | 19.9 | +0.2 ± 1.2 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 15.80 of 18 | +0.01 ± 0.02 (none detected) | 0.0 | 58 | 18.5 | no | 100% of runs |
| 4 | auto | 2 | 473 | control | 19.7 | control | 0.0 | control | 15.79 of 18 | control | 0.0 | 55 | 19.6 | no | 100% of runs |
