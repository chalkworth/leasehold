| run | condition | fitted | swap-in MiB | makespan s measured | sim, no swap term | error | sim, swap term | error | wait mean s (measured / sim, no swap term) |
|---|---|---|---|---|---|---|---|---|---|
| k4-d240 | lh-advisory | yes | 42 | 361 | 365 | +1% | 365 | +1% | 8.5 / 8.0 |
| k4-d240 | lh-advisory-greedy | yes | 2 | 301 | 306 | +2% | 306 | +2% | 4.7 / 4.9 |
| k4-d240 | lh-enforced | yes | 24 | 358 | 365 | +2% | 365 | +2% | 7.6 / 8.0 |
| k4-d240 | lh-enforced-greedy | yes | 13 | 304 | 306 | +1% | 306 | +1% | 4.7 / 4.9 |
| k4-d240 | lh-hardcpu-greedy | yes | 0 | 352 | 354 | +1% | 354 | +1% | 8.2 / 8.2 |
| k4-d240 | static-jobs | no | 47 | 318 | 304 | -4% | 304 | -4% | 0.1 / 0.1 |
| k4-d240 | unmanaged | no | 3 | 308 | 293 | -5% | 293 | -5% | 0.1 / 0.1 |
| k8-d240 | lh-advisory | yes | 219 | 848 | 833 | -2% | 833 | -2% | 50.1 / 48.7 |
| k8-d240 | lh-enforced | yes | 97 | 840 | 833 | -1% | 833 | -1% | 48.7 / 48.7 |
| k8-d240 | lh-enforced-greedy | yes | 107 | 660 | 632 | -4% | 632 | -4% | 29.9 / 29.4 |
| k8-d240 | lh-hardcpu-greedy | yes | 61 | 789 | 777 | -2% | 777 | -2% | 43.8 / 42.7 |
| k8-d240 | static-jobs | no | 10015 | 579 | 509 | -12% | 711 | +23% (over 15%) | 0.2 / 0.1 |
| k8-d240 | unmanaged | no | 2822 | 566 | 484 | -14% | 814 | +44% (over 15%) | 0.2 / 0.1 |
| k8-full-d240-two-at-once | lh-enforced | held out | 3370 | 813 | 541 | -33% (over 15%) | 541 | -33% (over 15%) | 39.6 / 12.7 |
| k8-full-d240-two-at-once | lh-enforced-greedy | held out | 162 | 550 | 476 | -13% | 476 | -13% | 15.2 / 8.4 |
| k8-full-d240-two-at-once | lh-enforced-v020 | held out | 14 | 714 | 710 | -1% | 710 | -1% | 37.3 / 36.9 |
| k8-full-d240-two-at-once | unmanaged | no | 2766 | 649 | 421 | -35% (over 15%) | 444 | -32% (over 15%) | 0.3 / 0.1 |
| k8-full-d240-two-at-once-budget7g | lh-enforced | held out | 25 | 638 | 642 | +1% | 642 | +1% | 20.7 / 21.2 |
| k8-full-d240-two-at-once-lh-enforced-greedy-rerun | lh-enforced-greedy | held out | 10 | 604 | 534 | -12% | 534 | -12% | 19.8 / 12.2 |
| k8-full-d240-v030-rounds | lh-enforced-r1 | held out | 223 | 834 | 872 | +5% | 872 | +5% | 48.3 / 52.4 |
| k8-full-d240-v030-rounds | lh-enforced-r2 | held out | 284 | 892 | 872 | -2% | 872 | -2% | 54.6 / 52.4 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r1 | held out | 532 | 841 | 872 | +4% | 872 | +4% | 49.4 / 52.4 |
| k8-full-d240-v030-rounds | lh-enforced-v020-r2 | held out | 19 | 842 | 872 | +4% | 872 | +4% | 49.2 / 52.4 |
| k8-full-d240-v030-rounds | unmanaged-r1 | no | 3829 | 585 | 508 | -13% | 633 | +8% | 0.1 / 0.1 |
| k8-full-d240-v030-rounds | unmanaged-r2 | no | 1681 | 729 | 508 | -30% (over 15%) | 633 | -13% | 0.2 / 0.1 |
| k8-memheavy-d240-two-at-once | lh-enforced | held out | 3016 | 754 | 698 | -7% | 760 | +1% | 30.0 / 24.1 |
| k8-memheavy-d240-two-at-once | lh-enforced-declared | held out | 43 | 697 | 717 | +3% | 717 | +3% | 26.7 / 27.2 |
| k8-memheavy-d240-two-at-once | unmanaged | no | 3726 | 579 | 492 | -15% (over 15%) | 816 | +41% (over 15%) | 0.2 / 0.1 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r1 | held out | 150 | 716 | 683 | -5% | 683 | -5% | 37.7 / 34.2 |
| k8-memheavy-d240-v030-rounds | lh-enforced-declared-r2 | held out | 93 | 693 | 683 | -1% | 683 | -1% | 35.2 / 34.2 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r1 | held out | 286 | 701 | 683 | -3% | 683 | -3% | 36.3 / 34.2 |
| k8-memheavy-d240-v030-rounds | lh-enforced-r2 | held out | 368 | 707 | 683 | -3% | 683 | -3% | 37.0 / 34.2 |
| k8-memheavy-d240-v030-rounds | unmanaged-r1 | no | 9301 | 584 | 407 | -30% (over 15%) | 478 | -18% (over 15%) | 0.4 / 0.1 |
| k8-memheavy-d240-v030-rounds | unmanaged-r2 | no | 6580 | 527 | 402 | -24% (over 15%) | 454 | -14% | 0.2 / 0.1 |

| | 0.2 config (weight 4) s | two at once s | two at once against 0.2 |
|---|---|---|---|
| measured | 714 | 813 | +14% |
| sim, no swap term | 710 | 541 | -24% |
| sim, swap term | 710 | 541 | -24% |
