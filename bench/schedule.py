"""The order the benchmark runs its arms in, round by round, and the seed of each round.

A round runs every arm once, on one plan: its seed is `seed_base + round`, the same for every
arm in the round, and a new one each round. The order within a round comes from a Williams
design: with two arms, AB then BA; with n arms, n orders (2n when n is odd) in which every arm
runs in every position equally often and follows every other arm equally often, so that what
one arm leaves behind (a warm page cache, swap still being read back) falls on every other arm
alike. Round r takes order (r - 1) modulo the design's length, so the schedule of a round never
depends on how many rounds there are, and a run of one round (`--round N`) runs exactly what a
whole run would have run in that round.
"""

from __future__ import annotations

from dataclasses import dataclass


def williams(n: int) -> list[list[int]]:
    """The orders of a Williams design for `n` arms, as lists of arm indexes."""
    if n < 1:
        raise ValueError("a schedule needs at least one arm")
    first, lo, hi = [0], 1, n - 1
    while len(first) < n:
        if len(first) % 2:
            first.append(lo)
            lo += 1
        else:
            first.append(hi)
            hi -= 1
    rows = [[(x + i) % n for x in first] for i in range(n)]
    if n % 2:
        rows += [row[::-1] for row in rows]
    return rows


@dataclass(frozen=True)
class Slot:
    """One run of the schedule."""

    round: int
    position: int  # 1-based place in its round
    condition: str
    label: str
    seed: int


def label(slug: str, round_: int, rounds: int) -> str:
    """The run's directory name: the condition's slug, and `-rN` when there is more than one
    round."""
    return slug if rounds == 1 else f"{slug}-r{round_}"


def schedule(
    conditions: list[str],
    rounds: int,
    seed_base: int,
    only: int | None = None,
    slugs: list[str] | None = None,
) -> list[Slot]:
    """Every run of `rounds` rounds of `conditions` (or of round `only` alone), in order.
    `slugs` names each condition's directory (default: the condition itself)."""
    if only is not None and not 1 <= only <= rounds:
        raise ValueError(f"round {only} is not one of rounds 1 to {rounds}")
    slugs = slugs or conditions
    orders = williams(len(conditions))
    out = []
    for r in range(1, rounds + 1):
        if only is not None and r != only:
            continue
        for pos, i in enumerate(orders[(r - 1) % len(orders)], 1):
            out.append(Slot(r, pos, conditions[i], label(slugs[i], r, rounds), seed_base + r))
    return out


def runs(
    conditions: list[str], rounds: int, seed_base: int = 0, only: int | None = None
) -> list[tuple[str, str, int]]:
    """Each run as (condition, label, seed)."""
    return [(s.condition, s.label, s.seed) for s in schedule(conditions, rounds, seed_base, only)]
