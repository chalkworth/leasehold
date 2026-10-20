"""How busy this machine is now, measured rather than declared.

* PSI (`/proc/pressure/{cpu,memory,io}`): the share of time tasks stalled on each resource,
  `some` and `full`, as `avg10`/`avg60` percentages;
* `/proc/loadavg` and `MemAvailable` from `/proc/meminfo`;
* per configured cgroup (systemd slices such as `work.slice`): CPU used since the last reading,
  from `cpu.stat`'s `usage_usec`, as cores, `memory.current`, and the slice's own PSI;
* when asked for (`memory_limit`), the lowest memory limit on this process's own cgroup and its
  ancestors (`leasehold.cgroup`), with what the cgroup that sets it uses now (its inactive
  file cache left out). `available` is then the smaller of `MemAvailable` and what is left
  under that limit.

Anything missing -- no PSI (an older or a container kernel), cgroup v1, a slice not created
yet -- is reported as None (unknown), never an error. Roots and the clock are injectable.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from pathlib import Path

from leasehold import cgroup
from leasehold.facts import meminfo

RESOURCES = ("cpu", "memory", "io")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def parse_pressure(text: str | None) -> dict | None:
    """A PSI file as `{"some": {"avg10": .., "avg60": ..}, "full": {...}}`; None if unreadable."""
    if not text:
        return None
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        try:
            out[parts[0]] = {"avg10": float(fields["avg10"]), "avg60": float(fields["avg60"])}
        except (KeyError, ValueError):
            continue
    return out or None


def _loadavg(proc: Path) -> list[float] | None:
    text = _read(proc / "loadavg")
    try:
        return [float(x) for x in (text or "").split()[:3]] or None
    except ValueError:
        return None


def _usage_usec(cgroup: Path) -> int | None:
    for line in (_read(cgroup / "cpu.stat") or "").splitlines():
        name, _, value = line.partition(" ")
        if name == "usage_usec" and value.strip().isdigit():
            return int(value)
    return None


def _int(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


class LoadReader:
    """Reads the load; keeps the previous `usage_usec` of each cgroup to turn it into a rate."""

    def __init__(
        self,
        slices: Iterable[str] = (),
        *,
        proc: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        clock: Callable[[], float] = time.monotonic,
        memory_limit: bool = False,
    ) -> None:
        self.slices = list(slices)
        #: Whether to read the memory limit of this process's own cgroup.
        self.memory_limit = memory_limit
        self.proc = proc
        self.cgroup_root = cgroup_root
        self._clock = clock
        self._last: dict[str, tuple[float, int]] = {}

    def cgroup_v2(self) -> bool:
        return (self.cgroup_root / "cgroup.controllers").is_file()

    def pressure(self, cgroup: str | None = None) -> dict[str, dict | None]:
        """PSI per resource: the machine's, or `cgroup`'s (a path under the cgroup root)."""
        if cgroup is None:
            return {r: parse_pressure(_read(self.proc / "pressure" / r)) for r in RESOURCES}
        base = self.cgroup_root / cgroup
        return {r: parse_pressure(_read(base / f"{r}.pressure")) for r in RESOURCES}

    def limit(self) -> dict | None:
        """The lowest memory limit on this process's cgroup and its ancestors, as
        `{"cgroup", "limit_bytes", "used_bytes"}` (`used_bytes`: what that cgroup uses now,
        `leasehold.cgroup.used`, None if unreadable); None when not asked for, or when no
        cgroup sets one."""
        if not self.memory_limit:
            return None
        own = cgroup.own_cgroup(self.proc)
        found = None if own is None else cgroup.limit(self.cgroup_root, own)
        if found is None:
            return None
        return {
            "cgroup": found.cgroup,
            "limit_bytes": found.limit_bytes,
            "used_bytes": cgroup.used(self.cgroup_root, found.cgroup),
        }

    def _slice(self, name: str, now: float) -> dict | None:
        base = self.cgroup_root / name
        if not base.is_dir():
            return None
        usage = _usage_usec(base)
        cpus = None
        if usage is not None:
            last = self._last.get(name)
            if last is not None and now > last[0] and usage >= last[1]:
                cpus = round((usage - last[1]) / 1e6 / (now - last[0]), 3)
            self._last[name] = (now, usage)
        mem = _read(base / "memory.current")
        pressure = self.pressure(name)
        return {
            "cpu_usage_usec": usage,
            "cpus_used": cpus,
            "memory_current_bytes": _int(mem),
            "pressure": {"cpu": pressure["cpu"], "memory": pressure["memory"]},
        }

    def read(self) -> dict:
        now = self._clock()
        mem = meminfo(self.proc)
        v2 = self.cgroup_v2()
        return {
            "pressure": self.pressure(),
            "loadavg": _loadavg(self.proc),
            "mem_available_bytes": mem.get("MemAvailable"),
            "mem_total_bytes": mem.get("MemTotal"),
            "cgroup_v2": v2,
            "slices": {name: (self._slice(name, now) if v2 else None) for name in self.slices},
            "memory_limit": self.limit(),
        }


def available(reading: dict | None) -> int | None:
    """The memory available to new work in `reading` (a `LoadReader.read`): `MemAvailable`,
    or less when a cgroup limit leaves less (its limit less what that cgroup uses now). None
    when neither is known."""
    reading = reading or {}
    machine = reading.get("mem_available_bytes")
    bound = reading.get("memory_limit")
    if not bound or bound.get("used_bytes") is None:
        return machine
    left = max(0, bound["limit_bytes"] - bound["used_bytes"])
    return left if machine is None else min(machine, left)


def some_avg10(pressure: dict | None) -> float | None:
    return None if not pressure or "some" not in pressure else pressure["some"]["avg10"]
