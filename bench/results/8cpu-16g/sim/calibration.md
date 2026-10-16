| run | condition | fitted | swap-in MiB | makespan s measured | sim, no swap term | error | sim, swap term | error | wait mean s (measured / sim, no swap term) |
|---|---|---|---|---|---|---|---|---|---|
| k4-d240 | lh-advisory | yes | 42 | 361 | 368 | +2% | 368 | +2% | 8.5 / 8.3 |
| k4-d240 | lh-advisory-greedy | yes | 2 | 301 | 313 | +4% | 313 | +4% | 4.7 / 5.6 |
| k4-d240 | lh-enforced | yes | 24 | 358 | 367 | +3% | 367 | +3% | 7.6 / 8.3 |
| k4-d240 | lh-enforced-greedy | yes | 13 | 304 | 312 | +3% | 312 | +3% | 4.7 / 5.5 |
| k4-d240 | lh-hardcpu-greedy | yes | 0 | 352 | 358 | +2% | 358 | +2% | 8.2 / 7.8 |
| k4-d240 | static-jobs | no | 47 | 318 | 324 | +2% | 324 | +2% | 0.1 / 0.1 |
| k4-d240 | unmanaged | no | 3 | 308 | 309 | +0% | 309 | +0% | 0.1 / 0.1 |
| k8-d240 | lh-advisory | yes | 219 | 848 | 839 | -1% | 839 | -1% | 50.1 / 49.2 |
| k8-d240 | lh-enforced | yes | 97 | 840 | 791 | -6% | 791 | -6% | 48.7 / 44.3 |
| k8-d240 | lh-enforced-greedy | yes | 107 | 660 | 637 | -4% | 637 | -4% | 29.9 / 29.9 |
| k8-d240 | lh-hardcpu-greedy | yes | 61 | 789 | 787 | -0% | 787 | -0% | 43.8 / 43.9 |
| k8-d240 | static-jobs | no | 10015 | 579 | 572 | -1% | 951 | +64% (over 15%) | 0.2 / 0.1 |
| k8-d240 | unmanaged | no | 2822 | 566 | 547 | -3% | 1072 | +89% (over 15%) | 0.2 / 0.1 |
| k8-full-d240-two-at-once | lh-enforced | yes | 3370 | 813 | 580 | -29% (over 15%) | 580 | -29% (over 15%) | 39.6 / 18.0 |
| k8-full-d240-two-at-once | lh-enforced-greedy | yes | 162 | 550 | 508 | -8% | 508 | -8% | 15.2 / 11.1 |
| k8-full-d240-two-at-once | lh-enforced-v020 | yes | 14 | 714 | 713 | -0% | 713 | -0% | 37.3 / 37.1 |
| k8-full-d240-two-at-once | unmanaged | no | 2766 | 649 | 460 | -29% (over 15%) | 530 | -18% (over 15%) | 0.3 / 0.1 |
| k8-full-d240-two-at-once-budget7g | lh-enforced | yes | 25 | 638 | 663 | +4% | 663 | +4% | 20.7 / 23.5 |
| k8-full-d240-two-at-once-lh-enforced-greedy-rerun | lh-enforced-greedy | yes | 10 | 604 | 582 | -4% | 582 | -4% | 19.8 / 17.1 |
| k8-full-d240-v030-rounds | lh-enforced-r1 | yes | 223 | 834 | 844 | +1% | 844 | +1% | 48.3 / 49.7 |
| k8-full-d240-v030-rounds | lh-enforced-r2 | yes | 284 | 892 | 845 | -5% | 845 | -5% | 54.6 / 49.7 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r1 | yes | 532 | 841 | 867 | +3% | 867 | +3% | 49.4 / 51.9 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r2 | yes | 19 | 842 | 866 | +3% | 866 | +3% | 49.2 / 51.8 |
| k8-full-d240-v030-rounds | unmanaged-r1 | no | 3829 | 585 | 559 | -4% | 761 | +30% (over 15%) | 0.1 / 0.1 |
| k8-full-d240-v030-rounds | unmanaged-r2 | no | 1681 | 729 | 559 | -23% (over 15%) | 761 | +4% | 0.2 / 0.1 |
| k8-memheavy-d240-two-at-once | lh-enforced | yes | 3016 | 754 | 736 | -2% | 814 | +8% | 30.0 / 28.7 |
| k8-memheavy-d240-two-at-once | lh-enforced-declared | yes | 43 | 697 | 749 | +7% | 749 | +7% | 26.7 / 30.7 |
| k8-memheavy-d240-two-at-once | unmanaged | no | 3726 | 579 | 550 | -5% | 1008 | +74% (over 15%) | 0.2 / 0.1 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r1 | yes | 150 | 716 | 676 | -6% | 676 | -6% | 37.7 / 33.1 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r2 | yes | 93 | 693 | 646 | -7% | 646 | -7% | 35.2 / 30.5 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r1 | yes | 286 | 701 | 687 | -2% | 687 | -2% | 36.3 / 34.6 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r2 | yes | 368 | 707 | 692 | -2% | 692 | -2% | 37.0 / 35.1 |
| k8-memheavy-d240-v030-rounds | unmanaged-r1 | no | 9301 | 584 | 441 | -25% (over 15%) | 574 | -2% | 0.4 / 0.1 |
| k8-memheavy-d240-v030-rounds | unmanaged-r2 | no | 6580 | 527 | 434 | -18% (over 15%) | 560 | +6% | 0.2 / 0.1 |

| | 0.2 config (weight 4) s | two at once s | two at once against 0.2 |
|---|---|---|---|
| measured | 714 | 813 | +14% |
| sim, no swap term | 713 | 580 | -19% |
| sim, swap term | 713 | 580 | -19% |
