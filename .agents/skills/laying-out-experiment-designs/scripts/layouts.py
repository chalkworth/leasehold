#!/usr/bin/env python3
"""Generate experiment layouts: factorial, screening, response-surface and space-filling matrices,
allocation schedules and Latin squares.

Needs numpy, pandas and pydoe 1.5.0. Run from anywhere with:
    uv run --no-project --with numpy --with pandas --with pydoe==1.5.0 python layouts.py <command> ...

Every command prints the layout as CSV on stdout and a JSON note (runs, resolution, aliases, balance)
on stderr; `--out FILE` writes the CSV to a file instead. Factors are `name=low:high` in real units.

Commands (run `layouts.py <command> --help` for the arguments):
    factorial   2^k full factorial, or a 2^(k-p) fraction from --generator or --resolution
    screen      Plackett-Burman main-effects screening design
    surface     central composite (ccd) or Box-Behnken (bbd) response surface
    sweep       Latin hypercube over continuous ranges
    allocate    permuted-block schedule, optionally stratified, for units or clusters
    square      randomised Latin square

Library-only: order_runs (restricted randomisation), resolution, balance.

Examples:
    layouts.py factorial --factor lr=1e-4:1e-2 --factor depth=2:8 --factor wd=0:0.1 --centers 3
    layouts.py factorial --resolution 4 --factor a=0:1 --factor b=0:1 --factor c=0:1 --factor d=0:1 \\
        --factor e=0:1 --factor f=0:1
    layouts.py surface --kind ccd --face inscribed --center 2,2 --factor lr=1e-4:1e-2 --factor wd=0:0.1
    layouts.py allocate --n 60 --arms new old --ratio 2 1 --block-size 6 --seed 20260101
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

Factors = dict[str, tuple[float, float]]
MAX_ALIAS_WORD = 3  # report aliases up to three-factor interactions


RESERVED_COLUMNS = ("run_order", "block")  # columns the layouts add themselves


def parse_factors(specs: Sequence[str]) -> Factors:
    """Turn `name=low:high` strings into an ordered factor mapping."""
    factors: Factors = {}
    for spec in specs:
        name, _, span = spec.partition("=")
        low, _, high = span.partition(":")
        try:
            pair = (float(low), float(high))
        except ValueError:
            raise ValueError(f"{spec!r}: expected name=low:high") from None
        if not name or name in RESERVED_COLUMNS or name in factors or not pair[0] < pair[1]:
            raise ValueError(
                f"{spec!r}: need a unique name (not run_order or block) and low < high"
            )
        factors[name] = pair
    if not factors:
        raise ValueError("give at least one --factor name=low:high")
    return factors


def _decode(coded: np.ndarray, factors: Factors) -> pd.DataFrame:
    """Map a coded matrix (-1/0/+1, or beyond for axial points) to real units."""
    if coded.shape[1] != len(factors):
        raise ValueError(f"design has {coded.shape[1]} columns but {len(factors)} factors")
    columns = {}
    for j, (name, (low, high)) in enumerate(factors.items()):
        columns[name] = (high + low) / 2 + coded[:, j] * (high - low) / 2
    return pd.DataFrame(columns)


def order_runs(design: pd.DataFrame, seed: int, within: str | None = None) -> pd.DataFrame:
    """Shuffle run order, globally or inside each group of the `within` column.

    With `within`, the groups keep their rows together (in random group order) and rows are shuffled
    only inside a group: use it for blocks, batches and the whole plots of a split-plot.
    """
    rng = np.random.default_rng(seed)
    if within is None:
        shuffled = design.iloc[rng.permutation(len(design))]
    else:
        groups = design[within].drop_duplicates().to_numpy()
        parts = []
        for group in rng.permutation(len(groups)):
            rows = design[design[within] == groups[group]]
            parts.append(rows.iloc[rng.permutation(len(rows))])
        shuffled = pd.concat(parts)
    shuffled = shuffled.reset_index(drop=True)
    shuffled = shuffled.drop(columns=["run_order"], errors="ignore")
    shuffled.insert(0, "run_order", np.arange(1, len(shuffled) + 1))
    return shuffled


def resolution(coded: np.ndarray) -> int | None:
    """Resolution of a two-level design: the size of the smallest set of columns whose product is
    constant. None when no set of columns is confounded (a full factorial)."""
    runs, k = coded.shape
    for size in range(2, k + 1):
        for columns in itertools.combinations(range(k), size):
            if abs(coded[:, columns].prod(axis=1).sum()) == runs:
                return size
    return None


def _aliases(coded: np.ndarray) -> list[str]:
    """Alias groups (`a = bc`) among terms of up to MAX_ALIAS_WORD factors; a, b, c... are the
    factors in order."""
    from pydoe import fracfact_aliasing

    groups, _ = fracfact_aliasing(coded)
    result = []
    for group in groups:
        terms = [term for term in group.split(" = ") if len(term) <= MAX_ALIAS_WORD]
        if len(terms) > 1:
            result.append(" = ".join(terms))
    return result


def factorial(
    factors: Factors,
    *,
    generator: str | None = None,
    resolution_target: int | None = None,
    centers: int = 0,
    fold_over: bool = False,
    seed: int = 0,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Full 2^k factorial, or a fraction from a pydoe generator ("a b c abc") or a target resolution.

    `centers` adds that many all-midpoint runs. `fold_over` appends the mirror image of every run
    (all signs flipped) as a second `block`, which de-aliases main effects from two-factor
    interactions; the rows of each block are shuffled but blocks never mix (block 0 first), and centre
    runs join block 0.
    """
    from pydoe import ff2n, fold, fracfact, fracfact_by_res

    if generator and resolution_target:
        raise ValueError("give a generator or a resolution, not both")
    if generator:
        coded = fracfact(generator)
    elif resolution_target:
        coded = fracfact_by_res(len(factors), resolution_target)
    else:
        coded = ff2n(len(factors))
    block = np.zeros(len(coded), dtype=int)
    if fold_over:
        coded = fold(coded)
        block = np.repeat([0, 1], len(coded) // 2)
    coded = np.vstack([coded, np.zeros((centers, coded.shape[1]))])
    block = np.append(block, np.zeros(centers, dtype=int))
    unordered = _decode(coded, factors)
    if fold_over:
        unordered["block"] = block
    design = order_runs(unordered, seed, within="block" if fold_over else None)
    if fold_over:  # blocks keep their order: run block 0 first, block 1 after
        design = design.sort_values("block", kind="stable").reset_index(drop=True)
        design["run_order"] = np.arange(1, len(design) + 1)
    fraction = len(coded) - centers < 2 ** len(factors)
    # Resolution and aliases describe the factorial rows; centre rows are constant and would hide them.
    factorial_rows = coded[: len(coded) - centers]
    notes: dict[str, object] = {"runs": len(design), "centers": centers}
    if fraction:
        notes["resolution"] = resolution(factorial_rows)
        notes["aliases"] = _aliases(factorial_rows)
        notes["letters"] = dict(zip("abcdefghijklmnopqrstuvwxyz", factors, strict=False))
    return design, notes


def screen(factors: Factors, seed: int = 0) -> tuple[pd.DataFrame, dict[str, object]]:
    """Plackett-Burman main-effects design; pydoe supports 2^a, 12*2^a and 20*2^a runs only."""
    from pydoe import pbdesign

    if len(factors) < 3:
        raise ValueError(f"screen needs at least 3 factors, got {len(factors)}")
    coded = pbdesign(len(factors))
    return order_runs(_decode(coded, factors), seed), {"runs": len(coded), "resolution": 3}


def surface(
    factors: Factors,
    kind: str = "ccd",
    *,
    face: str = "circumscribed",
    center: tuple[int, int] = (2, 2),
    box_behnken_center: int = 3,
    seed: int = 0,
) -> tuple[pd.DataFrame, dict[str, object]]:
    """Central composite (`ccd`) or Box-Behnken (`bbd`) design in real units.

    `center` is the (factorial-block, axial-block) centre-run count of a CCD. The actual minimum and
    maximum of each factor are in the notes: the circumscribed CCD goes beyond the stated range.
    """
    from pydoe import bbdesign, ccdesign

    if kind == "ccd":
        coded = ccdesign(len(factors), center=center, face=face)
    elif kind == "bbd":
        coded = bbdesign(len(factors), center=box_behnken_center)
    else:
        raise ValueError("kind must be ccd or bbd")
    design = order_runs(_decode(coded, factors), seed)
    actual = {n: [float(design[n].min()), float(design[n].max())] for n in factors}
    return design, {"runs": len(design), "actual_range": actual}


def sweep(factors: Factors, n: int, seed: int = 0) -> tuple[pd.DataFrame, dict[str, object]]:
    """Maximin Latin hypercube of `n` points. The seed goes to pydoe, which has its own generator."""
    from pydoe import lhs

    if n < 2:
        raise ValueError("n must be at least 2")
    unit = lhs(len(factors), samples=n, criterion="maximin", seed=seed)
    design = _decode(2 * unit - 1, factors)
    design.insert(0, "run", np.arange(1, n + 1))
    return design, {"runs": n}


def allocate(
    strata: Mapping[str, int] | Sequence[str],
    arms: Sequence[str] = ("treatment", "control"),
    *,
    ratio: Sequence[int] | None = None,
    block_size: int | None = None,
    seed: int = 0,
) -> pd.DataFrame:
    """Permuted-block schedule, independently within each stratum.

    `strata` is {label: count}, or one label per unit: `unit_id` is then the unit's one-based position
    in that sequence, so join the schedule to the roster on it. For an unstratified schedule pass
    {"all": n}. Each complete block holds the arms in the ratio; the last block of a stratum may be
    partial and then need not balance.
    """
    if len(set(arms)) != len(arms) or len(arms) < 2:
        raise ValueError("arms must be at least two distinct labels")
    ratio = list(ratio) if ratio is not None else [1] * len(arms)
    if len(ratio) != len(arms) or any(r < 1 or int(r) != r for r in ratio):
        raise ValueError("ratio needs one positive integer per arm")
    if block_size is not None and block_size <= 0:
        raise ValueError(f"block_size {block_size} must be positive")
    template = [arm for arm, r in zip(arms, ratio, strict=True) for _ in range(int(r))]
    size = block_size or 2 * len(template)
    if size % len(template):
        raise ValueError(f"block_size {size} must be a multiple of sum(ratio) = {len(template)}")
    if isinstance(strata, Mapping):
        items = [(label, count, None) for label, count in strata.items()]
    else:
        positions: dict[str, list[int]] = {}
        for position, label in enumerate(strata, start=1):
            positions.setdefault(label, []).append(position)
        items = [(label, len(ids), ids) for label, ids in positions.items()]
    frames = []
    next_id = 1
    for index, (label, count, ids) in enumerate(items):
        if count < 0:
            raise ValueError(f"stratum {label!r}: count must be >= 0")
        rng = np.random.default_rng([seed, index])
        arm_column: list[str] = []
        while len(arm_column) < count:
            block = template * (size // len(template))
            rng.shuffle(block)
            arm_column += block
        unit_ids = ids if ids is not None else list(range(next_id, next_id + count))
        next_id += count
        frames.append(
            pd.DataFrame(
                {
                    "unit_id": unit_ids,
                    "stratum": label,
                    "block": np.arange(count) // size,
                    "arm": arm_column[:count],
                }
            )
        )
    schedule = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["unit_id", "stratum", "block", "arm"])
    )
    schedule.attrs["block_size"] = size
    return schedule.sort_values("unit_id").reset_index(drop=True)


def balance(schedule: pd.DataFrame, block_size: int) -> dict[str, object]:
    """Arm counts per stratum, and the size of each stratum's incomplete last block (0 when none)."""
    counts = schedule.groupby(["stratum", "arm"]).size().unstack(fill_value=0)
    partial = {str(s): int(len(rows) % block_size) for s, rows in schedule.groupby("stratum")}
    return {"counts": json.loads(counts.to_json(orient="index")), "partial_block_size": partial}


def latin_square(order: int, seed: int = 0) -> pd.DataFrame:
    """Latin square of `order` treatments: rows, columns and symbols of the cyclic square permuted
    at random. Rows are one nuisance factor (day), columns the other (position)."""
    if order < 2:
        raise ValueError("order must be at least 2")
    rng = np.random.default_rng(seed)
    cyclic = (np.arange(order)[:, None] + np.arange(order)[None, :]) % order
    square = rng.permutation(order)[cyclic[rng.permutation(order)][:, rng.permutation(order)]]
    labels = [chr(ord("A") + i) if order <= 26 else str(i) for i in range(order)]
    return pd.DataFrame(
        np.array(labels)[square],
        columns=[f"col{j + 1}" for j in range(order)],
        index=pd.RangeIndex(1, order + 1, name="row"),
    )


def _emit(design: pd.DataFrame, notes: Mapping[str, object], out: str | None, index: bool = False):
    design.to_csv(out or sys.stdout, index=index, float_format="%.10g")
    print(json.dumps(dict(notes)), file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, factors: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name)
        sub.add_argument("--seed", type=int, default=0, help="record it with the layout")
        sub.add_argument("--out", help="write the CSV here instead of stdout")
        if factors:
            sub.add_argument("--factor", action="append", default=[], metavar="NAME=LOW:HIGH")
        return sub

    sub = command("factorial")
    sub.add_argument("--generator", help='pydoe generator, e.g. "a b c abc"')
    sub.add_argument(
        "--resolution", type=int, help="smallest fraction at this resolution or better"
    )
    sub.add_argument("--centers", type=int, default=0)
    sub.add_argument(
        "--fold-over", action="store_true", help="add the mirror-image runs as block 1"
    )
    command("screen")
    sub = command("surface")
    sub.add_argument("--kind", choices=["ccd", "bbd"], default="ccd")
    sub.add_argument(
        "--face", choices=["circumscribed", "inscribed", "faced"], default="circumscribed"
    )
    sub.add_argument("--center", default="2,2", help="CCD centre runs: factorial,axial blocks")
    sub.add_argument("--bbd-center", type=int, default=3)
    sub = command("sweep")
    sub.add_argument("--n", type=int, required=True)
    sub = command("allocate", factors=False)
    sub.add_argument("--n", type=int, help="units, unstratified")
    sub.add_argument(
        "--strata",
        nargs="+",
        metavar="LABEL=COUNT",
        help="one stratum per label, each with its unit count",
    )
    sub.add_argument("--clusters", nargs="+", help="cluster ids: one row per cluster")
    sub.add_argument("--arms", nargs="+", default=["treatment", "control"])
    sub.add_argument("--ratio", nargs="+", type=int)
    sub.add_argument("--block-size", type=int)
    sub = command("square", factors=False)
    sub.add_argument("--order", type=int, required=True)
    return parser


def parse_center(text: str) -> tuple[int, int]:
    """`--center factorial,axial`: exactly two counts of zero or more."""
    parts = text.split(",")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise ValueError(f"--center {text!r}: expected two counts, factorial,axial, such as 2,2")
    return int(parts[0]), int(parts[1])


def _run(args: argparse.Namespace) -> None:
    if args.command == "factorial":
        design, notes = factorial(
            parse_factors(args.factor),
            generator=args.generator,
            resolution_target=args.resolution,
            centers=args.centers,
            fold_over=args.fold_over,
            seed=args.seed,
        )
    elif args.command == "screen":
        design, notes = screen(parse_factors(args.factor), args.seed)
    elif args.command == "surface":
        design, notes = surface(
            parse_factors(args.factor),
            args.kind,
            face=args.face,
            center=parse_center(args.center),
            box_behnken_center=args.bbd_center,
            seed=args.seed,
        )
    elif args.command == "sweep":
        design, notes = sweep(parse_factors(args.factor), args.n, args.seed)
    elif args.command == "square":
        _emit(latin_square(args.order, args.seed), {"order": args.order}, args.out, index=True)
        return
    else:
        if args.clusters:
            if len(set(args.clusters)) != len(args.clusters):
                raise ValueError(
                    "--clusters ids must be unique: one cluster cannot land in two arms"
                )
            strata: Mapping[str, int] = {"all": len(args.clusters)}
        elif args.strata:
            strata = {}
            for spec in args.strata:
                label, _, count = spec.partition("=")
                if label in strata:
                    raise ValueError(f"stratum {label!r} is given more than once")
                strata[label] = int(count)
        elif args.n is not None:
            strata = {"all": args.n}
        else:
            raise ValueError("give --n, --strata or --clusters")
        design = allocate(
            strata, args.arms, ratio=args.ratio, block_size=args.block_size, seed=args.seed
        )
        if args.clusters:
            notes = {"clusters": len(args.clusters), **balance(design, design.attrs["block_size"])}
            design = design.assign(cluster_id=args.clusters).drop(columns=["unit_id", "stratum"])
        else:
            notes = balance(design, design.attrs["block_size"])
    _emit(design, notes, args.out)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _run(_build_parser().parse_args(argv))
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
