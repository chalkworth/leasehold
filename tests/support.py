"""Shared helpers for leasehold's tests: a fake clock and load reader, a small machine config, and
the bits the tests that start real processes share."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import leasehold
from leasehold import jobs


def outer_lease(name: str) -> bool:
    """Whether `name` is a variable a lease the tests run inside would set: `LEASEHOLD_*`
    (except the tests' own `LEASEHOLD_UPDATE_GOLDEN`), the job variables and make's flags.
    The tests run as if outside any lease."""
    if name == "LEASEHOLD_UPDATE_GOLDEN":
        return False
    return name.startswith("LEASEHOLD_") or name in {
        *jobs.JOB_VARIABLES,
        "MAKEFLAGS",
        "MFLAGS",
        "CARGO_MAKEFLAGS",
    }


for _name in [n for n in os.environ if outer_lease(n)]:
    del os.environ[_name]


class Orphan:
    """A process started so that its parent exits at once: it belongs to no ancestor of these
    tests, so a `leasehold hold` the tests run inside does not count as its own. The observer
    leaves a process under `hold` alone, so a process it should see starts this way."""

    def __init__(self, argv: list[str]) -> None:
        started = subprocess.run(
            ["sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!', "sh", *argv],
            capture_output=True, text=True, timeout=10, check=True,
        )  # fmt: skip
        self.pid = int(started.stdout)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, 9)

    def wait(self, timeout: float = 10.0) -> None:
        """Until the process is gone; its new parent reaps it."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (OSError, IndexError):
                return
            if state in ("Z", "X"):
                return
            time.sleep(0.02)


#: The directory `leasehold` is imported from, for child processes' PYTHONPATH.
SRC = Path(leasehold.__file__).resolve().parent.parent
#: The environment a child `python -m leasehold` runs with.
CHILD_ENV = {**os.environ, "PYTHONPATH": str(SRC)}


class Clock:
    """A clock a test moves by hand (`clock.now += 10`)."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def gone(pid: int) -> bool:
    """Whether process `pid` has ended (a zombie counts)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def until(predicate, timeout: float = 10.0) -> None:
    """Wait until `predicate()` is true, failing the test after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting")
        time.sleep(0.05)


def cli(sock: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    """`leasehold --socket SOCK ARGS...` in a child process, run to its end."""
    return subprocess.run(
        [sys.executable, "-m", "leasehold", "--socket", str(sock), *args],
        env=kw.pop("env", CHILD_ENV),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **kw,
    )


#: The idle machine a fake load reader reports by default.
IDLE = {"cpu": 0.0, "memory": 0.0, "io": 0.0}


def _psi(avg10: float | None) -> dict | None:
    if avg10 is None:
        return None
    return {"some": {"avg10": avg10, "avg60": avg10}, "full": {"avg10": 0.0, "avg60": 0.0}}


class FakeLoad:
    """A `LoadReader` stand-in whose pressure a test sets (`set(cpu=50)`)."""

    def __init__(self, loadavg: float | None = 0.1, **pressure: float | None) -> None:
        self.values = dict(IDLE) | pressure
        self.loadavg = loadavg
        self.by_cgroup: dict[str, dict] = {}
        #: `MemAvailable` in bytes, as a test sets it.
        self.available: int | None = 8 << 30
        #: The cgroup memory limit, as `LoadReader.limit` reports it (None: none).
        self.memory_limit: dict | None = None

    def limit(self) -> dict | None:
        return self.memory_limit

    def set(self, **pressure: float | None) -> None:
        self.values |= pressure

    def pressure(self, cgroup: str | None = None) -> dict:
        values = self.values if cgroup is None else self.by_cgroup.get(cgroup, {})
        return {r: _psi(values.get(r)) for r in ("cpu", "memory", "io")}

    def read(self) -> dict:
        return {
            "pressure": self.pressure(),
            "loadavg": None if self.loadavg is None else [self.loadavg] * 3,
            "mem_available_bytes": self.available,
            "mem_total_bytes": 16 << 30,
            "cgroup_v2": True,
            "slices": {},
            "memory_limit": self.memory_limit,
        }


FACTS = {"cpus_logical": 4, "memory_bytes": 16 << 30, "arch": "x86_64", "kvm": False}


#: The default pools of `machine_config`, with no headroom in `work`: a range alone there
#: grows to its most.
def machine_config(tmp, **overrides):
    """A small machine with the default pools on 2 slots: `work` reserves 1 and may use 2,
    `external` and `lend` may use 1 each, `observed` 2. A dict override is merged into the
    default's dict (`lend={"window": ...}`); anything else replaces it."""
    from leasehold.machine import MachineConfig

    raw = {
        "name": "test-machine",
        "ceiling": {"slots": 2},
        "ledger_dir": str(tmp),
        "socket": str(tmp / "s.sock"),
        "tick_s": 0.05,
        "lend": {"quiet_s": 0},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = raw[key] | value
        else:
            raw[key] = value
    return MachineConfig.model_validate(raw)


def make_agent(tmp, *, minute=lambda _now: 120, **overrides):
    """A `MachineAgent` on `machine_config(tmp, **overrides)` with a hand-moved clock and a fake
    load reader, at 02:00 local time unless `minute` says otherwise: (agent, clock, load)."""
    from leasehold.agent import MachineAgent

    clock = Clock()
    load = FakeLoad()
    config = machine_config(tmp, **overrides)
    agent = MachineAgent(config, facts=FACTS, load=load, clock=clock, minute=minute)
    return agent, clock, load


def lease_ids(agent) -> list[tuple[str, str, bool]]:
    """The agent's leases as (lease id, kind, revoked), oldest first."""
    return [(x["lease_id"], x["kind"], x["revoked"]) for x in agent.leases()["leases"]]
