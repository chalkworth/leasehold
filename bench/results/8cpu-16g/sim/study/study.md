# Policy study: 30 replications a configuration, 32 screening points

Background 1 busy thread; memory 2 GiB a CPU, 6 GiB of it used by other work; no swap term. Means over the replications. A difference from the control (weight 4, headroom auto, 2 kept) is the mean over seeds of the configuration less the control on the same seed, ± its 95% interval; where the interval holds 0, no difference was detected.

## 6 slots (8 CPUs), heavy workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 806 | control | 94.8 | control | 29.3 | control | 0.0 | control | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 686 | -120 ± 10 | 60.5 | -34.3 ± 1.9 | 8.4 | -20.9 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 61 | 11.3 | no | yes |  |
| 2-4 | 0 | 2 | 684 | -121 ± 9 | 62.3 | -32.5 ± 2.8 | 8.5 | -20.8 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 64 | 11.3 | no | yes |  |
| 2-4 | auto+1 | 2 | 694 | -111 ± 10 | 62.4 | -32.4 ± 2.6 | 8.9 | -20.4 ± 1.1 | 0.0 | +0.0 ± 0.0 (none detected) | 61 | 11.3 | no | yes |  |
| 1-4 | auto | 2 | 804 | -2 ± 14 (none detected) | 60.2 | -34.6 ± 3.5 | 20.9 | -8.4 ± 1.3 | 18.1 | +18.1 ± 1.8 | 22 | 18.8 | 100% of runs | yes |  |
| 1-4 | 0 | 2 | 770 | -35 ± 13 | 59.6 | -35.2 ± 4.5 | 19.5 | -9.9 ± 1.6 | 20.5 | +20.5 ± 2.3 | 33 | 18.4 | 97% of runs | yes |  |
| 1-4 | auto+1 | 2 | 808 | +3 ± 15 (none detected) | 55.8 | -39.0 ± 3.0 | 20.5 | -8.8 ± 1.3 | 17.7 | +17.7 ± 2.5 | 19 | 18.7 | 100% of runs | yes |  |
| 3-3 | 1 | 2 | 648 | -158 ± 8 | 61.0 | -33.8 ± 0.8 | 0.0 | -29.3 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 86 | 7.6 | no | yes | yes |
| 3-4 | 1 | 2 | 648 | -158 ± 9 | 60.3 | -34.5 ± 0.9 | 1.0 | -28.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes | yes |
| 4-8 | auto | 2 | 799 | -7 ± 1 | 94.6 | -0.2 ± 0.2 (none detected) | 28.8 | -0.5 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 571 | -234 ± 11 | 51.5 | -43.3 ± 1.0 | 0.4 | -28.9 ± 0.7 | 79.9 | +79.9 ± 2.9 | 81 | 7.6 | no | yes |
| 4-4 | 0 | 0 | 572 | -234 ± 11 | 51.9 | -42.9 ± 1.1 | 0.0 | -29.3 ± 0.7 | 82.9 | +82.9 ± 2.7 | 84 | 7.6 | no | yes |
| 3-8 | 1 | 0 | 632 | -173 ± 7 | 60.0 | -34.7 ± 1.1 | 18.8 | -10.5 ± 0.5 | 12.2 | +12.2 ± 2.4 | 0 | 7.6 | no | yes |
| 3-8 | 3 | 0 | 641 | -164 ± 8 | 60.2 | -34.6 ± 0.9 | 19.2 | -10.1 ± 0.4 | 2.9 | +2.9 ± 0.6 | 3 | 7.6 | no | yes |
| 3-4 | 0 | 1 | 642 | -164 ± 8 | 61.0 | -33.8 ± 0.8 | 10.9 | -18.4 ± 0.4 | 3.3 | +3.3 ± 0.6 | 3 | 7.6 | no | yes |
| 3-5 | 1 | 2 | 643 | -163 ± 8 | 60.4 | -34.4 ± 0.9 | 0.4 | -28.9 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes |
| 3-3 | 1 | 2 | 648 | -158 ± 8 | 61.0 | -33.8 ± 0.8 | 0.0 | -29.3 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 86 | 7.6 | no | yes |
| 3-4 | 1 | 2 | 648 | -158 ± 9 | 60.3 | -34.5 ± 0.9 | 1.0 | -28.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes |

## 6 slots (8 CPUs), heavy-declared workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 806 | control | 94.8 | control | 29.3 | control | 0.0 | control | 0 | 3.8 | no |  |  |
| 2-4 | auto | 2 | 767 | -38 ± 6 | 76.5 | -18.2 ± 1.0 | 24.7 | -4.6 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 7 | 7.6 | no | yes |  |
| 2-4 | 0 | 2 | 761 | -45 ± 6 | 76.6 | -18.2 ± 0.8 | 24.1 | -5.3 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 13 | 7.6 | no | yes |  |
| 2-4 | auto+1 | 2 | 774 | -31 ± 5 | 76.3 | -18.5 ± 1.0 | 25.2 | -4.2 ± 0.5 | 0.0 | +0.0 ± 0.0 (none detected) | 7 | 7.6 | no | yes |  |
| 1-4 | auto | 2 | 947 | +141 ± 18 | 99.8 | +5.1 ± 5.3 (none detected) | 40.5 | +11.2 ± 1.3 | 0.5 | +0.5 ± 0.5 (none detected) | 1 | 7.6 | no | yes |  |
| 1-4 | 0 | 2 | 907 | +102 ± 17 | 97.4 | +2.6 ± 5.6 (none detected) | 38.4 | +9.0 ± 1.6 | 0.1 | +0.1 ± 0.1 (none detected) | 9 | 7.6 | no | yes |  |
| 1-4 | auto+1 | 2 | 976 | +170 ± 18 | 99.6 | +4.9 ± 5.4 (none detected) | 40.6 | +11.3 ± 1.4 | 0.2 | +0.2 ± 0.2 (none detected) | 1 | 7.6 | no | yes |  |
| 3-3 | 1 | 2 | 648 | -158 ± 8 | 61.0 | -33.8 ± 0.8 | 0.0 | -29.3 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 86 | 7.6 | no | yes | yes |
| 3-4 | 1 | 2 | 648 | -158 ± 9 | 60.3 | -34.5 ± 0.9 | 1.0 | -28.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes | yes |
| 4-8 | auto | 2 | 799 | -7 ± 1 | 94.6 | -0.2 ± 0.2 (none detected) | 28.8 | -0.5 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 3.8 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 571 | -234 ± 11 | 51.5 | -43.3 ± 1.0 | 0.4 | -28.9 ± 0.7 | 79.9 | +79.9 ± 2.9 | 81 | 7.6 | no | yes |
| 4-4 | 0 | 0 | 572 | -234 ± 11 | 51.9 | -42.9 ± 1.1 | 0.0 | -29.3 ± 0.7 | 82.9 | +82.9 ± 2.7 | 84 | 7.6 | no | yes |
| 3-8 | 1 | 0 | 632 | -173 ± 7 | 60.0 | -34.7 ± 1.1 | 18.8 | -10.5 ± 0.5 | 12.2 | +12.2 ± 2.4 | 0 | 7.6 | no | yes |
| 3-8 | 3 | 0 | 641 | -164 ± 8 | 60.2 | -34.6 ± 0.9 | 19.2 | -10.1 ± 0.4 | 2.9 | +2.9 ± 0.6 | 3 | 7.6 | no | yes |
| 3-4 | 0 | 1 | 642 | -164 ± 8 | 61.0 | -33.8 ± 0.8 | 10.9 | -18.4 ± 0.4 | 3.3 | +3.3 ± 0.6 | 3 | 7.6 | no | yes |
| 3-5 | 1 | 2 | 643 | -163 ± 8 | 60.4 | -34.4 ± 0.9 | 0.4 | -28.9 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes |
| 3-3 | 1 | 2 | 648 | -158 ± 8 | 61.0 | -33.8 ± 0.8 | 0.0 | -29.3 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 86 | 7.6 | no | yes |
| 3-4 | 1 | 2 | 648 | -158 ± 9 | 60.3 | -34.5 ± 0.9 | 1.0 | -28.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.6 | no | yes |
| 2-6 | 2 | 0 | 654 | -151 ± 11 | 58.4 | -36.4 ± 2.4 | 21.4 | -7.9 ± 1.0 | 5.1 | +5.1 ± 1.0 | 5 | 7.6 | no | yes |
| 2-3 | 2 | 0 | 665 | -141 ± 10 | 56.4 | -38.3 ± 2.8 | 21.6 | -7.7 ± 0.9 | 4.5 | +4.5 ± 1.0 | 3 | 7.6 | no | yes |
| 2-3 | 0 | 1 | 666 | -139 ± 9 | 57.4 | -37.4 ± 2.2 | 15.2 | -14.1 ± 1.0 | 1.3 | +1.3 ± 0.6 | 1 | 7.6 | no | yes |

## 6 slots (8 CPUs), light workload

Jobs using more than 5 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 800 | control | 91.6 | control | 29.1 | control | 0.0 | control | 0 | 2.6 | no |  |  |
| 2-4 | auto | 2 | 639 | -161 ± 10 | 49.5 | -42.1 ± 1.3 | 0.4 | -28.6 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 77 | 7.5 | no | yes |  |
| 2-4 | 0 | 2 | 634 | -166 ± 11 | 49.1 | -42.5 ± 1.3 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.4 | no | yes | yes |
| 2-4 | auto+1 | 2 | 636 | -165 ± 11 | 47.9 | -43.7 ± 1.8 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 80 | 7.6 | no | yes | yes |
| 1-4 | auto | 2 | 681 | -120 ± 13 | 22.0 | -69.5 ± 2.3 | 1.9 | -27.2 ± 1.0 | 35.6 | +35.6 ± 2.6 | 43 | 10.2 | 7% of runs | yes |  |
| 1-4 | 0 | 2 | 658 | -142 ± 11 | 24.5 | -67.1 ± 2.2 | 1.5 | -27.5 ± 1.0 | 33.8 | +33.8 ± 3.6 | 64 | 9.4 | 7% of runs | yes |  |
| 1-4 | auto+1 | 2 | 695 | -106 ± 14 | 21.7 | -69.8 ± 2.4 | 2.1 | -26.9 ± 0.9 | 33.7 | +33.7 ± 3.2 | 36 | 10.2 | 7% of runs | yes |  |
| 3-3 | 1 | 2 | 643 | -158 ± 9 | 58.8 | -32.7 ± 1.2 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 85 | 5.2 | no | yes |  |
| 3-4 | 1 | 2 | 644 | -156 ± 9 | 58.7 | -32.9 ± 1.2 | 1.1 | -28.0 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 5.2 | no | yes |  |
| 4-8 | auto | 2 | 793 | -7 ± 1 | 91.6 | +0.0 ± 0.1 (none detected) | 28.5 | -0.5 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 2.6 | no |  |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-8 | 0 | 0 | 563 | -238 ± 12 | 49.8 | -41.8 ± 1.0 | 0.4 | -28.7 ± 0.7 | 79.3 | +79.3 ± 2.6 | 80 | 5.2 | no | yes |
| 4-4 | 0 | 0 | 566 | -234 ± 13 | 49.9 | -41.7 ± 1.4 | 0.0 | -29.1 ± 0.7 | 82.4 | +82.4 ± 2.8 | 83 | 5.2 | no | yes |
| 2-6 | 2 | 0 | 575 | -225 ± 14 | 30.2 | -61.4 ± 2.1 | 0.7 | -28.4 ± 0.6 | 65.3 | +65.3 ± 3.1 | 66 | 8.6 | no | yes |
| 2-3 | 2 | 0 | 581 | -219 ± 13 | 29.2 | -62.4 ± 1.6 | 0.7 | -28.4 ± 0.7 | 65.7 | +65.7 ± 3.1 | 64 | 8.4 | no | yes |
| 3-8 | 1 | 0 | 628 | -173 ± 9 | 58.9 | -32.7 ± 1.1 | 18.6 | -10.5 ± 0.4 | 12.1 | +12.1 ± 1.9 | 0 | 5.2 | no | yes |
| 2-3 | 0 | 1 | 633 | -167 ± 10 | 47.9 | -43.7 ± 1.9 | 9.6 | -19.4 ± 0.5 | 1.7 | +1.7 ± 0.6 | 2 | 7.3 | no | yes |
| 2-4 | 0 | 2 | 634 | -166 ± 11 | 49.1 | -42.5 ± 1.3 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 83 | 7.4 | no | yes |
| 2-3 | 0 | 2 | 634 | -166 ± 9 | 48.5 | -43.0 ± 1.5 | 0.3 | -28.7 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 81 | 7.1 | no | yes |
| 2-4 | auto+1 | 2 | 636 | -165 ± 11 | 47.9 | -43.7 ± 1.8 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 80 | 7.6 | no | yes |
| 2-2 | 2 | 2 | 651 | -150 ± 10 | 47.1 | -44.4 ± 1.9 | 0.0 | -29.1 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 78 | 7.4 | no | yes |
| 1-4 | 0 | 3 | 718 | -82 ± 12 | 34.0 | -57.6 ± 2.6 | 1.5 | -27.6 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 71 | 9.6 | no | yes |
| 1-4 | 2 | 3 | 739 | -62 ± 10 | 33.9 | -57.7 ± 2.8 | 1.5 | -27.6 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 58 | 9.5 | no | yes |
| 1-1 | 0 | 3 | 769 | -32 ± 12 | 33.5 | -58.1 ± 2.2 | 1.3 | -27.8 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 54 | 9.7 | no | yes |

## 8 slots (10 CPUs), heavy workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 603 | control | 56.9 | control | 0.0 | control | 0.0 | control | 85 | 9.5 | no | yes | yes |
| 2-4 | auto | 2 | 675 | +72 ± 8 | 54.2 | -2.7 ± 2.6 | 13.7 | +13.7 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 41 | 16.8 | 57% of runs | yes |  |
| 2-4 | 0 | 2 | 671 | +67 ± 8 | 56.2 | -0.7 ± 2.9 (none detected) | 14.8 | +14.8 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 34 | 16.5 | 50% of runs | yes |  |
| 2-4 | auto+1 | 2 | 678 | +74 ± 8 | 54.4 | -2.5 ± 2.7 (none detected) | 13.7 | +13.7 ± 1.0 | 0.0 | +0.0 ± 0.0 (none detected) | 39 | 16.8 | 57% of runs | yes |  |
| 1-4 | auto | 2 | 819 | +215 ± 14 | 54.8 | -2.1 ± 3.4 (none detected) | 26.1 | +26.1 ± 2.2 | 1.5 | +1.5 ± 0.9 | 3 | 21.9 | 97% of runs | yes |  |
| 1-4 | 0 | 2 | 799 | +196 ± 14 | 55.6 | -1.2 ± 3.3 (none detected) | 25.0 | +25.0 ± 2.0 | 2.1 | +2.1 ± 0.9 | 8 | 20.5 | 90% of runs | yes |  |
| 1-4 | auto+1 | 2 | 837 | +234 ± 14 | 56.7 | -0.2 ± 3.2 (none detected) | 26.2 | +26.2 ± 1.8 | 1.5 | +1.5 ± 0.6 | 2 | 21.7 | 97% of runs | yes |  |
| 3-3 | 1 | 2 | 700 | +96 ± 4 | 67.4 | +10.5 ± 0.7 | 20.0 | +20.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 3-4 | 1 | 2 | 687 | +83 ± 5 | 67.1 | +10.2 ± 0.8 | 19.2 | +19.2 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 4-8 | auto | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-9 | 0 | 0 | 594 | -10 ± 4 | 56.5 | -0.4 ± 1.0 (none detected) | 14.7 | +14.7 ± 0.6 | 13.4 | +13.4 ± 2.6 | 0 | 9.5 | no | yes |
| 4-5 | 0 | 0 | 595 | -8 ± 4 | 56.8 | -0.1 ± 1.0 (none detected) | 14.6 | +14.6 ± 0.7 | 16.0 | +16.0 ± 3.1 | 0 | 9.5 | no | yes |
| 3-10 | 1 | 0 | 600 | -3 ± 9 (none detected) | 53.0 | -3.8 ± 3.0 | 15.5 | +15.5 ± 1.5 | 55.9 | +55.9 ± 3.3 | 0 | 14.2 | no | yes |
| 4-9 | 1 | 1 | 601 | -2 ± 4 (none detected) | 56.6 | -0.3 ± 0.9 (none detected) | 8.8 | +8.8 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |
| 4-8 | auto | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes |
| 4-8 | 2 | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes |
| 4 | auto | 2 | 603 | control | 56.9 | control | 0.0 | control | 0.0 | control | 85 | 9.5 | no | yes |
| 3-9 | 3 | 0 | 608 | +4 ± 8 (none detected) | 54.2 | -2.7 ± 2.9 (none detected) | 15.9 | +15.9 ± 1.2 | 55.8 | +55.8 ± 3.7 | 5 | 14.2 | no | yes |
| 3-5 | 0 | 1 | 609 | +5 ± 8 (none detected) | 52.9 | -4.0 ± 1.8 | 10.2 | +10.2 ± 1.1 | 51.0 | +51.0 ± 3.2 | 52 | 14.2 | no | yes |

## 8 slots (10 CPUs), heavy-declared workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 603 | control | 56.9 | control | 0.0 | control | 0.0 | control | 85 | 9.5 | no | yes | yes |
| 2-4 | auto | 2 | 715 | +111 ± 7 | 65.4 | +8.5 ± 2.4 | 22.7 | +22.7 ± 1.1 | 0.0 | +0.0 ± 0.0 (none detected) | 3 | 9.5 | no | yes |  |
| 2-4 | 0 | 2 | 707 | +104 ± 7 | 65.7 | +8.8 ± 2.4 | 22.8 | +22.8 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 9.5 | no | yes |  |
| 2-4 | auto+1 | 2 | 719 | +116 ± 7 | 65.6 | +8.7 ± 2.4 | 23.0 | +23.0 ± 1.1 | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 9.5 | no | yes |  |
| 1-4 | auto | 2 | 1033 | +430 ± 32 | 123.7 | +66.8 ± 7.8 | 47.1 | +47.1 ± 2.1 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 1-4 | 0 | 2 | 984 | +381 ± 33 | 121.9 | +65.0 ± 8.5 | 44.7 | +44.7 ± 2.5 | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 9.5 | no | yes |  |
| 1-4 | auto+1 | 2 | 1065 | +462 ± 34 | 125.3 | +68.4 ± 7.3 | 46.9 | +46.9 ± 2.0 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |  |
| 3-3 | 1 | 2 | 700 | +96 ± 4 | 67.4 | +10.5 ± 0.7 | 20.0 | +20.0 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 3-4 | 1 | 2 | 687 | +83 ± 5 | 67.1 | +10.2 ± 0.8 | 19.2 | +19.2 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.4 | no | yes |  |
| 4-8 | auto | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-9 | 3 | 0 | 583 | -21 ± 6 | 49.5 | -7.4 ± 2.2 | 12.2 | +12.2 ± 1.0 | 59.4 | +59.4 ± 3.7 | 6 | 9.5 | no | yes |
| 3-10 | 1 | 0 | 583 | -20 ± 6 | 50.9 | -6.0 ± 2.2 | 12.5 | +12.5 ± 1.0 | 57.0 | +57.0 ± 4.0 | 0 | 9.5 | no | yes |
| 4-9 | 0 | 0 | 594 | -10 ± 4 | 56.5 | -0.4 ± 1.0 (none detected) | 14.7 | +14.7 ± 0.6 | 13.4 | +13.4 ± 2.6 | 0 | 9.5 | no | yes |
| 4-5 | 0 | 0 | 595 | -8 ± 4 | 56.8 | -0.1 ± 1.0 (none detected) | 14.6 | +14.6 ± 0.7 | 16.0 | +16.0 ± 3.1 | 0 | 9.5 | no | yes |
| 4-9 | 1 | 1 | 601 | -2 ± 4 (none detected) | 56.6 | -0.3 ± 0.9 (none detected) | 8.8 | +8.8 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 9.5 | no | yes |
| 4-8 | auto | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes |
| 4-8 | 2 | 2 | 603 | -0 ± 4 (none detected) | 56.8 | -0.1 ± 0.9 (none detected) | 0.8 | +0.8 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 9.5 | no | yes |
| 4 | auto | 2 | 603 | control | 56.9 | control | 0.0 | control | 0.0 | control | 85 | 9.5 | no | yes |

## 8 slots (10 CPUs), light workload

Jobs using more than 9 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 587 | control | 53.1 | control | 0.0 | control | 0.0 | control | 85 | 6.4 | no | yes | yes |
| 2-4 | auto | 2 | 588 | +2 ± 6 (none detected) | 29.5 | -23.6 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 69 | 10.3 | no | yes | yes |
| 2-4 | 0 | 2 | 592 | +5 ± 5 | 31.1 | -22.0 ± 1.6 | 1.5 | +1.5 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 56 | 10.2 | no | yes |  |
| 2-4 | auto+1 | 2 | 590 | +4 ± 6 (none detected) | 29.6 | -23.5 ± 1.5 | 0.2 | +0.2 ± 0.1 | 0.0 | +0.0 ± 0.0 (none detected) | 67 | 10.4 | no | yes |  |
| 1-4 | auto | 2 | 675 | +88 ± 10 | 11.2 | -41.9 ± 3.1 | 2.0 | +2.0 ± 0.9 | 5.9 | +5.9 ± 1.4 | 11 | 13.1 | 10% of runs | yes |  |
| 1-4 | 0 | 2 | 645 | +58 ± 9 | 12.9 | -40.2 ± 1.6 | 1.4 | +1.4 ± 0.6 | 6.3 | +6.3 ± 1.4 | 24 | 13.1 | 13% of runs | yes |  |
| 1-4 | auto+1 | 2 | 696 | +110 ± 12 | 8.6 | -44.6 ± 3.1 | 2.1 | +2.1 ± 0.7 | 4.2 | +4.2 ± 1.2 | 6 | 13.3 | 20% of runs | yes |  |
| 3-3 | 1 | 2 | 677 | +91 ± 6 | 62.9 | +9.8 ± 0.8 | 19.8 | +19.8 ± 0.6 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 6.5 | no |  |  |
| 3-4 | 1 | 2 | 664 | +77 ± 7 | 62.6 | +9.5 ± 0.9 | 18.9 | +18.9 ± 0.7 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 6.5 | no |  |  |
| 4-8 | auto | 2 | 585 | -2 ± 3 (none detected) | 53.4 | +0.3 ± 1.0 (none detected) | 0.7 | +0.7 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 6.5 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 3-9 | 3 | 0 | 543 | -44 ± 6 | 36.8 | -16.3 ± 1.8 | 5.8 | +5.8 ± 0.4 | 76.8 | +76.8 ± 3.0 | 7 | 9.2 | no | yes |
| 3-10 | 1 | 0 | 546 | -41 ± 6 | 38.2 | -14.9 ± 1.8 | 6.6 | +6.6 ± 0.3 | 74.7 | +74.7 ± 2.9 | 0 | 9.0 | no | yes |
| 3-5 | 0 | 1 | 551 | -35 ± 7 | 38.0 | -15.2 ± 1.8 | 0.5 | +0.5 ± 0.1 | 70.0 | +70.0 ± 3.3 | 71 | 9.1 | no | yes |
| 4-9 | 0 | 0 | 576 | -11 ± 5 | 53.1 | -0.0 ± 1.1 (none detected) | 14.3 | +14.3 ± 0.6 | 16.2 | +16.2 ± 2.7 | 0 | 6.5 | no |  |
| 4-5 | 0 | 0 | 579 | -8 ± 4 | 52.8 | -0.3 ± 0.9 (none detected) | 14.2 | +14.2 ± 0.6 | 18.3 | +18.3 ± 2.8 | 0 | 6.5 | no |  |
| 4-9 | 1 | 1 | 582 | -4 ± 3 | 53.3 | +0.2 ± 1.0 (none detected) | 8.7 | +8.7 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 6.5 | no |  |
| 4-8 | auto | 2 | 585 | -2 ± 3 (none detected) | 53.4 | +0.3 ± 1.0 (none detected) | 0.7 | +0.7 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 6.5 | no | yes |
| 4-8 | 2 | 2 | 585 | -2 ± 3 (none detected) | 53.4 | +0.3 ± 1.0 (none detected) | 0.7 | +0.7 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 82 | 6.5 | no | yes |
| 4 | auto | 2 | 587 | control | 53.1 | control | 0.0 | control | 0.0 | control | 85 | 6.4 | no | yes |
| 2-3 | 0 | 1 | 588 | +1 ± 5 (none detected) | 30.2 | -22.9 ± 1.4 | 4.9 | +4.9 ± 0.4 | 6.4 | +6.4 ± 1.2 | 7 | 11.0 | no | yes |
| 2-4 | auto | 2 | 588 | +2 ± 6 (none detected) | 29.5 | -23.6 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 69 | 10.3 | no | yes |

## 16 slots (18 CPUs), heavy workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 503 | control | 32.6 | control | 6.0 | control | 0.0 | control | 50 | 33.3 | 93% of runs | yes |  |
| 2-4 | auto | 2 | 591 | +88 ± 10 | 28.6 | -4.1 ± 2.8 | 13.9 | +7.8 ± 1.2 | 0.0 | +0.0 ± 0.0 (none detected) | 5 | 43.7 | 100% of runs | yes |  |
| 2-4 | 0 | 2 | 578 | +74 ± 9 | 29.4 | -3.3 ± 2.6 | 12.9 | +6.9 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 8 | 44.6 | 100% of runs | yes |  |
| 2-4 | auto+1 | 2 | 593 | +90 ± 11 | 31.1 | -1.5 ± 2.5 (none detected) | 15.4 | +9.3 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 43.2 | 100% of runs | yes |  |
| 1-4 | auto | 2 | 758 | +255 ± 26 | 44.7 | +12.0 ± 6.9 | 21.0 | +15.0 ± 2.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 41.5 | 100% of runs | yes |  |
| 1-4 | 0 | 2 | 752 | +249 ± 28 | 45.0 | +12.4 ± 6.3 | 20.7 | +14.7 ± 2.8 | 0.0 | +0.0 ± 0.0 (none detected) | 1 | 43.1 | 100% of runs | yes |  |
| 1-4 | auto+1 | 2 | 773 | +269 ± 21 | 51.2 | +18.5 ± 6.1 | 23.1 | +17.1 ± 2.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 45.1 | 100% of runs | yes |  |
| 3-3 | 1 | 2 | 553 | +50 ± 7 | 31.9 | -0.7 ± 2.2 (none detected) | 11.5 | +5.5 ± 0.9 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 36.4 | 100% of runs | yes |  |
| 3-4 | 1 | 2 | 538 | +35 ± 6 | 32.5 | -0.1 ± 2.1 (none detected) | 11.5 | +5.4 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 5 | 37.5 | 100% of runs | yes |  |
| 4-8 | auto | 2 | 499 | -4 ± 6 (none detected) | 33.0 | +0.4 ± 2.3 (none detected) | 6.3 | +0.2 ± 0.9 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 52 | 33.8 | 100% of runs | yes |  |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-11 | 0 | 3 | 526 | +22 ± 8 | 42.3 | +9.6 ± 2.8 | 11.5 | +5.5 ± 1.3 | 0.0 | +0.0 ± 0.0 (none detected) | 3 | 25.4 | no | yes |

## 16 slots (18 CPUs), heavy-declared workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 535 | control | 42.3 | control | 15.8 | control | 0.0 | control | 2 | 25.4 | no | yes |  |
| 2-4 | auto | 2 | 724 | +190 ± 15 | 67.9 | +25.6 ± 4.0 | 38.6 | +22.9 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |  |
| 2-4 | 0 | 2 | 710 | +176 ± 17 | 67.5 | +25.2 ± 3.9 | 37.2 | +21.4 ± 1.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |  |
| 2-4 | auto+1 | 2 | 731 | +196 ± 13 | 66.5 | +24.2 ± 4.2 | 38.5 | +22.7 ± 1.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |  |
| 1-4 | auto | 2 | 1224 | +689 ± 43 | 149.1 | +106.8 ± 7.0 | 58.3 | +42.5 ± 2.0 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.3 | no | yes |  |
| 1-4 | 0 | 2 | 1127 | +592 ± 47 | 145.8 | +103.5 ± 8.2 | 55.5 | +39.7 ± 2.5 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.3 | no | yes |  |
| 1-4 | auto+1 | 2 | 1255 | +720 ± 46 | 147.9 | +105.6 ± 7.9 | 56.7 | +40.9 ± 2.2 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.3 | no | yes |  |
| 3-3 | 1 | 2 | 609 | +74 ± 7 | 49.9 | +7.6 ± 2.2 | 27.0 | +11.2 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |  |
| 3-4 | 1 | 2 | 584 | +50 ± 7 | 48.6 | +6.3 ± 2.1 | 26.4 | +10.6 ± 0.8 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |  |
| 4-8 | auto | 2 | 529 | -6 ± 5 | 42.2 | -0.1 ± 1.6 (none detected) | 15.5 | -0.3 ± 0.3 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 5 | 25.4 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-6 | 0 | 0 | 512 | -23 ± 6 | 38.8 | -3.5 ± 2.1 | 17.0 | +1.3 ± 0.8 | 0.8 | +0.8 ± 0.5 | 1 | 25.4 | no | yes |
| 4-16 | 0 | 0 | 520 | -14 ± 8 | 43.0 | +0.8 ± 1.9 (none detected) | 17.5 | +1.7 ± 0.9 | 0.2 | +0.2 ± 0.3 (none detected) | 0 | 25.4 | no | yes |
| 4-10 | 2 | 1 | 526 | -9 ± 6 | 43.5 | +1.2 ± 1.9 (none detected) | 18.1 | +2.3 ± 0.4 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |
| 4-8 | auto | 2 | 529 | -6 ± 5 | 42.2 | -0.1 ± 1.6 (none detected) | 15.5 | -0.3 ± 0.3 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 5 | 25.4 | no | yes |
| 4-5 | 3 | 1 | 529 | -6 ± 5 | 42.0 | -0.3 ± 1.3 (none detected) | 18.1 | +2.4 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 25.4 | no | yes |
| 4-11 | 0 | 3 | 533 | -2 ± 6 (none detected) | 43.7 | +1.4 ± 1.9 (none detected) | 12.8 | -3.0 ± 0.5 | 0.0 | +0.0 ± 0.0 (none detected) | 3 | 25.4 | no | yes |

## 16 slots (18 CPUs), light workload

Jobs using more than 25 GiB at once leave less than 5 GiB available, where the measured machine went into swap.

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted | non-dominated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4 | auto | 2 | 474 | control | 19.6 | control | 0.0 | control | 0.0 | control | 56 | 18.7 | no | yes |  |
| 2-4 | auto | 2 | 528 | +54 ± 9 | 3.6 | -16.0 ± 2.9 | 0.4 | +0.4 ± 0.4 | 0.0 | +0.0 ± 0.0 (none detected) | 9 | 24.3 | 33% of runs |  |  |
| 2-4 | 0 | 2 | 511 | +37 ± 9 | 4.8 | -14.8 ± 2.0 | 0.2 | +0.2 ± 0.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 15 | 22.8 | 17% of runs | yes |  |
| 2-4 | auto+1 | 2 | 537 | +63 ± 7 | 2.9 | -16.7 ± 2.3 | 0.5 | +0.5 ± 0.4 | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 23.4 | 23% of runs |  |  |
| 1-4 | auto | 2 | 606 | +132 ± 12 | 2.0 | -17.6 ± 2.9 | 0.7 | +0.7 ± 0.8 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.7 | 40% of runs |  |  |
| 1-4 | 0 | 2 | 577 | +104 ± 17 | 1.6 | -18.0 ± 2.4 | 0.5 | +0.5 ± 0.6 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 22.6 | 23% of runs |  |  |
| 1-4 | auto+1 | 2 | 628 | +154 ± 15 | 2.1 | -17.5 ± 2.7 | 0.7 | +0.7 ± 0.8 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 24.1 | 33% of runs |  |  |
| 3-3 | 1 | 2 | 511 | +37 ± 5 | 13.9 | -5.7 ± 1.7 | 1.3 | +1.3 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 0 | 21.6 | 13% of runs |  |  |
| 3-4 | 1 | 2 | 491 | +17 ± 5 | 17.3 | -2.3 ± 1.6 | 2.4 | +2.4 ± 0.3 | 0.0 | +0.0 ± 0.0 (none detected) | 6 | 21.0 | 10% of runs |  |  |
| 4-8 | auto | 2 | 462 | -12 ± 5 | 19.2 | -0.4 ± 1.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 58 | 19.1 | no | yes | yes |

Non-dominated (named and screened):

| weights | headroom | kept | makespan s | vs control | wait p95 s | vs control | idle while waiting % | vs control | at most 1 CPU idle % | vs control | all slots held % | memory used peak GiB | over budget | untrusted |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 4-6 | 0 | 0 | 456 | -18 ± 6 | 20.1 | +0.5 ± 1.2 (none detected) | 3.9 | +3.9 ± 0.3 | 6.5 | +6.5 ± 1.2 | 7 | 18.3 | no |  |
| 4-16 | 0 | 0 | 460 | -14 ± 6 | 22.2 | +2.6 ± 1.5 | 5.3 | +5.3 ± 0.4 | 0.4 | +0.4 ± 0.3 | 0 | 18.7 | no |  |
| 4-10 | 2 | 1 | 462 | -12 ± 6 | 21.0 | +1.4 ± 1.7 (none detected) | 3.9 | +3.9 ± 0.2 | 0.0 | +0.0 ± 0.0 (none detected) | 2 | 18.5 | no |  |
| 4-8 | auto | 2 | 462 | -12 ± 5 | 19.2 | -0.4 ± 1.4 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 0.0 | +0.0 ± 0.0 (none detected) | 58 | 19.1 | no | yes |
