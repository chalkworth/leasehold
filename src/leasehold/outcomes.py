"""What contained commands used: the outcome a client sends with its release (`Outcome`, measured
by `leasehold.measure`), and the summary `GET /v1/outcomes` gives of them (`summarize`).

The ledger keeps each outcome with what the daemon knows of its lease (`Ledger.record_outcome`).
Outcomes are reported by the client and only shown: nothing decides admission by them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from leasehold.units import MEMORY_MAX

_Bytes = Field(None, ge=0, le=MEMORY_MAX)
_Count = Field(None, ge=0, le=1 << 62)


class Outcome(BaseModel):
    """What one contained command used, as its client measured it. Fields this daemon does not
    know (from a newer client) are dropped, so a newer client's outcome is still taken."""

    model_config = ConfigDict(extra="ignore")

    #: The `[commands]` rule that sized the command (`shell.Rule.key`); None: none did.
    rule: str | None = Field(None, max_length=1024)
    #: The top of the git work tree it ran in; None: none.
    repo: str | None = Field(None, max_length=4096)
    #: `memory.peak`: the most memory its scope used, page cache included.
    peak_bytes: int | None = _Bytes
    #: The most anonymous memory (`memory.stat` `anon`) its scope was seen to use.
    anon_bytes: int | None = _Bytes
    #: `memory.events` `oom_kill` and `high`.
    oom_kills: int | None = _Count
    high_events: int | None = _Count
    #: `cpu.stat` `usage_usec`: the CPU time it used.
    cpu_usec: int | None = _Count
    #: How long it ran.
    wall_s: float = Field(ge=0, le=1e9)
    #: Its exit status; negative: the signal that ended it.
    exit_status: int | None = Field(None, ge=-255, le=255)


def summarize(rows: Iterable[Mapping]) -> list[dict]:
    """`rows` (outcomes as the ledger keeps them, newest first) grouped by rule and repository,
    the group with the newest outcome first. Each group has its `rule`, `repo`, `runs`; the
    95th percentile (nearest rank) and most of `peak_bytes`, and the most `anon_bytes`, over the
    runs that report them (None when none does); `oom_runs` and `high_runs`, the runs the OOM
    killer or `MemoryHigh` reached; `parallelism`, the mean of each run's CPU time over its wall
    time (None when no run reports both); and `last_ended_at`."""
    groups: dict[tuple, list[Mapping]] = {}
    for row in rows:
        groups.setdefault((row["rule"], row["repo"]), []).append(row)
    out = []
    for (rule, repo), runs in groups.items():
        peaks = sorted(r["peak_bytes"] for r in runs if r["peak_bytes"] is not None)
        anons = [r["anon_bytes"] for r in runs if r["anon_bytes"] is not None]
        shares = [
            r["cpu_usec"] / 1e6 / r["wall_s"]
            for r in runs
            if r["cpu_usec"] is not None and r["wall_s"] > 0
        ]
        out.append(
            {
                "rule": rule,
                "repo": repo,
                "runs": len(runs),
                "peak_p95_bytes": peaks[math.ceil(0.95 * len(peaks)) - 1] if peaks else None,
                "peak_max_bytes": peaks[-1] if peaks else None,
                "anon_max_bytes": max(anons) if anons else None,
                "oom_runs": sum(1 for r in runs if r["oom_kills"]),
                "high_runs": sum(1 for r in runs if r["high_events"]),
                "parallelism": sum(shares) / len(shares) if shares else None,
                "last_ended_at": max(r["ended_at"] for r in runs),
            }
        )
    out.sort(key=lambda g: g["last_ended_at"], reverse=True)
    return out
