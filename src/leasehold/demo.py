"""`leasehold demo`: a throwaway machine with a scripted crowd on it, to watch with `top` or
`ui` (and to take screenshots of). Nothing touches the real machine: the daemon serves a socket
in a temporary directory, its load is made up from what it holds, and its users are the
script's, given to the agent directly.

The crowd, on a 12-slot machine: alice runs two coding-agent sessions and bob one, each taking
a lease per heavy command; two CI runners take external leases for their jobs; dave lends idle
capacity to a batch job, which is revoked when other work waits; and carol starts a known data
preparation program by hand now and then, which `[[observe]]` charges. A seed makes the script
repeat exactly.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from leasehold.agent import AcquireRequest, MachineAgent
from leasehold.machine import MachineConfig
from leasehold.proctable import Proc
from leasehold.server import Daemon

FACTS = {
    "arch": "x86_64",
    "cpus_logical": 16,
    "cpus_physical": 8,
    "memory_bytes": 64 << 30,
    "kvm": True,
    "kernel": "6.12.0",
    "os": "Debian GNU/Linux 13",
}

#: A rule no real program on this machine matches: the demo's own stand-in carries the marker.
MARKER = "leasehold-demo-prep"


def config(directory: Path) -> MachineConfig:
    return MachineConfig.model_validate(
        {
            "name": "buildbox",
            "mode": "user",
            "labels": {"site": "rack1"},
            "ledger_dir": str(directory),
            "socket": str(directory / "leasehold.sock"),
            "tick_s": 1.0,
            "lease_ttl_s": 15.0,
            "ceiling": {"slots": 12, "cpus": 16, "memory": "48g"},
            "pools": {
                "work": {"reserved": 4, "max": 6, "priority": 0},
                "external": {"reserved": 0, "max": 8, "priority": 1},
                "lend": {"reserved": 0, "max": 8, "priority": 2},
                "observed": {"reserved": 0, "max": 12, "priority": 3},
            },
            "lend": {"quiet_s": 20, "cpu_pressure_max": 10.0},
            "observe": [{"name": "training-prep", "match": {"cmdline": MARKER}, "weight": 3}],
        }
    )


class ScriptedLoad:
    """Pressure that follows what the machine holds: calm under half full, climbing past it."""

    def __init__(self, held: Callable[[], int], rng: random.Random) -> None:
        self._held = held
        self._rng = rng
        self._cpu = 0.5

    def _psi(self, value: float) -> dict:
        return {"some": {"avg10": round(value, 1), "avg60": round(value * 0.8, 1)},
                "full": {"avg10": round(value / 4, 1), "avg60": round(value / 5, 1)}}  # fmt: skip

    def pressure(self, cgroup: str | None = None) -> dict:
        target = max(0.3, (self._held() - 6) * 7.0) + self._rng.uniform(-1.0, 1.0)
        self._cpu += (target - self._cpu) * 0.3
        cpu = max(0.0, self._cpu)
        return {"cpu": self._psi(cpu), "memory": self._psi(cpu / 12), "io": self._psi(cpu / 5)}

    def limit(self) -> dict | None:
        """No cgroup memory limit: the demo's machine is all its own."""
        return None

    def read(self) -> dict:
        held = self._held()
        return {
            "pressure": self.pressure(),
            "loadavg": [round(held * 0.95, 1), round(held * 0.8, 1), round(held * 0.6, 1)],
            "mem_available_bytes": (64 - 3 * held) << 30,
            "mem_total_bytes": 64 << 30,
            "cgroup_v2": True,
            "slices": {},
            "memory_limit": None,
        }


@dataclass(frozen=True)
class Actor:
    """One member of the crowd: what it asks for, how long it holds, how long it rests."""

    user: str
    kind: str
    weight: int
    hold: tuple[float, float]
    rest: tuple[float, float]
    labels: dict[str, str]
    session: str | None = None
    prefix: str = "job"


CROWD = (
    Actor("alice", "work", 2, (18, 40), (4, 12), {"agent": "claude-code", "cmd": "pytest -n auto"},
          "cc-7f3a", "cc"),
    Actor("alice", "work", 2, (25, 55), (6, 15), {"agent": "claude-code",
          "cmd": "cargo build --release"}, "cc-91be", "cc"),
    Actor("bob", "work", 2, (20, 35), (5, 10), {"agent": "codex", "cmd": "npm test"},
          "codex-2", "cx"),
    Actor("bob", "work", 1, (60, 120), (10, 20), {"cmd": "make -j"}),
    Actor("runner", "external", 4, (35, 70), (8, 20), {"consumer": "github-runner",
          "repository": "acme/api", "job": "test"}, None, "gh.buildbox-1"),
    Actor("runner", "external", 4, (30, 60), (12, 25), {"consumer": "github-runner",
          "repository": "acme/web", "job": "e2e"}, None, "gh.buildbox-2"),
    Actor("dave", "lend", 6, (60, 120), (5, 10), {"cmd": "./batch-job"}, None, "lend"),
)  # fmt: skip


class Demo:
    """The machine, its daemon and its crowd; `start` and `stop`."""

    def __init__(self, *, seed: int = 0) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="leasehold-demo-"))
        self.config = config(self.directory)
        self.rng = random.Random(seed)
        self.stopping = threading.Event()
        self._prep: subprocess.Popen | None = None
        self.agent = MachineAgent(
            self.config,
            facts=FACTS,
            load=ScriptedLoad(self._held, random.Random(seed + 1)),
            processes=self._processes,
            user_name=lambda uid: "carol",
        )
        self.daemon = Daemon(self.config, self.agent)
        self._threads: list[threading.Thread] = []

    @property
    def socket(self) -> Path:
        return Path(self.config.socket)

    def _held(self) -> int:
        return sum(lease["weight"] for lease in self.agent.ledger.leases())

    def _processes(self) -> dict[int, Proc]:
        prep = self._prep
        if prep is None or prep.poll() is not None:
            return {}
        stat = Path(f"/proc/{prep.pid}/stat").read_text()
        start = int(stat[stat.rindex(")") + 2 :].split()[19])
        cmdline = f"python3 prep_dataset.py --shards 64 {MARKER}"
        proc = Proc(pid=prep.pid, ppid=1, uid=4242, start=start, comm="python3")
        proc._read.add("cmdline")
        proc._cmdline = cmdline
        return {prep.pid: proc}

    def start(self) -> None:
        self.daemon.start()
        rngs = [random.Random(self.rng.random()) for _ in CROWD]
        for n, (actor, rng) in enumerate(zip(CROWD, rngs, strict=True)):
            thread = threading.Thread(target=self._play, args=(actor, rng, n), daemon=True)
            self._threads.append(thread)
        self._threads.append(threading.Thread(target=self._carol, daemon=True))
        for thread in self._threads:
            thread.start()

    def _play(self, actor: Actor, rng: random.Random, n: int) -> None:
        self.stopping.wait(rng.uniform(0, 6) + n * 0.7)  # not all at once
        count = 0
        while not self.stopping.is_set():
            count += 1
            lease_id = f"{actor.prefix}.{rng.getrandbits(32):08x}"
            request = AcquireRequest(
                kind=actor.kind, weight=actor.weight, lease_id=lease_id, labels=actor.labels,
                session=actor.session, wait_s=2.0,
            )  # fmt: skip
            answer = self.agent.acquire_wait(request, user=actor.user)
            while answer["status"] == "queued" and not self.stopping.is_set():
                answer = self.agent.acquire_wait(request, user=actor.user)
            if answer["status"] != "granted":
                self.stopping.wait(rng.uniform(*actor.rest))
                continue
            self._hold(lease_id, actor, rng.uniform(*actor.hold))
            self.stopping.wait(rng.uniform(*actor.rest))

    def _hold(self, lease_id: str, actor: Actor, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self.stopping.wait(2.0):
            renewal = self.agent.renew(lease_id, user=actor.user)
            if renewal["status"] == "revoked":
                self.stopping.wait(3.0)  # the batch job stops within its grace
                break
        self.agent.release(lease_id, user=actor.user)

    def _carol(self) -> None:
        """A data preparation run started by hand every couple of minutes."""
        self.stopping.wait(45)
        while not self.stopping.is_set():
            self._prep = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)", MARKER]
            )
            self.stopping.wait(60)
            self._stop_prep()
            self.stopping.wait(70)

    def _stop_prep(self) -> None:
        if self._prep is not None and self._prep.poll() is None:
            self._prep.kill()
            self._prep.wait()

    def stop(self) -> None:
        """Stop the crowd, then the daemon, then remove the machine. Each actor finishes the
        call it is in (at most a couple of seconds) before the ledger goes."""
        self.stopping.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self._stop_prep()
        self.daemon.stop()
        shutil.rmtree(self.directory, ignore_errors=True)
