# Policy study: 30 replications a configuration, 0 screening points

Background 1 busy thread; memory 2 GiB a CPU, 6 GiB of it used by other work; no swap term. Means over the replications. A difference from the control (weight 4, headroom auto, 2 kept) is the mean over seeds of the configuration less the control on the same seed, ± its 95% interval; where the interval holds 0, no difference was detected.

## 6 slots (8 CPUs), heavy workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 691 | control | 75.9 | control | 28.4 | control | 4.78 of 8 | control | 0.0 | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 515 | -176 ± 14 | 33.0 | -42.9 ± 4.0 | 6.4 | -21.9 ± 1.3 | 6.86 of 8 | +2.08 ± 0.01 | 0.0 | 54 | 11.3 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 509 | -182 ± 15 | 32.2 | -43.7 ± 3.6 | 5.0 | -23.4 ± 1.4 | 6.86 of 8 | +2.08 ± 0.01 | 0.0 | 63 | 11.2 | no | 100% of runs | yes |
| 2-4 | auto+1 | 2 | 515 | -177 ± 15 | 32.8 | -43.1 ± 3.7 | 5.2 | -23.1 ± 1.4 | 6.86 of 8 | +2.08 ± 0.00 | 0.0 | 59 | 11.2 | no | 100% of runs |  |
| 1-4 | auto | 2 | 631 | -60 ± 17 | 29.0 | -46.9 ± 4.2 | 14.2 | -14.1 ± 1.8 | 6.99 of 8 | +2.21 ± 0.01 | 13.0 | 18 | 16.2 | 83% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 592 | -99 ± 17 | 27.6 | -48.3 ± 4.1 | 13.5 | -14.8 ± 2.0 | 6.97 of 8 | +2.18 ± 0.02 | 12.2 | 35 | 16.6 | 87% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 646 | -45 ± 16 | 29.6 | -46.3 ± 4.0 | 14.5 | -13.8 ± 1.7 | 6.97 of 8 | +2.19 ± 0.03 | 11.5 | 13 | 16.0 | 83% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 490 | -201 ± 12 | 36.0 | -39.9 ± 2.2 | 0.0 | -28.4 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 79 | 7.6 | no | 100% of runs | yes |
| 3-4 | 1 | 2 | 489 | -202 ± 11 | 35.7 | -40.2 ± 2.6 | 1.6 | -26.8 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 75 | 7.6 | no | 100% of runs | yes |
| 2-2 | auto | 3 | 675 | -17 ± 6 | 59.7 | -16.2 ± 1.8 | 15.5 | -12.9 ± 0.3 | 4.93 of 8 | +0.14 ± 0.00 | 0.0 | 0 | 7.6 | no | 100% of runs |  |
| 2-3 | auto | 3 | 663 | -28 ± 5 | 59.4 | -16.4 ± 1.8 | 15.2 | -13.2 ± 0.3 | 4.95 of 8 | +0.17 ± 0.05 | 0.0 | 3 | 7.6 | no | 100% of runs |  |
| 4-8 | auto | 2 | 678 | -13 ± 4 | 75.4 | -0.5 ± 0.7 (none detected) | 27.7 | -0.6 ± 0.2 | 5.42 of 8 | +0.64 ± 0.04 | 0.0 | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-4 | 1 | 2 | 489 | -202 ± 11 | 35.7 | -40.2 ± 2.6 | 1.6 | -26.8 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 75 | 7.6 | no | 100% of runs |
| 3-3 | 1 | 2 | 490 | -201 ± 12 | 36.0 | -39.9 ± 2.2 | 0.0 | -28.4 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 79 | 7.6 | no | 100% of runs |
| 2-4 | 0 | 2 | 509 | -182 ± 15 | 32.2 | -43.7 ± 3.6 | 5.0 | -23.4 ± 1.4 | 6.86 of 8 | +2.08 ± 0.01 | 0.0 | 63 | 11.2 | no | 100% of runs |

## 6 slots (8 CPUs), heavy-declared workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 691 | control | 75.9 | control | 28.4 | control | 4.78 of 8 | control | 0.0 | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 620 | -71 ± 7 | 53.9 | -22.0 ± 2.2 | 21.3 | -7.0 ± 0.8 | 6.59 of 8 | +1.80 ± 0.14 | 0.0 | 10 | 7.6 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 606 | -85 ± 9 | 53.7 | -22.2 ± 2.8 | 20.2 | -8.2 ± 1.1 | 6.76 of 8 | +1.98 ± 0.02 | 0.0 | 20 | 7.6 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 632 | -59 ± 8 | 54.6 | -21.3 ± 2.4 | 22.3 | -6.1 ± 0.8 | 6.52 of 8 | +1.73 ± 0.24 | 0.0 | 10 | 7.6 | no | 100% of runs |  |
| 1-4 | auto | 2 | 784 | +92 ± 17 | 64.7 | -11.2 ± 5.0 | 33.2 | +4.9 ± 1.7 | 5.76 of 8 | +0.98 ± 0.10 | 0.6 | 2 | 7.6 | no | 100% of runs |  |
| 1-4 | 0 | 2 | 707 | +15 ± 22 (none detected) | 64.1 | -11.8 ± 4.5 | 30.2 | +1.8 ± 2.3 (none detected) | 6.74 of 8 | +1.96 ± 0.06 | 0.3 | 15 | 7.6 | no | 100% of runs |  |
| 1-4 | auto+1 | 2 | 812 | +121 ± 18 | 64.7 | -11.2 ± 4.6 | 34.4 | +6.0 ± 1.6 | 5.18 of 8 | +0.39 ± 0.14 | 0.4 | 1 | 7.6 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 490 | -201 ± 12 | 36.0 | -39.9 ± 2.2 | 0.0 | -28.4 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 79 | 7.6 | no | 100% of runs | yes |
| 3-4 | 1 | 2 | 489 | -202 ± 11 | 35.7 | -40.2 ± 2.6 | 1.6 | -26.8 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 75 | 7.6 | no | 100% of runs | yes |
| 2-2 | auto | 3 | 675 | -17 ± 6 | 59.7 | -16.2 ± 1.8 | 15.5 | -12.9 ± 0.3 | 4.93 of 8 | +0.14 ± 0.00 | 0.0 | 0 | 7.6 | no | 100% of runs |  |
| 2-3 | auto | 3 | 663 | -28 ± 5 | 59.4 | -16.4 ± 1.8 | 15.2 | -13.2 ± 0.3 | 4.95 of 8 | +0.17 ± 0.05 | 0.0 | 3 | 7.6 | no | 100% of runs |  |
| 4-8 | auto | 2 | 678 | -13 ± 4 | 75.4 | -0.5 ± 0.7 (none detected) | 27.7 | -0.6 ± 0.2 | 5.42 of 8 | +0.64 ± 0.04 | 0.0 | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-4 | 1 | 2 | 489 | -202 ± 11 | 35.7 | -40.2 ± 2.6 | 1.6 | -26.8 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 75 | 7.6 | no | 100% of runs |
| 3-3 | 1 | 2 | 490 | -201 ± 12 | 36.0 | -39.9 ± 2.2 | 0.0 | -28.4 ± 0.7 | 6.78 of 8 | +2.00 ± 0.00 | 0.0 | 79 | 7.6 | no | 100% of runs |

## 6 slots (8 CPUs), light workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 771 | control | 87.5 | control | 29.0 | control | 4.78 of 8 | control | 0.0 | 0 | 2.6 | no |  |  |
| 2-4 | auto | 2 | 531 | -239 ± 12 | 33.7 | -53.8 ± 2.4 | 0.9 | -28.1 ± 0.8 | 6.85 of 8 | +2.06 ± 0.01 | 0.0 | 70 | 6.5 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 525 | -245 ± 12 | 32.7 | -54.8 ± 2.5 | 0.0 | -29.0 ± 0.7 | 6.85 of 8 | +2.07 ± 0.01 | 0.0 | 78 | 6.6 | no | 100% of runs | yes |
| 2-4 | auto+1 | 2 | 531 | -240 ± 14 | 33.6 | -53.9 ± 2.3 | 0.0 | -29.0 ± 0.7 | 6.85 of 8 | +2.06 ± 0.01 | 0.0 | 75 | 6.5 | no | 100% of runs |  |
| 1-4 | auto | 2 | 603 | -168 ± 18 | 12.5 | -75.0 ± 2.8 | 0.9 | -28.0 ± 0.9 | 7.00 of 8 | +2.21 ± 0.00 | 26.6 | 36 | 9.1 | 10% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 563 | -208 ± 18 | 17.1 | -70.4 ± 3.0 | 0.8 | -28.2 ± 0.8 | 6.98 of 8 | +2.19 ± 0.01 | 21.1 | 60 | 8.7 | 3% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 622 | -149 ± 16 | 11.5 | -76.0 ± 3.1 | 0.9 | -28.0 ± 0.9 | 7.00 of 8 | +2.22 ± 0.00 | 25.4 | 28 | 9.1 | 3% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 537 | -234 ± 13 | 45.4 | -42.2 ± 1.6 | 0.0 | -29.0 ± 0.7 | 6.72 of 8 | +1.93 ± 0.02 | 0.0 | 82 | 5.2 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 536 | -235 ± 11 | 44.4 | -43.1 ± 2.2 | 1.4 | -27.6 ± 0.7 | 6.72 of 8 | +1.93 ± 0.02 | 0.0 | 79 | 5.1 | no | 100% of runs |  |
| 2-2 | auto | 3 | 747 | -23 ± 7 | 70.2 | -17.3 ± 1.8 | 15.9 | -13.1 ± 0.4 | 4.90 of 8 | +0.12 ± 0.01 | 0.0 | 0 | 5.2 | no | 100% of runs |  |
| 2-3 | auto | 3 | 735 | -35 ± 7 | 71.1 | -16.4 ± 1.8 | 15.7 | -13.3 ± 0.4 | 4.97 of 8 | +0.19 ± 0.08 | 0.0 | 3 | 5.0 | no | 93% of runs |  |
| 4-8 | auto | 2 | 759 | -12 ± 3 | 87.7 | +0.2 ± 0.4 (none detected) | 28.4 | -0.5 ± 0.2 | 5.28 of 8 | +0.49 ± 0.07 | 0.0 | 0 | 2.6 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2-4 | 0 | 2 | 525 | -245 ± 12 | 32.7 | -54.8 ± 2.5 | 0.0 | -29.0 ± 0.7 | 6.85 of 8 | +2.07 ± 0.01 | 0.0 | 78 | 6.6 | no | 100% of runs |

## 8 slots (10 CPUs), heavy workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 410 | control | 24.8 | control | 0.0 | control | 8.56 of 10 | control | 0.0 | 73 | 9.5 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 467 | +57 ± 9 | 21.0 | -3.9 ± 2.3 | 7.7 | +7.7 ± 1.1 | 8.79 of 10 | +0.23 ± 0.01 | 0.0 | 33 | 15.4 | 27% of runs | 100% of runs |  |
| 2-4 | 0 | 2 | 465 | +55 ± 9 | 20.9 | -3.9 ± 2.4 | 8.4 | +8.4 ± 1.3 | 8.79 of 10 | +0.22 ± 0.01 | 0.0 | 21 | 15.4 | 33% of runs | 100% of runs |  |
| 2-4 | auto+1 | 2 | 472 | +62 ± 8 | 21.5 | -3.4 ± 2.6 | 8.3 | +8.3 ± 1.1 | 8.79 of 10 | +0.23 ± 0.01 | 0.0 | 29 | 15.4 | 27% of runs | 100% of runs |  |
| 1-4 | auto | 2 | 600 | +190 ± 17 | 23.0 | -1.8 ± 3.4 (none detected) | 13.9 | +13.9 ± 2.1 | 7.87 of 10 | -0.69 ± 0.11 | 0.6 | 3 | 19.5 | 80% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 573 | +163 ± 17 | 21.9 | -2.9 ± 3.8 (none detected) | 13.7 | +13.7 ± 2.3 | 8.56 of 10 | -0.00 ± 0.11 (none detected) | 0.9 | 9 | 19.7 | 83% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 634 | +224 ± 11 | 25.8 | +0.9 ± 3.2 (none detected) | 15.6 | +15.6 ± 1.8 | 7.43 of 10 | -1.13 ± 0.17 | 0.9 | 1 | 19.4 | 70% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 504 | +94 ± 11 | 38.5 | +13.6 ± 1.8 | 16.2 | +16.2 ± 0.8 | 6.78 of 10 | -1.78 ± 0.01 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 479 | +69 ± 10 | 36.8 | +11.9 ± 1.7 | 13.8 | +13.8 ± 1.1 | 7.62 of 10 | -0.94 ± 0.02 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 2-2 | auto | 3 | 533 | +123 ± 13 | 30.0 | +5.2 ± 1.5 | 10.5 | +10.5 ± 1.0 | 6.86 of 10 | -1.70 ± 0.01 | 0.0 | 0 | 14.0 | no | 100% of runs |  |
| 2-3 | auto | 3 | 517 | +107 ± 10 | 30.1 | +5.3 ± 1.5 | 10.0 | +10.0 ± 0.7 | 7.03 of 10 | -1.53 ± 0.12 | 0.0 | 3 | 13.8 | no | 100% of runs |  |
| 4-8 | auto | 2 | 412 | +2 ± 5 (none detected) | 25.9 | +1.0 ± 1.2 (none detected) | 1.9 | +1.9 ± 0.3 | 8.56 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 67 | 9.5 | no | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 410 | control | 24.8 | control | 0.0 | control | 8.56 of 10 | control | 0.0 | 73 | 9.5 | no | 100% of runs |

## 8 slots (10 CPUs), heavy-declared workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 410 | control | 24.8 | control | 0.0 | control | 8.56 of 10 | control | 0.0 | 73 | 9.5 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 499 | +89 ± 9 | 28.5 | +3.7 ± 2.1 | 13.8 | +13.8 ± 1.2 | 7.79 of 10 | -0.77 ± 0.33 | 0.0 | 6 | 9.5 | no | 100% of runs |  |
| 2-4 | 0 | 2 | 493 | +83 ± 9 | 29.5 | +4.7 ± 2.0 | 13.1 | +13.1 ± 1.3 | 7.89 of 10 | -0.67 ± 0.15 | 0.0 | 3 | 9.5 | no | 100% of runs |  |
| 2-4 | auto+1 | 2 | 503 | +93 ± 10 | 28.4 | +3.6 ± 1.9 | 14.0 | +14.0 ± 0.9 | 7.67 of 10 | -0.89 ± 0.31 | 0.0 | 4 | 9.5 | no | 100% of runs |  |
| 1-4 | auto | 2 | 706 | +296 ± 22 | 53.6 | +28.7 ± 5.3 | 31.3 | +31.3 ± 2.5 | 6.76 of 10 | -1.80 ± 0.17 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 1-4 | 0 | 2 | 651 | +241 ± 22 | 50.6 | +25.8 ± 6.1 | 28.5 | +28.5 ± 2.9 | 7.67 of 10 | -0.89 ± 0.15 | 0.0 | 2 | 9.5 | no | 100% of runs |  |
| 1-4 | auto+1 | 2 | 741 | +331 ± 22 | 53.3 | +28.4 ± 5.8 | 31.7 | +31.7 ± 2.7 | 6.21 of 10 | -2.35 ± 0.23 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 504 | +94 ± 11 | 38.5 | +13.6 ± 1.8 | 16.2 | +16.2 ± 0.8 | 6.78 of 10 | -1.78 ± 0.01 | 0.0 | 0 | 9.4 | no | 100% of runs |  |
| 3-4 | 1 | 2 | 479 | +69 ± 10 | 36.8 | +11.9 ± 1.7 | 13.8 | +13.8 ± 1.1 | 7.62 of 10 | -0.94 ± 0.02 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 2-2 | auto | 3 | 525 | +115 ± 12 | 28.8 | +3.9 ± 1.5 | 9.1 | +9.1 ± 0.7 | 6.85 of 10 | -1.71 ± 0.01 | 0.0 | 0 | 9.5 | no | 100% of runs |  |
| 2-3 | auto | 3 | 511 | +101 ± 12 | 30.2 | +5.4 ± 1.7 | 9.6 | +9.6 ± 0.9 | 6.97 of 10 | -1.59 ± 0.12 | 0.0 | 3 | 9.5 | no | 100% of runs |  |
| 4-8 | auto | 2 | 412 | +2 ± 5 (none detected) | 25.9 | +1.0 ± 1.2 (none detected) | 1.9 | +1.9 ± 0.3 | 8.56 of 10 | +0.00 ± 0.00 (none detected) | 0.0 | 67 | 9.5 | no | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 410 | control | 24.8 | control | 0.0 | control | 8.56 of 10 | control | 0.0 | 73 | 9.5 | no | 100% of runs |

## 8 slots (10 CPUs), light workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 450 | control | 32.8 | control | 0.0 | control | 8.43 of 10 | control | 0.0 | 78 | 6.4 | no | 100% of runs | yes |
| 2-4 | auto | 2 | 467 | +16 ± 8 | 15.6 | -17.2 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 8.78 of 10 | +0.35 ± 0.03 | 0.0 | 50 | 9.1 | no | 100% of runs | yes |
| 2-4 | 0 | 2 | 467 | +16 ± 7 | 18.4 | -14.4 ± 1.4 | 2.1 | +2.1 ± 0.3 | 8.77 of 10 | +0.34 ± 0.03 | 0.0 | 34 | 9.0 | no | 97% of runs | yes |
| 2-4 | auto+1 | 2 | 472 | +21 ± 7 | 16.1 | -16.7 ± 1.4 | 0.5 | +0.5 ± 0.1 | 8.78 of 10 | +0.34 ± 0.03 | 0.0 | 45 | 9.3 | no | 100% of runs |  |
| 1-4 | auto | 2 | 568 | +118 ± 12 | 2.9 | -29.9 ± 2.4 | 0.7 | +0.7 ± 0.4 | 8.64 of 10 | +0.21 ± 0.13 | 2.9 | 8 | 11.8 | 7% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 541 | +90 ± 12 | 6.8 | -25.9 ± 2.1 | 0.6 | +0.6 ± 0.5 | 8.80 of 10 | +0.37 ± 0.05 | 2.3 | 18 | 11.0 | 7% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 602 | +152 ± 9 | 2.2 | -30.6 ± 2.6 | 0.6 | +0.6 ± 0.5 | 8.07 of 10 | -0.36 ± 0.11 | 1.7 | 3 | 11.5 | 7% of runs | 97% of runs |  |
| 3-3 | 1 | 2 | 560 | +110 ± 9 | 48.5 | +15.7 ± 1.8 | 17.9 | +17.9 ± 0.7 | 6.72 of 10 | -1.72 ± 0.03 | 0.0 | 0 | 6.4 | no |  |  |
| 3-4 | 1 | 2 | 536 | +86 ± 9 | 46.9 | +14.1 ± 2.1 | 16.2 | +16.2 ± 0.9 | 7.50 of 10 | -0.93 ± 0.04 | 0.0 | 0 | 6.5 | no |  |  |
| 2-2 | auto | 3 | 565 | +114 ± 7 | 35.6 | +2.9 ± 1.5 | 8.0 | +8.0 ± 0.6 | 6.85 of 10 | -1.58 ± 0.03 | 0.0 | 0 | 8.0 | no | 47% of runs |  |
| 2-3 | auto | 3 | 550 | +99 ± 6 | 34.4 | +1.6 ± 1.7 (none detected) | 8.0 | +8.0 ± 0.6 | 7.15 of 10 | -1.28 ± 0.15 | 0.0 | 4 | 7.8 | no | 43% of runs |  |
| 4-8 | auto | 2 | 456 | +6 ± 3 | 32.6 | -0.2 ± 1.3 (none detected) | 1.3 | +1.3 ± 0.3 | 8.43 of 10 | -0.01 ± 0.03 (none detected) | 0.0 | 72 | 6.5 | no | 100% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 450 | control | 32.8 | control | 0.0 | control | 8.43 of 10 | control | 0.0 | 78 | 6.4 | no | 100% of runs |
| 4-8 | auto | 2 | 456 | +6 ± 3 | 32.6 | -0.2 ± 1.3 (none detected) | 1.3 | +1.3 ± 0.3 | 8.43 of 10 | -0.01 ± 0.03 (none detected) | 0.0 | 72 | 6.5 | no | 100% of runs |
| 2-4 | 0 | 2 | 467 | +16 ± 7 | 18.4 | -14.4 ± 1.4 | 2.1 | +2.1 ± 0.3 | 8.77 of 10 | +0.34 ± 0.03 | 0.0 | 34 | 9.0 | no | 97% of runs |
| 2-4 | auto | 2 | 467 | +16 ± 8 | 15.6 | -17.2 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 8.78 of 10 | +0.35 ± 0.03 | 0.0 | 50 | 9.1 | no | 100% of runs |

## 16 slots (18 CPUs), heavy workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 345 | control | 2.9 | control | 0.6 | control | 15.53 of 18 | control | 0.0 | 14 | 29.6 | 53% of runs | 97% of runs |  |
| 2-4 | auto | 2 | 381 | +36 ± 6 | 2.7 | -0.2 ± 1.5 (none detected) | 1.9 | +1.4 ± 0.9 | 13.51 of 18 | -2.02 ± 0.33 | 0.0 | 1 | 33.3 | 87% of runs | 97% of runs |  |
| 2-4 | 0 | 2 | 369 | +24 ± 5 | 2.1 | -0.8 ± 1.2 (none detected) | 1.3 | +0.8 ± 0.7 | 14.18 of 18 | -1.35 ± 0.31 | 0.0 | 2 | 33.3 | 83% of runs | 97% of runs |  |
| 2-4 | auto+1 | 2 | 384 | +39 ± 6 | 2.5 | -0.4 ± 1.1 (none detected) | 2.0 | +1.4 ± 0.7 | 13.11 of 18 | -2.42 ± 0.39 | 0.0 | 0 | 32.8 | 77% of runs | 100% of runs |  |
| 1-4 | auto | 2 | 437 | +92 ± 13 | 5.2 | +2.3 ± 1.9 | 4.0 | +3.5 ± 1.2 | 12.40 of 18 | -3.12 ± 0.28 | 0.0 | 0 | 37.0 | 97% of runs | 100% of runs |  |
| 1-4 | 0 | 2 | 412 | +67 ± 14 | 4.2 | +1.3 ± 1.9 (none detected) | 3.3 | +2.8 ± 1.4 | 13.63 of 18 | -1.90 ± 0.37 | 0.0 | 0 | 35.0 | 90% of runs | 100% of runs |  |
| 1-4 | auto+1 | 2 | 446 | +101 ± 13 | 6.1 | +3.2 ± 2.7 | 4.6 | +4.1 ± 1.5 | 11.64 of 18 | -3.89 ± 0.34 | 0.0 | 0 | 37.2 | 93% of runs | 100% of runs |  |
| 3-3 | 1 | 2 | 375 | +30 ± 3 | 3.6 | +0.7 ± 0.9 (none detected) | 1.6 | +1.0 ± 0.6 | 14.23 of 18 | -1.30 ± 0.46 | 0.0 | 0 | 31.3 | 70% of runs | 100% of runs |  |
| 3-4 | 1 | 2 | 355 | +10 ± 2 | 3.5 | +0.6 ± 0.8 (none detected) | 1.4 | +0.9 ± 0.6 | 14.44 of 18 | -1.08 ± 0.30 | 0.0 | 1 | 30.2 | 60% of runs | 97% of runs |  |
| 2-2 | auto | 3 | 439 | +95 ± 7 | 4.8 | +1.9 ± 1.3 | 2.9 | +2.3 ± 0.6 | 11.12 of 18 | -4.41 ± 0.40 | 0.0 | 0 | 35.3 | 93% of runs | 100% of runs |  |
| 2-3 | auto | 3 | 391 | +46 ± 5 | 3.0 | +0.1 ± 1.0 (none detected) | 2.0 | +1.4 ± 0.6 | 13.12 of 18 | -2.41 ± 0.37 | 0.0 | 1 | 33.0 | 77% of runs | 100% of runs |  |
| 4-8 | auto | 2 | 339 | -6 ± 2 | 4.2 | +1.3 ± 0.8 | 0.5 | -0.0 ± 0.5 (none detected) | 15.02 of 18 | -0.50 ± 0.44 | 0.0 | 20 | 28.5 | 40% of runs | 100% of runs |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## 16 slots (18 CPUs), heavy-declared workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 353 | control | 7.4 | control | 3.8 | control | 12.36 of 18 | control | 0.0 | 1 | 24.3 | no | 87% of runs | yes |
| 2-4 | auto | 2 | 420 | +67 ± 8 | 13.5 | +6.1 ± 1.6 | 12.0 | +8.2 ± 1.4 | 10.81 of 18 | -1.55 ± 0.36 | 0.0 | 0 | 24.5 | no | 90% of runs |  |
| 2-4 | 0 | 2 | 399 | +45 ± 9 | 11.1 | +3.7 ± 1.5 | 9.3 | +5.5 ± 1.5 | 11.91 of 18 | -0.45 ± 0.33 | 0.0 | 0 | 24.2 | no | 87% of runs |  |
| 2-4 | auto+1 | 2 | 429 | +75 ± 8 | 13.2 | +5.8 ± 1.6 | 11.7 | +7.9 ± 1.4 | 10.47 of 18 | -1.89 ± 0.33 | 0.0 | 0 | 25.1 | no | 97% of runs |  |
| 1-4 | auto | 2 | 590 | +236 ± 20 | 34.4 | +27.0 ± 3.7 | 28.7 | +24.9 ± 3.1 | 9.18 of 18 | -3.18 ± 0.37 | 0.0 | 0 | 24.8 | no | 93% of runs |  |
| 1-4 | 0 | 2 | 515 | +161 ± 19 | 32.7 | +25.3 ± 4.8 | 24.0 | +20.2 ± 3.4 | 10.87 of 18 | -1.49 ± 0.45 | 0.0 | 0 | 24.5 | no | 90% of runs |  |
| 1-4 | auto+1 | 2 | 626 | +273 ± 23 | 38.0 | +30.6 ± 4.1 | 29.8 | +26.0 ± 3.1 | 8.55 of 18 | -3.81 ± 0.31 | 0.0 | 0 | 25.3 | no | 100% of runs |  |
| 3-3 | 1 | 2 | 388 | +34 ± 3 | 10.2 | +2.8 ± 1.1 | 8.1 | +4.3 ± 0.8 | 11.91 of 18 | -0.45 ± 0.39 | 0.0 | 0 | 24.8 | no | 93% of runs |  |
| 3-4 | 1 | 2 | 366 | +12 ± 3 | 8.5 | +1.1 ± 0.9 | 6.1 | +2.3 ± 0.7 | 12.33 of 18 | -0.02 ± 0.32 (none detected) | 0.0 | 0 | 24.8 | no | 93% of runs |  |
| 2-2 | auto | 3 | 458 | +105 ± 9 | 13.3 | +5.9 ± 1.3 | 12.6 | +8.8 ± 1.7 | 9.60 of 18 | -2.75 ± 0.41 | 0.0 | 0 | 24.8 | no | 93% of runs |  |
| 2-3 | auto | 3 | 421 | +68 ± 8 | 11.5 | +4.1 ± 1.3 | 10.3 | +6.5 ± 1.3 | 10.79 of 18 | -1.57 ± 0.37 | 0.0 | 0 | 25.1 | no | 97% of runs |  |
| 4-8 | auto | 2 | 347 | -7 ± 3 | 7.8 | +0.4 ± 0.6 (none detected) | 3.6 | -0.2 ± 0.3 (none detected) | 13.27 of 18 | +0.91 ± 0.23 | 0.0 | 8 | 25.1 | no | 97% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | auto | 2 | 347 | -7 ± 3 | 7.8 | +0.4 ± 0.6 (none detected) | 3.6 | -0.2 ± 0.3 (none detected) | 13.27 of 18 | +0.91 ± 0.23 | 0.0 | 8 | 25.1 | no | 97% of runs |
| 4 | auto | 2 | 353 | control | 7.4 | control | 3.8 | control | 12.36 of 18 | control | 0.0 | 1 | 24.3 | no | 87% of runs |

## 16 slots (18 CPUs), light workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 359 | control | 3.7 | control | 0.0 | control | 15.61 of 18 | control | 0.0 | 18 | 15.4 | no | 93% of runs | yes |
| 2-4 | auto | 2 | 391 | +32 ± 5 | 0.6 | -3.1 ± 0.9 | 0.1 | +0.1 ± 0.2 (none detected) | 13.86 of 18 | -1.74 ± 0.21 | 0.0 | 2 | 18.2 | 3% of runs |  |  |
| 2-4 | 0 | 2 | 380 | +21 ± 4 | 0.8 | -3.0 ± 0.9 | 0.1 | +0.1 ± 0.2 (none detected) | 14.49 of 18 | -1.11 ± 0.27 | 0.0 | 4 | 17.6 | 3% of runs | 10% of runs |  |
| 2-4 | auto+1 | 2 | 393 | +35 ± 5 | 0.4 | -3.3 ± 0.8 | 0.1 | +0.1 ± 0.1 (none detected) | 13.31 of 18 | -2.29 ± 0.24 | 0.0 | 0 | 18.5 | 7% of runs |  |  |
| 1-4 | auto | 2 | 434 | +75 ± 9 | 0.4 | -3.3 ± 0.8 | 0.1 | +0.1 ± 0.3 (none detected) | 12.67 of 18 | -2.93 ± 0.14 | 0.0 | 0 | 18.5 | 3% of runs |  |  |
| 1-4 | 0 | 2 | 411 | +52 ± 9 | 0.4 | -3.3 ± 0.8 | 0.1 | +0.1 ± 0.2 (none detected) | 14.05 of 18 | -1.56 ± 0.08 | 0.0 | 0 | 18.5 | 7% of runs |  |  |
| 1-4 | auto+1 | 2 | 443 | +84 ± 12 | 0.4 | -3.3 ± 0.8 | 0.2 | +0.2 ± 0.3 (none detected) | 11.99 of 18 | -3.61 ± 0.11 | 0.0 | 0 | 19.3 | 10% of runs | 10% of runs |  |
| 3-3 | 1 | 2 | 389 | +30 ± 2 | 1.7 | -2.0 ± 0.6 | 0.1 | +0.1 ± 0.0 | 14.45 of 18 | -1.16 ± 0.43 | 0.0 | 0 | 17.0 | no |  | yes |
| 3-4 | 1 | 2 | 369 | +10 ± 2 | 3.9 | +0.2 ± 0.7 (none detected) | 0.6 | +0.6 ± 0.1 | 14.62 of 18 | -0.99 ± 0.16 | 0.0 | 1 | 15.6 | no |  |  |
| 2-2 | auto | 3 | 457 | +98 ± 6 | 0.5 | -3.2 ± 0.9 | 0.1 | +0.1 ± 0.1 (none detected) | 12.25 of 18 | -3.36 ± 0.38 | 0.0 | 0 | 18.5 | 7% of runs |  |  |
| 2-3 | auto | 3 | 403 | +44 ± 4 | 0.7 | -3.0 ± 0.9 | 0.1 | +0.1 ± 0.0 | 13.37 of 18 | -2.24 ± 0.28 | 0.0 | 0 | 17.7 | 3% of runs |  |  |
| 4-8 | auto | 2 | 351 | -8 ± 3 | 4.9 | +1.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 15.46 of 18 | -0.15 ± 0.16 (none detected) | 0.0 | 25 | 15.4 | no | 100% of runs | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | busy threads p95 | vs control | at most 1 CPU idle % (cutoff) | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | auto | 2 | 351 | -8 ± 3 | 4.9 | +1.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 15.46 of 18 | -0.15 ± 0.16 (none detected) | 0.0 | 25 | 15.4 | no | 100% of runs |
| 4 | auto | 2 | 359 | control | 3.7 | control | 0.0 | control | 15.61 of 18 | control | 0.0 | 18 | 15.4 | no | 93% of runs |
| 3-3 | 1 | 2 | 389 | +30 ± 2 | 1.7 | -2.0 ± 0.6 | 0.1 | +0.1 ± 0.0 | 14.45 of 18 | -1.16 ± 0.43 | 0.0 | 0 | 17.0 | no |  |
