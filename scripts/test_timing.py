"""Write a test run's timing to the GitHub step summary: the total time, the test count and the
slowest tests of a JUnit XML report, and, on a pull request, how they changed against the base
branch.

    python scripts/test_timing.py junit.xml --title "Python 3.12" --compare junit-3.12

The summary is appended to the file `$GITHUB_STEP_SUMMARY` names, or printed without it.

With `--compare ARTIFACT` on a pull request (`GITHUB_BASE_REF` is set), the script downloads
the artifact of that name from the latest successful push run of the same workflow on the base
branch, with the `gh` CLI and `GH_TOKEN`, and reads the JUnit report in it. When the total time
grew by more than `GROWTH_RATIO` or by more than `GROWTH_S` seconds, a `::warning::` line says
so. When there is no such run or artifact, the summary says so. Timing never fails the job:
the script exits 1 only when the report of this run cannot be read.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

#: How many of the slowest tests the summary lists.
SLOWEST = 10
#: A total time more than this fraction above the base's is a warning.
GROWTH_RATIO = 0.5
#: A total time more than this many seconds above the base's is a warning.
GROWTH_S = 30.0


@dataclass(frozen=True)
class Run:
    """One JUnit report: its total time, the tests that ran with their times, and how many
    were skipped."""

    total_s: float
    tests: list[tuple[str, float]]
    skipped: int


def read(report: Path) -> Run:
    """The run in `report`. Raises `ValueError` when it cannot be read or parsed."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"cannot read the JUnit report {report}: {exc}") from None
    tests: list[tuple[str, float]] = []
    skipped = 0
    for case in root.iter("testcase"):
        if any(child.tag == "skipped" for child in case):
            skipped += 1
            continue
        name = "::".join(p for p in (case.get("classname"), case.get("name")) if p)
        tests.append((name, _seconds(case.get("time"))))
    suites = [s for s in root.iter("testsuite") if s.find("testsuite") is None]
    total = sum(_seconds(s.get("time")) for s in suites) or sum(t for _, t in tests)
    return Run(total, tests, skipped)


def _seconds(text: str | None) -> float:
    try:
        return float(text or 0)
    except ValueError:
        return 0.0


def base_report(artifact: str, into: Path) -> tuple[Path | None, str]:
    """The JUnit report in `artifact` of the base branch's latest successful push run of this
    workflow, downloaded below `into`, and the base branch's name. The path is None, with the
    reason in place of the name, when there is no such run, artifact or report."""
    base = os.environ["GITHUB_BASE_REF"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    workflow = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].rsplit("/", 1)[-1]
    listed = _gh(
        "run", "list", "--repo", repo, "--workflow", workflow, "--branch", base,
        "--event", "push", "--status", "success", "--limit", "1",
        "--json", "databaseId", "--jq", ".[0].databaseId // empty",
    )  # fmt: skip
    if listed is None or not listed.strip():
        return None, f"no successful `{workflow}` run on `{base}` to compare against"
    run_id = listed.strip()
    got = _gh("run", "download", run_id, "--repo", repo, "--name", artifact, "--dir", str(into))
    reports = sorted(into.rglob("*.xml")) if got is not None else []
    if not reports:
        return None, f"no `{artifact}` artifact in `{base}`'s run {run_id} to compare against"
    return reports[0], base


def _gh(*args: str) -> str | None:
    """`gh`'s output, or None when it fails (its error is printed as a notice)."""
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"::notice::gh could not start: {exc}")
        return None
    if done.returncode != 0:
        print(f"::notice::gh {args[0]} {args[1]}: {done.stderr.strip()}")
        return None
    return done.stdout


def summary(run: Run, title: str, base: Run | None, against: str | None) -> tuple[str, str | None]:
    """The Markdown summary of `run`, and the warning to raise, if any. `against` names the
    base branch when `base` is set, says why there is no base when it is not, and is None when
    no comparison was asked for."""
    lines = [
        f"### Test timing: {title}",
        "",
        f"{len(run.tests)} tests ran in {run.total_s:.1f} s, and {run.skipped} were skipped.",
        "",
    ]
    warning = None
    if base is None and against is not None:
        lines += [f"No comparison: {against}.", ""]
    elif base is not None:
        grew = run.total_s - base.total_s
        ratio = grew / base.total_s if base.total_s else 0.0
        lines += [
            f"Against `{against}`: {base.total_s:.1f} s to {run.total_s:.1f} s "
            f"({grew:+.1f} s, {ratio:+.0%}), and {len(base.tests)} to {len(run.tests)} tests.",
            "",
        ]
        if grew > GROWTH_S or (base.total_s and ratio > GROWTH_RATIO):
            warning = (
                f"{title}: the tests took {run.total_s:.1f} s, {grew:.1f} s ({ratio:.0%}) "
                f"more than on {against}"
            )
    slowest = sorted(run.tests, key=lambda test: -test[1])[:SLOWEST]
    if slowest:
        lines += ["| Slowest tests | Seconds |", "|---|---:|"]
        lines += [f"| `{_cell(name)}` | {seconds:.2f} |" for name, seconds in slowest]
        lines.append("")
    return "\n".join(lines), warning


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("report", type=Path)
    parser.add_argument("--title", default="tests")
    parser.add_argument(
        "--compare",
        metavar="ARTIFACT",
        help="on a pull request, compare with this artifact of the base branch's latest run",
    )
    args = parser.parse_args(argv)
    try:
        run = read(args.report)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1
    base, against = None, None
    if args.compare and os.environ.get("GITHUB_BASE_REF"):
        with tempfile.TemporaryDirectory() as into:
            path, against = base_report(args.compare, Path(into))
            if path is not None:
                try:
                    base = read(path)
                except ValueError as exc:
                    against = str(exc)
    text, warning = summary(run, args.title, base, against)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as out:
            out.write(text + "\n")
    else:
        print(text)
    if warning:
        print(f"::warning title=Test timing::{warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
