| run | condition | fitted | swap-in MiB | makespan s measured | sim, no swap term | error | sim, swap term | error | wait mean s (measured / sim, no swap term) |
|---|---|---|---|---|---|---|---|---|---|
| k4-d240 | lh-advisory | yes | 42 | 361 | 367 | +2% | 367 | +2% | 8.5 / 8.3 |
| k4-d240 | lh-advisory-greedy | yes | 2 | 301 | 315 | +5% | 315 | +5% | 4.7 / 5.7 |
| k4-d240 | lh-enforced | yes | 24 | 358 | 367 | +3% | 367 | +3% | 7.6 / 8.3 |
| k4-d240 | lh-enforced-greedy | yes | 13 | 304 | 312 | +3% | 312 | +3% | 4.7 / 5.5 |
| k4-d240 | lh-hardcpu-greedy | yes | 0 | 352 | 358 | +2% | 358 | +2% | 8.2 / 7.9 |
| k4-d240 | static-jobs | no | 47 | 318 | 324 | +2% | 324 | +2% | 0.1 / 0.1 |
| k4-d240 | unmanaged | no | 3 | 308 | 309 | +0% | 309 | +0% | 0.1 / 0.1 |
| k8-d240 | lh-advisory | yes | 219 | 848 | 840 | -1% | 840 | -1% | 50.1 / 49.3 |
| k8-d240 | lh-enforced | yes | 97 | 840 | 792 | -6% | 792 | -6% | 48.7 / 44.3 |
| k8-d240 | lh-enforced-greedy | yes | 107 | 660 | 637 | -3% | 637 | -3% | 29.9 / 29.9 |
| k8-d240 | lh-hardcpu-greedy | yes | 61 | 789 | 788 | -0% | 788 | -0% | 43.8 / 43.9 |
| k8-d240 | static-jobs | no | 10015 | 579 | 575 | -1% | 946 | +63% (over 15%) | 0.2 / 0.1 |
| k8-d240 | unmanaged | no | 2822 | 566 | 550 | -3% | 1068 | +89% (over 15%) | 0.2 / 0.1 |
| k8-full-d240-two-at-once | lh-enforced | yes | 3370 | 813 | 581 | -29% (over 15%) | 581 | -29% (over 15%) | 39.6 / 17.9 |
| k8-full-d240-two-at-once | lh-enforced-greedy | yes | 162 | 550 | 509 | -7% | 509 | -7% | 15.2 / 11.0 |
| k8-full-d240-two-at-once | lh-enforced-v020 | yes | 14 | 714 | 714 | -0% | 714 | -0% | 37.3 / 37.2 |
| k8-full-d240-two-at-once | unmanaged | no | 2766 | 649 | 464 | -29% (over 15%) | 533 | -18% (over 15%) | 0.3 / 0.1 |
| k8-full-d240-two-at-once-budget7g | lh-enforced | yes | 25 | 638 | 663 | +4% | 663 | +4% | 20.7 / 23.6 |
| k8-full-d240-two-at-once-lh-enforced-greedy-rerun | lh-enforced-greedy | yes | 10 | 604 | 584 | -3% | 584 | -3% | 19.8 / 17.0 |
| k8-full-d240-v030-rounds | lh-enforced-r1 | yes | 223 | 834 | 845 | +1% | 845 | +1% | 48.3 / 49.8 |
| k8-full-d240-v030-rounds | lh-enforced-r2 | yes | 284 | 892 | 846 | -5% | 846 | -5% | 54.6 / 49.8 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r1 | yes | 532 | 841 | 868 | +3% | 868 | +3% | 49.4 / 52.0 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r2 | yes | 19 | 842 | 867 | +3% | 867 | +3% | 49.2 / 51.9 |
| k8-full-d240-v030-rounds | unmanaged-r1 | no | 3829 | 585 | 563 | -4% | 771 | +32% (over 15%) | 0.1 / 0.1 |
| k8-full-d240-v030-rounds | unmanaged-r2 | no | 1681 | 729 | 563 | -23% (over 15%) | 771 | +6% | 0.2 / 0.1 |
| k8-memheavy-d240-two-at-once | lh-enforced | yes | 3016 | 754 | 739 | -2% | 813 | +8% | 30.0 / 29.0 |
| k8-memheavy-d240-two-at-once | lh-enforced-declared | yes | 43 | 697 | 751 | +8% | 751 | +8% | 26.7 / 30.9 |
| k8-memheavy-d240-two-at-once | unmanaged | no | 3726 | 579 | 553 | -4% | 1000 | +73% (over 15%) | 0.2 / 0.1 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r1 | yes | 150 | 716 | 678 | -5% | 678 | -5% | 37.7 / 33.2 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r2 | yes | 93 | 693 | 647 | -7% | 647 | -7% | 35.2 / 30.6 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r1 | yes | 286 | 701 | 688 | -2% | 688 | -2% | 36.3 / 34.7 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r2 | yes | 368 | 707 | 693 | -2% | 693 | -2% | 37.0 / 35.2 |
| k8-memheavy-d240-v030-rounds | unmanaged-r1 | no | 9301 | 584 | 444 | -24% (over 15%) | 561 | -4% | 0.4 / 0.1 |
| k8-memheavy-d240-v030-rounds | unmanaged-r2 | no | 6580 | 527 | 437 | -17% (over 15%) | 550 | +4% | 0.2 / 0.1 |

| | 0.2 config (weight 4) s | two at once s | two at once against 0.2 |
|---|---|---|---|
| measured | 714 | 813 | +14% |
| sim, no swap term | 714 | 581 | -19% |
| sim, swap term | 714 | 581 | -19% |
