# Policy study: 30 replications a configuration, 0 screening points

Background 1 busy thread; memory 2 GiB a CPU, 6 GiB of it used by other work; no swap term. Means over the replications. A difference from the control (weight 4, headroom auto, 2 kept) is the mean over seeds of the configuration less the control on the same seed, ± its 95% interval; where the interval holds 0, no difference was detected.

## 6 slots (8 CPUs), heavy workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 690 | control | 75.4 | control | 28.4 | control | 0.0 | control | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 521 | -170 ± 16 | 34.0 | -41.4 ± 3.6 | 6.3 | -22.2 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 53 | 11.2 | no | yes |  |
| 2-4 | 0 | 2 | 510 | -180 ± 14 | 32.3 | -43.1 ± 2.9 | 4.6 | -23.8 ± 1.2 | 0.0 | +0.0 ± 0.0 (none detected) | 63 | 11.1 | no | yes | yes |
| 2-4 | auto+1 | 2 | 516 | -174 ± 15 | 32.0 | -43.4 ± 3.3 | 5.2 | -23.2 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 59 | 11.1 | no | yes | yes |
| 1-4 | auto | 2 | 626 | -64 ± 17 | 27.8 | -47.6 ± 3.4 | 13.4 | -15.1 ± 1.7 | 13.4 | +13.4 ± 1.7 | 20 | 16.5 | 80% of runs | yes |  |
| 1-4 | 0 | 2 | 585 | -105 ± 17 | 27.0 | -48.4 ± 3.9 | 12.6 | -15.8 ± 2.1 | 13.9 | +13.9 ± 2.0 | 37 | 16.4 | 83% of runs | yes |  |
| 1-4 | auto+1 | 2 | 643 | -47 ± 16 | 28.2 | -47.1 ± 3.2 | 14.5 | -13.9 ± 1.7 | 12.4 | +12.4 ± 2.1 | 14 | 16.5 | 90% of runs | yes |  |
| 3-3 | 1 | 2 | 490 | -200 ± 12 | 35.5 | -39.9 ± 2.1 | 0.0 | -28.4 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 7.6 | no | yes | yes |
| 3-4 | 1 | 2 | 493 | -197 ± 11 | 36.0 | -39.3 ± 2.3 | 1.7 | -26.8 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 75 | 7.6 | no | yes |  |
| 4-8 | auto | 2 | 677 | -13 ± 4 | 75.4 | -0.0 ± 0.5 (none detected) | 27.8 | -0.6 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-3 | 1 | 2 | 490 | -200 ± 12 | 35.5 | -39.9 ± 2.1 | 0.0 | -28.4 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 7.6 | no | yes |
| 2-4 | 0 | 2 | 510 | -180 ± 14 | 32.3 | -43.1 ± 2.9 | 4.6 | -23.8 ± 1.2 | 0.0 | +0.0 ± 0.0 (none detected) | 63 | 11.1 | no | yes |
| 2-4 | auto+1 | 2 | 516 | -174 ± 15 | 32.0 | -43.4 ± 3.3 | 5.2 | -23.2 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 59 | 11.1 | no | yes |

## 6 slots (8 CPUs), heavy-declared workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 690 | control | 75.4 | control | 28.4 | control | 0.0 | control | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 617 | -73 ± 9 | 53.3 | -22.1 ± 2.3 | 21.2 | -7.2 ± 0.9 | 0.0 | +0.0 ± 0.0 (none detected) | 10 | 7.6 | no | yes |  |
| 2-4 | 0 | 2 | 605 | -85 ± 10 | 54.6 | -20.8 ± 2.4 | 20.5 | -7.9 ± 0.9 | 0.0 | +0.0 ± 0.0 (none detected) | 20 | 7.6 | no | yes |  |
| 2-4 | auto+1 | 2 | 632 | -58 ± 7 | 54.7 | -20.6 ± 2.5 | 22.3 | -6.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 10 | 7.6 | no | yes |  |
| 1-4 | auto | 2 | 779 | +89 ± 17 | 64.4 | -10.9 ± 5.1 | 33.7 | +5.3 ± 1.4 | 0.6 | +0.6 ± 0.4 | 2 | 7.6 | no | yes |  |
| 1-4 | 0 | 2 | 716 | +26 ± 22 | 65.2 | -10.2 ± 4.8 | 30.9 | +2.4 ± 2.5 (none detected) | 0.3 | +0.3 ± 0.4 (none detected) | 13 | 7.6 | no | yes |  |
| 1-4 | auto+1 | 2 | 810 | +120 ± 17 | 63.5 | -11.9 ± 4.6 | 34.3 | +5.9 ± 1.5 | 0.3 | +0.3 ± 0.3 | 1 | 7.6 | no | yes |  |
| 3-3 | 1 | 2 | 490 | -200 ± 12 | 35.5 | -39.9 ± 2.1 | 0.0 | -28.4 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 7.6 | no | yes | yes |
| 3-4 | 1 | 2 | 493 | -197 ± 11 | 36.0 | -39.3 ± 2.3 | 1.7 | -26.8 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 75 | 7.6 | no | yes |  |
| 4-8 | auto | 2 | 677 | -13 ± 4 | 75.4 | -0.0 ± 0.5 (none detected) | 27.8 | -0.6 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-3 | 1 | 2 | 490 | -200 ± 12 | 35.5 | -39.9 ± 2.1 | 0.0 | -28.4 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 7.6 | no | yes |

## 6 slots (8 CPUs), light workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 770 | control | 87.5 | control | 29.0 | control | 0.0 | control | 0 | 2.6 | no |  |  |
| 2-4 | auto | 2 | 530 | -240 ± 12 | 34.3 | -53.2 ± 2.0 | 1.0 | -28.0 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 69 | 6.5 | no | yes |  |
| 2-4 | 0 | 2 | 524 | -246 ± 13 | 33.6 | -53.8 ± 2.3 | 0.0 | -29.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 6.4 | no | yes | yes |
| 2-4 | auto+1 | 2 | 530 | -240 ± 14 | 33.3 | -54.2 ± 1.8 | 0.0 | -29.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 75 | 6.4 | no | yes | yes |
| 1-4 | auto | 2 | 593 | -177 ± 16 | 13.2 | -74.3 ± 2.6 | 0.7 | -28.3 ± 0.8 | 27.5 | +27.5 ± 3.0 | 36 | 8.8 | 3% of runs | yes |  |
| 1-4 | 0 | 2 | 558 | -212 ± 15 | 17.0 | -70.5 ± 2.5 | 0.5 | -28.5 ± 0.7 | 24.6 | +24.6 ± 2.9 | 61 | 8.4 | no | yes | yes |
| 1-4 | auto+1 | 2 | 619 | -151 ± 15 | 11.0 | -76.5 ± 3.0 | 0.9 | -28.1 ± 0.9 | 25.6 | +25.6 ± 2.8 | 28 | 9.1 | 3% of runs | yes |  |
| 3-3 | 1 | 2 | 537 | -233 ± 12 | 44.7 | -42.8 ± 1.5 | 0.0 | -29.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 5.2 | no | yes |  |
| 3-4 | 1 | 2 | 540 | -230 ± 11 | 45.0 | -42.5 ± 2.1 | 1.5 | -27.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 78 | 5.2 | no | yes |  |
| 4-8 | auto | 2 | 758 | -12 ± 3 | 87.4 | -0.0 ± 0.3 (none detected) | 28.4 | -0.5 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 2.6 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2-4 | 0 | 2 | 524 | -246 ± 13 | 33.6 | -53.8 ± 2.3 | 0.0 | -29.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 79 | 6.4 | no | yes |
| 2-4 | auto+1 | 2 | 530 | -240 ± 14 | 33.3 | -54.2 ± 1.8 | 0.0 | -29.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 75 | 6.4 | no | yes |
| 1-4 | 0 | 2 | 558 | -212 ± 15 | 17.0 | -70.5 ± 2.5 | 0.5 | -28.5 ± 0.7 | 24.6 | +24.6 ± 2.9 | 61 | 8.4 | no | yes |

## 8 slots (10 CPUs), heavy workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 408 | control | 24.2 | control | 0.0 | control | 0.0 | control | 73 | 9.5 | no | yes | yes |
| 2-4 | auto | 2 | 468 | +60 ± 10 | 21.5 | -2.7 ± 2.6 | 7.9 | +7.9 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 32 | 15.6 | 30% of runs | yes |  |
| 2-4 | 0 | 2 | 467 | +59 ± 9 | 21.2 | -2.9 ± 2.3 | 8.5 | +8.5 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 22 | 15.3 | 33% of runs | yes |  |
| 2-4 | auto+1 | 2 | 473 | +65 ± 11 | 21.3 | -2.8 ± 2.2 | 8.2 | +8.2 ± 1.4 | 0.0 | +0.0 ± 0.0 (none detected) | 28 | 15.4 | 30% of runs | yes |  |
| 1-4 | auto | 2 | 598 | +190 ± 17 | 23.6 | -0.6 ± 2.9 (none detected) | 14.6 | +14.6 ± 2.0 | 0.5 | +0.5 ± 0.5 (none detected) | 2 | 20.2 | 80% of runs | yes |  |
| 1-4 | 0 | 2 | 572 | +164 ± 16 | 21.5 | -2.7 ± 3.4 (none detected) | 13.6 | +13.6 ± 2.2 | 0.8 | +0.8 ± 0.5 | 8 | 19.2 | 80% of runs | yes |  |
| 1-4 | auto+1 | 2 | 617 | +209 ± 15 | 24.0 | -0.2 ± 2.6 (none detected) | 14.6 | +14.6 ± 2.0 | 0.5 | +0.5 ± 0.4 | 1 | 19.2 | 70% of runs | yes |  |
| 3-3 | 1 | 2 | 504 | +96 ± 11 | 37.8 | +13.6 ± 1.5 | 16.2 | +16.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 3-4 | 1 | 2 | 480 | +72 ± 10 | 36.6 | +12.4 ± 1.8 | 13.7 | +13.7 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 4-8 | auto | 2 | 414 | +6 ± 7 (none detected) | 24.8 | +0.7 ± 1.1 (none detected) | 1.6 | +1.6 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 67 | 9.5 | no | yes |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 408 | control | 24.2 | control | 0.0 | control | 0.0 | control | 73 | 9.5 | no | yes |

## 8 slots (10 CPUs), heavy-declared workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 408 | control | 24.2 | control | 0.0 | control | 0.0 | control | 73 | 9.5 | no | yes | yes |
| 2-4 | auto | 2 | 497 | +88 ± 10 | 28.3 | +4.1 ± 2.0 | 13.6 | +13.6 ± 1.1 | 0.0 | +0.0 ± 0.0 (none detected) | 5 | 9.5 | no | yes |  |
| 2-4 | 0 | 2 | 489 | +81 ± 8 | 27.3 | +3.1 ± 1.7 | 12.4 | +12.4 ± 1.2 | 0.0 | +0.0 ± 0.0 (none detected) | 3 | 9.5 | no | yes |  |
| 2-4 | auto+1 | 2 | 508 | +100 ± 10 | 29.0 | +4.8 ± 1.7 | 13.8 | +13.8 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 4 | 9.5 | no | yes |  |
| 1-4 | auto | 2 | 704 | +296 ± 24 | 51.1 | +26.9 ± 4.8 | 31.8 | +31.8 ± 2.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 1-4 | 0 | 2 | 637 | +229 ± 25 | 48.6 | +24.4 ± 6.0 | 26.6 | +26.6 ± 3.3 | 0.1 | +0.1 ± 0.2 (none detected) | 2 | 9.5 | no | yes |  |
| 1-4 | auto+1 | 2 | 733 | +325 ± 24 | 51.5 | +27.3 ± 4.7 | 31.6 | +31.6 ± 2.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 3-3 | 1 | 2 | 504 | +96 ± 11 | 37.8 | +13.6 ± 1.5 | 16.2 | +16.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 3-4 | 1 | 2 | 480 | +72 ± 10 | 36.6 | +12.4 ± 1.8 | 13.7 | +13.7 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 4-8 | auto | 2 | 414 | +6 ± 7 (none detected) | 24.8 | +0.7 ± 1.1 (none detected) | 1.6 | +1.6 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 67 | 9.5 | no | yes |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 408 | control | 24.2 | control | 0.0 | control | 0.0 | control | 73 | 9.5 | no | yes |

## 8 slots (10 CPUs), light workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 450 | control | 31.9 | control | 0.0 | control | 0.0 | control | 78 | 6.4 | no | yes | yes |
| 2-4 | auto | 2 | 470 | +20 ± 7 | 15.7 | -16.2 ± 1.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 50 | 9.1 | no | yes | yes |
| 2-4 | 0 | 2 | 469 | +19 ± 7 | 17.9 | -14.0 ± 1.7 | 2.2 | +2.2 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 33 | 9.6 | no | yes | yes |
| 2-4 | auto+1 | 2 | 476 | +26 ± 7 | 16.3 | -15.6 ± 1.7 | 0.4 | +0.4 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 45 | 9.3 | no | yes |  |
| 1-4 | auto | 2 | 568 | +118 ± 9 | 4.0 | -27.9 ± 2.4 | 0.8 | +0.8 ± 0.4 | 2.0 | +2.0 ± 0.7 | 7 | 11.6 | 3% of runs | yes |  |
| 1-4 | 0 | 2 | 532 | +82 ± 10 | 7.4 | -24.5 ± 2.2 | 0.7 | +0.7 ± 0.5 | 2.1 | +2.1 ± 0.8 | 19 | 11.0 | 7% of runs | yes |  |
| 1-4 | auto+1 | 2 | 597 | +147 ± 9 | 1.6 | -30.3 ± 2.5 | 0.6 | +0.6 ± 0.5 | 1.6 | +1.6 ± 0.7 | 3 | 11.7 | 7% of runs | yes |  |
| 3-3 | 1 | 2 | 561 | +111 ± 9 | 47.9 | +16.1 ± 1.6 | 17.8 | +17.8 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 6.4 | no |  |  |
| 3-4 | 1 | 2 | 539 | +89 ± 11 | 46.6 | +14.7 ± 1.7 | 16.0 | +16.0 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 6.4 | no |  |  |
| 4-8 | auto | 2 | 454 | +4 ± 5 (none detected) | 32.9 | +1.1 ± 1.3 (none detected) | 1.1 | +1.1 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 73 | 6.5 | no | yes |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 450 | control | 31.9 | control | 0.0 | control | 0.0 | control | 78 | 6.4 | no | yes |
| 2-4 | 0 | 2 | 469 | +19 ± 7 | 17.9 | -14.0 ± 1.7 | 2.2 | +2.2 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 33 | 9.6 | no | yes |
| 2-4 | auto | 2 | 470 | +20 ± 7 | 15.7 | -16.2 ± 1.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 50 | 9.1 | no | yes |

## 16 slots (18 CPUs), heavy workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 345 | control | 3.3 | control | 0.7 | control | 0.0 | control | 14 | 29.9 | 57% of runs | yes |  |
| 2-4 | auto | 2 | 379 | +34 ± 5 | 2.5 | -0.8 ± 1.2 (none detected) | 2.0 | +1.2 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 33.6 | 87% of runs | yes |  |
| 2-4 | 0 | 2 | 371 | +26 ± 4 | 1.9 | -1.4 ± 1.0 | 1.5 | +0.7 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 3 | 33.6 | 83% of runs | yes |  |
| 2-4 | auto+1 | 2 | 388 | +43 ± 7 | 3.2 | -0.1 ± 1.0 (none detected) | 2.4 | +1.6 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 33.6 | 87% of runs | yes |  |
| 1-4 | auto | 2 | 425 | +80 ± 12 | 3.9 | +0.6 ± 1.6 (none detected) | 3.5 | +2.7 ± 1.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 34.2 | 80% of runs | yes |  |
| 1-4 | 0 | 2 | 403 | +58 ± 13 | 3.6 | +0.3 ± 1.6 (none detected) | 2.8 | +2.0 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 35.0 | 87% of runs | yes |  |
| 1-4 | auto+1 | 2 | 444 | +99 ± 15 | 6.3 | +3.0 ± 2.2 | 4.6 | +3.8 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 36.1 | 87% of runs | yes |  |
| 3-3 | 1 | 2 | 375 | +30 ± 3 | 3.6 | +0.2 ± 1.0 (none detected) | 1.6 | +0.8 ± 0.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 31.6 | 70% of runs | yes |  |
| 3-4 | 1 | 2 | 356 | +11 ± 2 | 4.1 | +0.7 ± 0.8 (none detected) | 1.6 | +0.9 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 30.2 | 60% of runs | yes |  |
| 4-8 | auto | 2 | 339 | -6 ± 2 | 3.8 | +0.5 ± 0.7 (none detected) | 0.5 | -0.2 ± 0.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 20 | 29.1 | 47% of runs | yes |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## 16 slots (18 CPUs), heavy-declared workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 353 | control | 7.3 | control | 3.8 | control | 0.0 | control | 1 | 24.8 | no |  | yes |
| 2-4 | auto | 2 | 421 | +68 ± 9 | 12.6 | +5.3 ± 2.1 | 11.6 | +7.8 ± 1.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.8 | no |  |  |
| 2-4 | 0 | 2 | 398 | +45 ± 8 | 10.2 | +2.9 ± 1.5 | 9.2 | +5.3 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.8 | no |  |  |
| 2-4 | auto+1 | 2 | 427 | +74 ± 9 | 13.5 | +6.2 ± 2.1 | 11.6 | +7.8 ± 1.6 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.5 | no |  |  |
| 1-4 | auto | 2 | 591 | +238 ± 21 | 34.8 | +27.5 ± 4.3 | 27.7 | +23.8 ± 3.0 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.1 | no | yes |  |
| 1-4 | 0 | 2 | 512 | +159 ± 20 | 31.1 | +23.8 ± 4.7 | 22.6 | +18.7 ± 3.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.1 | no | yes |  |
| 1-4 | auto+1 | 2 | 631 | +278 ± 23 | 38.0 | +30.7 ± 3.9 | 30.2 | +26.4 ± 2.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.8 | no |  |  |
| 3-3 | 1 | 2 | 389 | +36 ± 4 | 10.3 | +3.1 ± 0.9 | 8.6 | +4.7 ± 0.9 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.8 | no |  |  |
| 3-4 | 1 | 2 | 368 | +15 ± 4 | 8.7 | +1.4 ± 0.9 | 6.3 | +2.5 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.1 | no | yes |  |
| 4-8 | auto | 2 | 349 | -4 ± 3 | 8.2 | +0.9 ± 0.7 | 4.0 | +0.2 ± 0.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 6 | 25.1 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | auto | 2 | 349 | -4 ± 3 | 8.2 | +0.9 ± 0.7 | 4.0 | +0.2 ± 0.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 6 | 25.1 | no | yes |
| 4 | auto | 2 | 353 | control | 7.3 | control | 3.8 | control | 0.0 | control | 1 | 24.8 | no |  |

## 16 slots (18 CPUs), light workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 359 | control | 3.6 | control | 0.0 | control | 0.0 | control | 18 | 15.2 | no | yes | yes |
| 2-4 | auto | 2 | 394 | +35 ± 6 | 0.7 | -2.9 ± 1.0 | 0.0 | +0.0 ± 0.1 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 17.6 | 3% of runs |  |  |
| 2-4 | 0 | 2 | 381 | +22 ± 3 | 0.4 | -3.1 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 4 | 17.4 | 3% of runs |  |  |
| 2-4 | auto+1 | 2 | 400 | +41 ± 6 | 0.6 | -3.0 ± 0.9 | 0.1 | +0.1 ± 0.1 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 18.3 | 7% of runs |  |  |
| 1-4 | auto | 2 | 435 | +76 ± 11 | 0.4 | -3.2 ± 0.8 | 0.1 | +0.1 ± 0.2 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 18.9 | 3% of runs |  |  |
| 1-4 | 0 | 2 | 408 | +49 ± 9 | 0.4 | -3.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 19.1 | 7% of runs |  |  |
| 1-4 | auto+1 | 2 | 447 | +88 ± 10 | 0.5 | -3.1 ± 0.8 | 0.0 | +0.0 ± 0.1 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 19.7 | 3% of runs |  |  |
| 3-3 | 1 | 2 | 389 | +30 ± 2 | 1.7 | -1.8 ± 0.6 | 0.2 | +0.2 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 16.4 | no |  | yes |
| 3-4 | 1 | 2 | 370 | +11 ± 3 | 3.7 | +0.1 ± 0.5 (none detected) | 0.5 | +0.5 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 16.4 | no |  |  |
| 4-8 | auto | 2 | 352 | -7 ± 2 | 4.5 | +1.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 25 | 15.0 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | auto | 2 | 352 | -7 ± 2 | 4.5 | +1.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 25 | 15.0 | no | yes |
| 4 | auto | 2 | 359 | control | 3.6 | control | 0.0 | control | 0.0 | control | 18 | 15.2 | no | yes |
| 3-3 | 1 | 2 | 389 | +30 ± 2 | 1.7 | -1.8 ± 0.6 | 0.2 | +0.2 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 16.4 | no |  |
