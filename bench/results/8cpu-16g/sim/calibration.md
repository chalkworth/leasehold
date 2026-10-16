| run | condition | fitted | makespan s (measured / sim) | error | job mean s (measured / sim) | wait mean s (measured / sim) | swap-in MiB |
|---|---|---|---|---|---|---|---|
| k4-d240 | lh-advisory | yes | 361 / 364 | +1% | 20.5 / 20.1 | 8.5 / 8.0 | 42 |
| k4-d240 | lh-advisory-greedy | yes | 301 / 312 | +4% | 13.0 / 14.4 | 4.7 / 5.4 | 2 |
| k4-d240 | lh-enforced | yes | 358 / 364 | +2% | 19.2 / 20.1 | 7.6 / 8.0 | 24 |
| k4-d240 | lh-enforced-greedy | yes | 304 / 310 | +2% | 13.3 / 14.2 | 4.7 / 5.3 | 13 |
| k4-d240 | lh-hardcpu-greedy | yes | 352 / 360 | +2% | 19.0 / 19.7 | 8.2 / 7.9 | 0 |
| k4-d240 | static-jobs | no | 318 / 325 | +2% | 16.8 / 17.7 | 0.1 / 0.1 | 47 |
| k4-d240 | unmanaged | no | 308 / 310 | +0% | 14.9 / 15.3 | 0.1 / 0.1 | 3 |
| k8-d240 | lh-advisory | yes | 848 / 831 | -2% | 64.4 / 62.3 | 50.1 / 48.4 | 219 |
| k8-d240 | lh-enforced | yes | 840 / 787 | -6% | 62.7 / 56.9 | 48.7 / 43.9 | 97 |
| k8-d240 | lh-enforced-greedy | yes | 660 / 632 | -4% | 40.1 / 39.4 | 29.9 / 29.4 | 107 |
| k8-d240 | lh-hardcpu-greedy | yes | 789 / 799 | +1% | 56.8 / 58.3 | 43.8 / 45.0 | 61 |
| k8-d240 | static-jobs | no | 579 / 573 | -1% | 42.3 / 38.0 | 0.2 / 0.1 | 10015 |
| k8-d240 | unmanaged | no | 566 / 549 | -3% | 37.8 / 34.1 | 0.2 / 0.1 | 2822 |
| k8-full-d240-two-at-once | lh-enforced | yes | 813 / 577 | -29% (over 15%) | 62.9 / 34.5 | 39.6 / 17.7 | 3370 |
| k8-full-d240-two-at-once | lh-enforced-greedy | yes | 550 / 509 | -7% | 30.9 / 24.8 | 15.2 / 11.0 | 162 |
| k8-full-d240-two-at-once | lh-enforced-v020 | yes | 714 / 706 | -1% | 48.8 / 48.0 | 37.3 / 36.5 | 14 |
| k8-full-d240-two-at-once | unmanaged | no | 649 / 462 | -29% (over 15%) | 48.9 / 24.7 | 0.3 / 0.1 | 2766 |
| k8-full-d240-two-at-once-budget7g | lh-enforced | yes | 638 / 663 | +4% | 39.3 / 42.8 | 20.7 / 23.4 | 25 |
| k8-full-d240-two-at-once-lh-enforced-greedy-rerun | lh-enforced-greedy | yes | 604 / 586 | -3% | 36.8 / 33.8 | 19.8 / 17.2 | 10 |
| k8-full-d240-v030-rounds | lh-enforced-r1 | yes | 834 / 837 | +0% | 62.2 / 63.0 | 48.3 / 48.9 | 223 |
| k8-full-d240-v030-rounds | lh-enforced-r2 | yes | 892 / 838 | -6% | 69.6 / 63.1 | 54.6 / 49.0 | 284 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r1 | yes | 841 / 858 | +2% | 63.4 / 65.6 | 49.4 / 51.1 | 532 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r2 | yes | 842 / 857 | +2% | 63.3 / 65.5 | 49.2 / 51.0 | 19 |
| k8-full-d240-v030-rounds | unmanaged-r1 | no | 585 / 563 | -4% | 40.2 / 36.3 | 0.1 / 0.1 | 3829 |
| k8-full-d240-v030-rounds | unmanaged-r2 | no | 729 / 563 | -23% (over 15%) | 57.9 / 36.3 | 0.2 / 0.1 | 1681 |
| k8-memheavy-d240-two-at-once | lh-enforced | yes | 754 / 739 | -2% | 53.5 / 53.2 | 30.0 / 29.4 | 3016 |
| k8-memheavy-d240-two-at-once | lh-enforced-declared | yes | 697 / 745 | +7% | 48.3 / 53.7 | 26.7 / 30.3 | 43 |
| k8-memheavy-d240-two-at-once | unmanaged | no | 579 / 553 | -5% | 44.6 / 34.9 | 0.2 / 0.1 | 3726 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r1 | yes | 716 / 670 | -6% | 49.0 / 43.1 | 37.7 / 32.6 | 150 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r2 | yes | 693 / 643 | -7% | 46.1 / 40.2 | 35.2 / 30.2 | 93 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r1 | yes | 701 / 682 | -3% | 47.4 / 44.7 | 36.3 / 33.9 | 286 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r2 | yes | 707 / 685 | -3% | 48.2 / 45.2 | 37.0 / 34.4 | 368 |
| k8-memheavy-d240-v030-rounds | unmanaged-r1 | no | 584 / 443 | -24% (over 15%) | 39.5 / 22.2 | 0.4 / 0.1 | 9301 |
| k8-memheavy-d240-v030-rounds | unmanaged-r2 | no | 527 / 435 | -17% (over 15%) | 31.1 / 21.4 | 0.2 / 0.1 | 6580 |
