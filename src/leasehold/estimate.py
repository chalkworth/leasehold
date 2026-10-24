"""What a command is expected to use, learned from what its past runs used (`leasehold.outcomes`).

`learn` takes the outcomes kept for one user, rule and repository, newest first, and gives the
learned figure, or none while too few runs are known:

* A run's size is the most anonymous memory its scope was seen to use (`anon_bytes`): page cache
  can be reclaimed, so it says little about running out of memory, and `memory.peak` counts it.
  `anon_bytes` is read about once a second, so it can miss a short spike: a run shorter than
  `SAMPLED_S`, or one with no `anon_bytes`, gives its `peak_bytes` instead, the upper bound.
* A clean run exited 0 and was not reached by the OOM killer. A run that failed, or that a
  signal ended, may have stopped before it used what a whole run uses, so it would pull the
  figure down; it is left out.
* The figure is `MARGIN` times the larger of two things: the weighted median of the clean runs'
  sizes, each run weighing half as much as the one `HALF_LIFE_RUNS` newer, and the smaller of
  the newest two clean runs. So two runs in a row above the median raise the figure at once,
  and it comes down only as smaller runs take over the median.
* A run the OOM killer reached keeps the figure at least `KILLED_MARGIN` times its size, and
  `KILLED_PAD` above it, for as long as its outcome is kept.
* The figure is never below `FLOOR_BYTES`, and there is none until `TRUSTED_AFTER` clean runs
  are known.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: What the median is multiplied by.
MARGIN = 1.15
#: How many clean runs a figure needs.
TRUSTED_AFTER = 3
#: How many runs newer a run must be to weigh twice as much in the median.
HALF_LIFE_RUNS = 10
#: The least a figure can be: 256 MiB.
FLOOR_BYTES = 256 << 20
#: A run shorter than this, in seconds, is sized by its `peak_bytes`.
SAMPLED_S = 10.0
#: A run the OOM killer reached keeps the figure at least this many times its size...
KILLED_MARGIN = 1.2
#: ...and at least this far above it: 256 MiB.
KILLED_PAD = 256 << 20


@dataclass(frozen=True)
class Learned:
    """A learned figure: `bytes` (None: too few clean runs are known yet) and the clean `runs`
    it rests on."""

    bytes: int | None
    runs: int


def size(run: Mapping) -> int | None:
    """One outcome's size in bytes, as the module docstring says; None when it reports none."""
    anon, peak = run.get("anon_bytes"), run.get("peak_bytes")
    if anon is not None and (run.get("wall_s") or 0) >= SAMPLED_S:
        return anon
    return peak if peak is not None else anon


def clean(run: Mapping) -> bool:
    """Whether an outcome exited 0 and the OOM killer did not reach it."""
    return run.get("exit_status") == 0 and not run.get("oom_kills")


def learn(runs: Iterable[Mapping]) -> Learned:
    """The figure the outcomes `runs` (one user's, rule's and repository's, newest first) give."""
    sizes, pinned = [], 0
    for run in runs:
        used = size(run)
        if used is None:
            continue
        if run.get("oom_kills"):
            pinned = max(pinned, math.ceil(used * KILLED_MARGIN), used + KILLED_PAD)
        elif clean(run):
            sizes.append(used)
    if len(sizes) < TRUSTED_AFTER:
        return Learned(None, len(sizes))
    base = max(_weighted_median(sizes), min(sizes[:2]))
    return Learned(max(math.ceil(base * MARGIN), pinned, FLOOR_BYTES), len(sizes))


def _weighted_median(sizes: list[int]) -> int:
    """The weighted median of `sizes` (newest first), weighted as the module docstring says."""
    weighted = sorted((s, 0.5 ** (i / HALF_LIFE_RUNS)) for i, s in enumerate(sizes))
    half = sum(w for _, w in weighted) / 2
    acc = 0.0
    for value, weight in weighted:
        acc += weight
        if acc >= half:
            return value
    return weighted[-1][0]
