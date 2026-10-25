"""What a command is expected to use for each slot of weight, learned from what its past runs
used (`leasehold.outcomes`).

`learn` takes the outcomes kept for one user, rule and repository, newest first, and gives the
learned figure a slot, or none while too few runs are known:

* A run's size is the most anonymous memory its scope was seen to use (`anon_bytes`): page cache
  can be reclaimed, so it says little about running out of memory, and `memory.peak` counts it.
  `anon_bytes` is read about once a second, so it can miss a short spike: a run shorter than
  `SAMPLED_S`, or one with no `anon_bytes`, gives its `peak_bytes` instead, the upper bound.
  Neither counts memory in swap, so the most swap the scope used (`swap_bytes`) is added: a run
  that spilled to swap, or that the memory net parked, is sized by all it held. An outcome that
  reports no swap (from an older client) adds none.
* A run's size a slot is its size over the weight its lease held when it ended. A command given
  more slots runs more jobs, so what it uses is counted as growing with its weight.
* A clean run exited 0 and was not reached by the OOM killer. A run that failed, or that a
  signal ended, may have stopped before it used what a whole run uses, so it would pull the
  figure down; it is left out.
* The figure is `MARGIN` times the larger of two things: the weighted median of the clean runs'
  sizes a slot, each run weighing half as much as the one `HALF_LIFE_RUNS` newer, and the
  smaller of the newest two clean runs'. So two runs in a row above the median raise the figure
  at once, and it comes down only as smaller runs take over the median.
* A run the OOM killer reached keeps the figure at least `KILLED_MARGIN` times its size a slot,
  and `KILLED_PAD` a slot above it, for as long as its outcome is kept.
* The figure is never below `FLOOR_BYTES` a slot, and there is none until `TRUSTED_AFTER` clean
  runs are known.
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
#: The least a figure can be, a slot: 64 MiB.
FLOOR_BYTES = 64 << 20
#: A run shorter than this, in seconds, is sized by its `peak_bytes`.
SAMPLED_S = 10.0
#: A run the OOM killer reached keeps the figure at least this many times its size a slot...
KILLED_MARGIN = 1.2
#: ...and at least this far above it, a slot: 256 MiB.
KILLED_PAD = 256 << 20


@dataclass(frozen=True)
class Learned:
    """A learned figure: `per_slot` bytes for each slot of weight (None: too few clean runs are
    known yet) and the clean `runs` it rests on."""

    per_slot: int | None
    runs: int


def size(run: Mapping) -> int | None:
    """One outcome's size in bytes, as the module docstring says; None when it reports none."""
    anon, peak = run.get("anon_bytes"), run.get("peak_bytes")
    if anon is not None and (run.get("wall_s") or 0) >= SAMPLED_S:
        used = anon
    else:
        used = peak if peak is not None else anon
    return None if used is None else used + (run.get("swap_bytes") or 0)


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
        per_slot = math.ceil(used / max(1, run.get("weight") or 1))
        if run.get("oom_kills"):
            pinned = max(pinned, math.ceil(per_slot * KILLED_MARGIN), per_slot + KILLED_PAD)
        elif clean(run):
            sizes.append(per_slot)
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
