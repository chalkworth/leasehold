| run | condition | fitted | makespan s (measured / sim) | error | job mean s (measured / sim) | wait mean s (measured / sim) | swap-in MiB |
|---|---|---|---|---|---|---|---|
| k4-d240 | lh-advisory | yes | 361 / 367 | +2% | 20.5 / 20.4 | 8.5 / 8.1 | 42 |
| k4-d240 | lh-advisory-greedy | yes | 301 / 305 | +1% | 13.0 / 13.4 | 4.7 / 4.9 | 2 |
| k4-d240 | lh-enforced | yes | 358 / 367 | +2% | 19.2 / 20.3 | 7.6 / 8.1 | 24 |
| k4-d240 | lh-enforced-greedy | yes | 304 / 305 | +0% | 13.3 / 13.4 | 4.7 / 4.9 | 13 |
| k4-d240 | lh-hardcpu-greedy | yes | 352 / 354 | +1% | 19.0 / 19.1 | 8.2 / 7.9 | 0 |
| k4-d240 | static-jobs | no | 318 / 312 | -2% | 16.8 / 15.4 | 0.1 / 0.1 | 47 |
| k4-d240 | unmanaged | no | 308 / 297 | -4% | 14.9 / 13.0 | 0.1 / 0.1 | 3 |
| k8-d240 | lh-advisory | yes | 848 / 844 | -1% | 64.4 / 63.9 | 50.1 / 49.7 | 219 |
| k8-d240 | lh-enforced | yes | 840 / 821 | -2% | 62.7 / 61.0 | 48.7 / 47.2 | 97 |
| k8-d240 | lh-enforced-greedy | yes | 660 / 626 | -5% | 40.1 / 38.7 | 29.9 / 28.8 | 107 |
| k8-d240 | lh-hardcpu-greedy | yes | 789 / 777 | -2% | 56.8 / 55.8 | 43.8 / 42.9 | 61 |
| k8-d240 | static-jobs | no | 579 / 538 | -7% | 42.3 / 33.3 | 0.2 / 0.1 | 10015 |
| k8-d240 | unmanaged | no | 566 / 514 | -9% | 37.8 / 29.8 | 0.2 / 0.1 | 2822 |
| k8-full-d240-two-at-once | lh-enforced | held out | 813 / 555 | -32% (over 15%) | 62.9 / 31.0 | 39.6 / 15.1 | 3370 |
| k8-full-d240-two-at-once | lh-enforced-greedy | held out | 550 / 488 | -11% | 30.9 / 22.4 | 15.2 / 9.8 | 162 |
| k8-full-d240-two-at-once | lh-enforced-v020 | held out | 714 / 720 | +1% | 48.8 / 49.5 | 37.3 / 37.8 | 14 |
| k8-full-d240-two-at-once | unmanaged | no | 649 / 434 | -33% (over 15%) | 48.9 / 21.4 | 0.3 / 0.1 | 2766 |
| k8-full-d240-two-at-once-budget7g | lh-enforced | held out | 638 / 655 | +3% | 39.3 / 41.9 | 20.7 / 22.7 | 25 |
| k8-full-d240-two-at-once-lh-enforced-greedy-rerun | lh-enforced-greedy | held out | 604 / 556 | -8% | 36.8 / 29.6 | 19.8 / 14.2 | 10 |
| k8-full-d240-v030-rounds | lh-enforced-r1 | held out | 834 / 872 | +5% | 62.2 / 67.2 | 48.3 / 52.4 | 223 |
| k8-full-d240-v030-rounds | lh-enforced-r2 | held out | 892 / 872 | -2% | 69.6 / 67.2 | 54.6 / 52.4 | 284 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r1 | held out | 841 / 883 | +5% | 63.4 / 68.5 | 49.4 / 53.5 | 532 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r2 | held out | 842 / 882 | +5% | 63.3 / 68.4 | 49.2 / 53.4 | 19 |
| k8-full-d240-v030-rounds | unmanaged-r1 | no | 585 / 534 | -9% | 40.2 / 32.1 | 0.1 / 0.1 | 3829 |
| k8-full-d240-v030-rounds | unmanaged-r2 | no | 729 / 534 | -27% (over 15%) | 57.9 / 32.1 | 0.2 / 0.1 | 1681 |
| k8-memheavy-d240-two-at-once | lh-enforced | held out | 754 / 734 | -3% | 53.5 / 51.9 | 30.0 / 28.6 | 3016 |
| k8-memheavy-d240-two-at-once | lh-enforced-declared | held out | 697 / 739 | +6% | 48.3 / 52.7 | 26.7 / 29.5 | 43 |
| k8-memheavy-d240-two-at-once | unmanaged | no | 579 / 522 | -10% | 44.6 / 30.7 | 0.2 / 0.1 | 3726 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r1 | held out | 716 / 684 | -4% | 49.0 / 45.1 | 37.7 / 34.3 | 150 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r2 | held out | 693 / 672 | -3% | 46.1 / 43.4 | 35.2 / 32.9 | 93 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r1 | held out | 701 / 691 | -1% | 47.4 / 45.8 | 36.3 / 34.9 | 286 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r2 | held out | 707 / 693 | -2% | 48.2 / 46.1 | 37.0 / 35.1 | 368 |
| k8-memheavy-d240-v030-rounds | unmanaged-r1 | no | 584 / 418 | -28% (over 15%) | 39.5 / 19.4 | 0.4 / 0.1 | 9301 |
| k8-memheavy-d240-v030-rounds | unmanaged-r2 | no | 527 / 414 | -21% (over 15%) | 31.1 / 18.7 | 0.2 / 0.1 | 6580 |
