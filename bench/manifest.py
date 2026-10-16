"""The manifest of one run (`manifest.json` in its directory): what ran, on what, from which
code, so that a result can be traced and compared without the log.

It holds the git commit of this checkout (and whether it had changes), the leasehold version,
the condition and its parameters, the config the condition's daemon ran on, the round, its seed
and the run's place in it, the kernel release, CPU count, memory, swap, virtualisation, Python
version, the machine class, the washout the run waited for, whether the run is valid, and any
`--note`. It never holds the host's name, the CPU model or a path under the home directory.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git_state(repo: Path = ROOT) -> dict | None:
    """The checkout's commit and whether its tracked files have changes; None outside git."""
    try:
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=30,
        )  # fmt: skip
        if sha.returncode != 0:
            return None
        dirty = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=False, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {"sha": sha.stdout.strip(), "dirty": bool(dirty.stdout.strip())}


def leasehold_version() -> str | None:
    try:
        import leasehold
    except ImportError:
        return None
    return leasehold.__version__


def build(
    *,
    spec: dict,
    slot,
    condition,
    config: dict | None,
    washout: list[dict],
    valid: bool,
    why_invalid: str = "",
) -> dict:
    """The manifest of `slot` (a `schedule.Slot`) of the run `spec` describes."""
    m = spec["machine"]
    return {
        "label": slot.label,
        "round": slot.round,
        "position": slot.position,
        "seed": slot.seed,
        "workload": spec.get("workload"),
        "condition": {
            "text": condition.text,
            "base": condition.base,
            "params": dict(condition.canonical_params()),
        },
        "config": config,
        "valid": valid,
        "why_invalid": why_invalid,
        "washout": washout,
        "git": spec.get("git"),
        "leasehold": spec.get("leasehold_version"),
        "machine": {
            "class": m["class"],
            "cpus": m["cpus"],
            "memory_bytes": m["memory_bytes"],
            "swap_bytes": m["swap_bytes"],
            "kernel": m["kernel"],
            "virt": m["virt"],
            "python": m["python"],
        },
        "notes": spec.get("notes", []),
        "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
