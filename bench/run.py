"""The leasehold benchmark: several coding agents each starting heavy jobs on one Linux machine,
run under each way of keeping the machine usable that a skeptic would name.

    uv run python bench/run.py --quick                # about 2 minutes a condition
    uv run python bench/run.py --agents 8 --duration 300
    uv run python bench/run.py --dry-run              # print the seeded schedule and stop
    uv run python bench/run.py summarize DIR          # rebuild the summary of a finished run
    uv run python bench/run.py compare DIR --gate bench/gates/defaults.toml

Each of K simulated agents works through its own seeded list of steps: think for a random
(exponential) time, then run one job and wait for it, as an agent waits for its tool call. The
jobs (`bench/job.py`) are either `cpu` (hashing spread over as many workers as the job may use)
or `mem` (the same, over a large block of touched memory). Every condition in a round runs the
same lists, so every condition does the same work; only how the jobs are started changes:

* `unmanaged`: as they are, beside the person's own programs;
* `nice`: under `nice -n 19`;
* `slice`: each in a scope of one systemd user slice with a low `CPUWeight` (`--slice-weight`);
* `lh-advisory`: through `leasehold hold` with containment off (`LEASEHOLD_CONTAIN=off`): a
  lease and a job count, nothing enforced;
* `lh-enforced`: through `leasehold hold` with containment on: the lease's scope, weighted;
* `lh-advisory-greedy`, `lh-enforced-greedy`: the same, with jobs that ignore the job count
  they are given and use every CPU, as a tool that reads no job variable does;
* `lh-hardcpu-greedy`: as `lh-enforced-greedy`, with `[contain] hard_cpu = true`, which caps
  each scope at its weight in CPUs;
* `static-jobs`: unmanaged, but every job is told the job count a lease of its weight would
  give (`LEASEHOLD_JOBS`), as when one sets `-j4` everywhere and coordinates nothing;
* `lh-enforced-declared`: as `lh-enforced`, with each `mem` job declaring its size plus 512
  MiB (`hold --memory`), which a config with a memory budget counts;
* `lh-enforced-v020`: as `lh-enforced`, on the config leasehold 0.2.0's `init --user` wrote
  for this machine (no memory counted) with every job at 0.2.0's built-in weight, 4;
* `lh-jobserver`: as `lh-enforced`, through `hold --jobserver`, with jobs that are clients of
  the lease's jobserver (`job.py --jobserver`), and leases that are never elastic
  (`hold --no-elastic`, where the installed leasehold has elastic leases);
* `lh-elastic`: as `lh-jobserver`, with elastic leases (`hold --elastic`). It needs a leasehold
  that has them, and stops with an error before anything runs if the installed one does not.

The leasehold conditions take parameters (`bench/conditions.py`): `lh-enforced@w=2-4,h=1`
asks for 2-4 slots with a work-pool headroom of 1; `kept=N` keeps N CPUs back from the slots,
`grow=use` lets an `lh-elastic` lease grow into the headroom, `mem=declared` declares memory,
`budget=7g` sets the memory budget, and `arm=b` tells two arms of one condition apart.
`lh-enforced@park=freeze` runs the parker (`bench/park.py`), an experiment that pauses a `mem`
job and pushes its memory to swap instead of letting the OOM killer end it (`overflow=1g`
and `parkcap=24g` tune it, `park=threshold` is its older trigger); `overflow=1g` alone gives
each `mem` job that swap limit and parks nothing; `hold=parked` holds back a `mem` job's start
while a job is parked.

leasehold's conditions run a private user-mode daemon on a socket of their own, with the
config `leasehold init --user` writes for this machine (its `[memory] budget` replaced by
`--memory-budget` when given) and the condition's parameters, and `hold --weight` as the
built-in `[commands]` rules weigh a test run in it (`--weights`, by default the config's
`heavy_weight`), or the condition's `w`.

Rounds (`bench/schedule.py`): each round runs every condition once, on the plan of its own seed
(`--seed-base` plus the round), in the order of a Williams design (AB, then BA, for two).
`--round N` runs round N alone into `--dir`, so that a runner can run one round per invocation
and pick up where it stopped. Before each run the harness waits for the machine to be quiet
(`bench/washout.py`), and marks the run invalid if it never is.

Safety: everything the benchmark starts is held to one memory limit (`MemoryMax`, with a
bounded `MemorySwapMax`): the harness's own scope for the conditions whose jobs run in it, and
the slice the jobs run in for `slice` and the enforced conditions. Running out of that memory
makes the kernel kill a job (jobs raise their OOM score), never the person's session. The run
also stops every job if the machine itself runs short of memory.

What is measured, per run: makespan; each job's completion time from when its agent asked
(and, under leasehold, the wait before it started); a 10 ms wakeup probe in the jobs' own cgroup
(a shell in the agents' terminal, an editor running the agent) and one in the session the
benchmark was started from; optionally `git status` time; OOM kills; swap-in and swap-out;
memory pressure; and under `park` or `overflow`, what the parker did (`park.csv`).
Raw rows go to CSV files beside a summary table, and each run's `manifest.json` says what ran
(`bench/manifest.py`). `compare` judges the runs by a gate file written before they ran
(`bench/gate.py`, `bench/gates/`).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import gzip
import json
import math
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import conditions as conds
import manifest
import park
import schedule
import washout
from conditions import DECLARED_EXTRA, DEFAULT_CONDITIONS

HERE = Path(__file__).resolve().parent
JOB = HERE / "job.py"
PROBE = HERE / "probe.py"
GIB = 1 << 30
MIB = 1 << 20
#: The size of a page, which /proc/vmstat counts swap in.
PAGE = os.sysconf("SC_PAGE_SIZE")

#: The built-in weight of a test run or build in leasehold 0.2.0.
WEIGHT_V020 = 4
#: How the abort reason of a run stopped by the harness's own failure starts: such a run is
#: invalid, and breaks no hard rule.
HARNESS_ERROR = "harness error"
_CLASS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Preset:
    duration_s: float
    think_s: float  # mean think time between an agent's jobs
    cpu_s: float  # a cpu job's length alone on the machine, using every CPU
    mem_s: float  # a mem job's hashing length alone on the machine, after its touch
    p_mem: float  # the share of jobs that are mem jobs


PRESETS = {
    "full": Preset(duration_s=600, think_s=30, cpu_s=10, mem_s=6, p_mem=0.35),
    "quick": Preset(duration_s=120, think_s=20, cpu_s=6, mem_s=4, p_mem=0.35),
    # For the test: no calibration, tiny jobs, no systemd.
    "tiny": Preset(duration_s=1, think_s=0.1, cpu_s=0.05, mem_s=0.05, p_mem=0.5),
}


# ---------------------------------------------------------------------------------------------
# The machine


def meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0]) * 1024
    return out


def vmstat() -> dict[str, int]:
    pairs = (line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    return {k: int(v) for k, v in pairs}


def psi(resource: str, cgroup: Path | None = None) -> dict[str, float]:
    """`some_avg10`, `full_avg10` of /proc/pressure/RESOURCE, or of `cgroup`'s
    RESOURCE.pressure (missing: empty)."""
    out = {}
    path = cgroup / f"{resource}.pressure" if cgroup else Path(f"/proc/pressure/{resource}")
    try:
        for line in path.read_text().splitlines():
            kind, *fields = line.split()
            values = dict(f.split("=") for f in fields)
            out[f"{kind}_avg10"] = float(values["avg10"])
    except OSError:
        pass
    return out


def cpu_times() -> tuple[int, int, int]:
    """(busy, total, steal) jiffies of all CPUs; steal is time a virtual machine's CPUs
    wanted and its host gave to something else."""
    fields = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + fields[4]
    total = sum(fields[:8])
    return total - idle, total, fields[7]


def machine(label: str | None = None) -> dict:
    """What the results say about the machine: never its name or its CPU model. `class` is
    `label`, or `<CPUs>cpu-<GiB>g`."""
    mem = meminfo()
    cpus = os.cpu_count() or 1
    try:
        virt = subprocess.run(
            ["systemd-detect-virt"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        virt = "unknown"
    return {
        "cpus": cpus,
        "memory_bytes": mem["MemTotal"],
        "swap_bytes": mem.get("SwapTotal", 0),
        "kernel": platform.release(),
        "virt": virt or "none",
        "python": platform.python_version(),
        "class": label or f"{cpus}cpu-{round(mem['MemTotal'] / GIB)}g",
    }


# ---------------------------------------------------------------------------------------------
# The plan


def make_plan(seed: int, agents: int, preset: Preset, nominal: dict[str, float]) -> list[list]:
    """Each agent's steps, `[think_s, kind]`, until its planned time (think plus each job's
    length alone on the machine) reaches the duration. Think times are exponential, cut at four
    times their mean so that no one long pause decides the makespan. Agent i's steps depend only
    on the seed and i, so a run with more agents adds agents and keeps the others' steps."""
    plan = []
    for i in range(agents):
        rng = random.Random(f"{seed}:{i}")
        steps, t = [], 0.0
        while t < preset.duration_s:
            think = min(rng.expovariate(1 / preset.think_s), 4 * preset.think_s)
            kind = "mem" if rng.random() < preset.p_mem else "cpu"
            steps.append([round(think, 2), kind])
            t += think + nominal[kind]
        plan.append(steps)
    return plan


def spec_plan(spec: dict, seed: int) -> list[list]:
    """The plan of `seed` for the run `spec` describes."""
    return make_plan(seed, spec["agents"], Preset(**spec["preset"]), spec["nominal_s"])


def calibrate(cpus: int) -> tuple[float, float]:
    """(bytes hashed per second per CPU with every CPU busy, seconds to touch one GiB)."""
    work = 2 * GIB * cpus
    t0 = time.monotonic()
    subprocess.run(
        [sys.executable, str(JOB), "cpu", "--work", str(work), "--ignore-env"],
        check=True,
        capture_output=True,
    )
    rate = work / ((time.monotonic() - t0) * cpus)
    t0 = time.monotonic()
    size = 512 * MIB
    subprocess.run(
        [sys.executable, str(JOB), "mem", "--work", "0", "--size", str(size)],
        check=True,
        capture_output=True,
    )
    touch = (time.monotonic() - t0) * GIB / size
    return rate, touch


# ---------------------------------------------------------------------------------------------
# A condition's setup: how a job is started


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=60
    )


def slice_cgroup(name: str) -> Path:
    """Where the user manager's slice `name` has (or will have) its cgroup: a `-` in a slice's
    name nests it under the slice named by what comes before."""
    uid = os.getuid()
    path = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")
    stem = name.removesuffix(".slice").split("-")
    for i in range(1, len(stem) + 1):
        path /= "-".join(stem[:i]) + ".slice"
    return path


def own_cgroup() -> Path:
    line = next(x for x in Path("/proc/self/cgroup").read_text().splitlines() if x[:3] == "0::")
    return Path("/sys/fs/cgroup" + line[3:])


def read_kv(path: Path) -> dict[str, int]:
    try:
        return {k: int(v) for k, v in (x.split() for x in path.read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def clean_env() -> dict[str, str]:
    """This environment without any leasehold variable, so a job sees only what its condition
    gives it."""
    return {k: v for k, v in os.environ.items() if not k.startswith("LEASEHOLD_")}


def job_weight(cond: conds.Condition, spec: dict, kind: str) -> str:
    """What a `kind` job of `cond` asks `hold --weight` for."""
    if cond.weight is not None:
        return conds.weight_text(cond.weight)
    if cond.base == "lh-enforced-v020":
        return str(WEIGHT_V020)
    return str(spec["weights"][kind])


def condition_config(spec: dict, cond: conds.Condition, ledger: str = "ledger") -> dict | None:
    """The config `cond`'s daemon runs on, and what its jobs ask for (None: not a leasehold
    condition). The ledger is placed at `ledger`; the default is the neutral name the
    manifest records."""
    if not cond.leasehold:
        return None
    text = conds.config(
        spec["init_config"],
        cond,
        cpus=spec["machine"]["cpus"],
        ledger=ledger,
        memory_budget=spec.get("memory_budget"),
    )
    return {
        "summary": conds.summary(text),
        "hold_weight": {k: job_weight(cond, spec, k) for k in ("cpu", "mem")},
        "mem_declared_bytes": spec["mem_bytes"] + DECLARED_EXTRA if cond.declared else None,
        "jobserver": cond.jobserver,
        "elastic": cond.elastic,
        "contained": cond.contained,
        "text": text,
    }


class Setup:
    """How one condition starts a job, and what it sets up and takes down around the run."""

    def __init__(self, condition: str, spec: dict, workdir: Path) -> None:
        self.cond = conds.parse(condition)
        self.condition = self.cond.base
        self.spec = spec
        self.workdir = workdir
        self.token = os.urandom(3).hex()
        self.slice: str | None = None
        self.daemon: subprocess.Popen | None = None
        self.sockdir: Path | None = None
        self.env = clean_env()
        self.leasehold = self.cond.leasehold
        self.greedy = self.cond.greedy

    def start(self) -> None:
        caps = [f"MemoryMax={self.spec['cap_bytes']}", f"MemorySwapMax={self.spec['swap_bytes']}"]
        if self.condition == "slice":
            self.slice = f"lhbenchjobs_{self.token}.slice"
            weight = f"CPUWeight={self.spec['slice_weight']}"
            self._set_property(self.slice, weight, *caps)
        elif self.cond.contained:
            # A session slice as `leasehold run` makes, under leasehold.slice; it carries the
            # benchmark's memory limit, as the harness's scope does for the other conditions.
            # Under `park=threshold`, MemoryHigh throttles growth near the limit so the parker
            # can act first; `stop` reverts it with the rest.
            self.slice = f"leasehold-bench_{self.token}.slice"
            if self.cond.park == "threshold":
                caps.append(f"MemoryHigh={int(park.HIGH_AT * self.spec['cap_bytes'])}")
            self._set_property(self.slice, *caps)
            self.env["LEASEHOLD_SLICE"] = self.slice
        if self.condition.startswith("lh-advisory"):
            self.env["LEASEHOLD_CONTAIN"] = "off"
        if self.leasehold:
            self._start_daemon()

    def _set_property(self, unit: str, *props: str) -> None:
        done = systemctl("set-property", "--runtime", unit, *props)
        if done.returncode != 0:
            raise SystemExit(f"cannot set up {unit}: {done.stderr.strip()}")

    def _start_daemon(self) -> None:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.sockdir = Path(tempfile.mkdtemp(prefix="lhbench-", dir=runtime))
        # The daemon's config and ledger live beside its socket, so the results directory holds
        # no path of this machine's; config.toml there is the same config, ledger named only.
        config = self.sockdir / "machine.toml"
        try:
            resolved = condition_config(self.spec, self.cond, str(self.sockdir / "ledger"))
        except ValueError as exc:
            raise SystemExit(f"{self.cond.text}: {exc}") from exc
        assert resolved is not None
        config.write_text(resolved["text"])
        neutral = condition_config(self.spec, self.cond)
        assert neutral is not None
        (self.workdir / "config.toml").write_text(neutral["text"])
        log = (self.workdir / "daemon.log").open("w")
        self.daemon = subprocess.Popen(
            [*leasehold_cli(), "serve", "--user", "--config", str(config), "--socket", self.sock],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=self.env,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ok = subprocess.run(
                [*leasehold_cli(), "status", "--socket", self.sock],
                capture_output=True,
                env=self.env,
                check=False,
            )
            if ok.returncode == 0:
                return
            time.sleep(0.2)
        raise SystemExit("the benchmark's leasehold daemon did not start; see daemon.log")

    @property
    def sock(self) -> str:
        assert self.sockdir is not None
        return str(self.sockdir / "s.sock")

    def env_for(self, kind: str) -> dict[str, str]:
        if self.condition == "static-jobs":
            return self.env | {"LEASEHOLD_JOBS": str(self.spec["weights"][kind])}
        return self.env

    def argv(self, agent: int, kind: str) -> list[str]:
        job = [sys.executable, str(JOB), kind, "--work", str(self.spec["work"][kind])]
        if kind == "mem":
            job += ["--size", str(self.spec["mem_bytes"])]
        if self.greedy:
            job.append("--ignore-env")
        if self.cond.jobserver:
            job.append("--jobserver")
        if self.condition == "nice":
            return ["nice", "-n", "19", *job]
        if self.condition == "slice":
            return [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--slice={self.slice}",
                "-p",
                "OOMPolicy=continue",
                "--",
                *job,
            ]
        if self.leasehold:
            # A size rounded up past the job's touched block, as a person declares one: the job's
            # interpreter and workers need a little more, and MemoryHigh is set to it.
            size = self.spec["mem_bytes"] + DECLARED_EXTRA
            declared = ["--memory", f"{size}b"] if self.cond.declared else []
            jobserver = ["--jobserver"] if self.cond.jobserver else []
            elastic = ["--elastic"] if self.cond.elastic else []
            if self.cond.jobserver and not self.cond.elastic and elastic_supported():
                elastic = ["--no-elastic"]
            return [
                *leasehold_cli(),
                "hold",
                "--socket",
                self.sock,
                "--weight",
                job_weight(self.cond, self.spec, kind),
                *(declared if kind == "mem" else []),
                *jobserver,
                *elastic,
                "--session",
                f"agent{agent}",
                "--if-unavailable",
                "fail",
                "--",
                *job,
            ]
        return job

    def job_cgroup(self) -> Path | None:
        return slice_cgroup(self.slice) if self.slice else None

    def kill_jobs(self) -> None:
        if self.slice:
            systemctl("kill", "--signal=KILL", self.slice)

    def stop(self) -> None:
        if self.daemon is not None:
            self.daemon.terminate()
            try:
                self.daemon.wait(15)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
                self.daemon.wait()
        if self.slice:
            systemctl("stop", self.slice)
            systemctl("revert", self.slice)
        if self.sockdir is not None:
            shutil.rmtree(self.sockdir, ignore_errors=True)


def leasehold_cli() -> list[str]:
    return [sys.executable, "-m", "leasehold"]


def init_config() -> str:
    """The config `leasehold init --user` writes for this machine, named `bench`."""
    return conds.neutral(
        subprocess.run(
            [*leasehold_cli(), "init", "--user", "--print"],
            capture_output=True,
            text=True,
            check=True,
            env=clean_env(),
        ).stdout
    )


@functools.cache
def elastic_supported() -> bool:
    """Whether the installed leasehold's `hold` has elastic leases (`--elastic`)."""
    done = subprocess.run(
        [*leasehold_cli(), "hold", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=clean_env(),
    )
    return "--elastic" in done.stdout


def heavy_weight(text: str) -> int:
    """The built-in weight of a build or test run in the config `text`."""
    import tomllib

    return int(tomllib.loads(text).get("commands", {}).get("heavy_weight", WEIGHT_V020))


# ---------------------------------------------------------------------------------------------
# One condition, run inside the benchmark's scope


class Run:
    """The agents, the sampler and the safety stop of one condition."""

    def __init__(self, setup: Setup, spec: dict, workdir: Path) -> None:
        self.setup = setup
        self.spec = spec
        self.workdir = workdir
        self.stop = threading.Event()
        self.aborted: str | None = None
        self.lock = threading.Lock()
        self.jobs: list[dict] = []
        self.live: dict[int, tuple[subprocess.Popen, float]] = {}
        self.samples: list[dict] = []
        self.stderr = (workdir / "jobs.stderr").open("w")
        self.parker: park.Parker | None = None
        self.starts_held = 0
        self.start_hold_s = 0.0
        self.post_thaw_hold_s = 0.0
        cap_cgroup = setup.job_cgroup()
        mode = setup.cond.parker
        if mode and cap_cgroup is not None:
            cap, overflow = setup.cond.park_cap, setup.cond.overflow
            swap_max = park.DEFAULT_OVERFLOW_BYTES if mode == "freeze" else None
            self.parker = park.Parker(
                cap_cgroup,
                spec["cap_bytes"],
                conds.size_bytes(cap) if cap else park.DEFAULT_CAP_BYTES,
                mode,
                conds.size_bytes(overflow) if overflow else swap_max,
                settle=setup.cond.hold == "parked",
            )

    def agent(self, index: int, steps: list) -> None:
        for seq, (think, kind) in enumerate(steps):
            if self.stop.wait(think):
                return
            argv = self.setup.argv(index, kind)
            issued = time.time()
            if kind == "mem" and not self.hold_start(issued):
                return
            proc = subprocess.Popen(
                argv,
                env=self.setup.env_for(kind),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self.stderr,
                text=True,
                start_new_session=True,
            )
            with self.lock:
                self.live[proc.pid] = (proc, issued)
            started = ended = workers = None
            cgroup = ""
            assert proc.stdout is not None
            for line in proc.stdout:
                parts = line.split()
                if parts[:2] == ["BENCH", "start"]:
                    started, workers = float(parts[2]), int(parts[3])
                    cgroup = parts[4] if len(parts) > 4 else ""
                    if self.parker and kind == "mem" and cgroup.startswith("/"):
                        self.parker.track(proc.pid, Path("/sys/fs/cgroup" + cgroup), started)
                elif parts[:2] == ["BENCH", "end"]:
                    ended = float(parts[2])
            code = proc.wait()
            done = time.time()
            if self.parker:
                self.parker.untrack(proc.pid)
            with self.lock:
                self.live.pop(proc.pid, None)
                self.jobs.append(
                    {
                        "agent": index,
                        "seq": seq,
                        "kind": kind,
                        "issued": issued,
                        "started": started,
                        "ended": ended,
                        "done": done,
                        "rc": code,
                        "workers": workers,
                        "cgroup": cgroup,
                    }
                )

    def hold_start(self, issued: float) -> bool:
        """Under `hold=parked`, wait while the parker has a job parked or settling after a
        thaw; False if the run stopped first. The forced thaw, the parker's settling limit and
        the thaw at the run's end bound the wait. Time held only by a settling job counts as
        post-thaw hold too."""
        parker = self.parker
        if not parker or self.setup.cond.hold != "parked" or parker.starts_open.is_set():
            return True
        post_thaw = 0.0
        while True:
            why, t0 = parker.blocked_by(), time.time()
            if parker.starts_open.wait(0.1):
                break
            if why == "settling":
                post_thaw += time.time() - t0
            if self.stop.is_set():
                return False
        with self.lock:
            self.starts_held += 1
            self.start_hold_s += time.time() - issued
            self.post_thaw_hold_s += post_thaw
        return True

    def sampler(self) -> None:
        """Once a second: the machine's CPU, memory, swap and pressure, the benchmark's own
        memory, and the memory pressure inside its cap (`cap_mem_*`, from the cgroup that
        carries `MemoryMax`: the jobs' slice, else the harness's scope). Stops everything if
        the machine itself runs short of memory, and kills a job that has run far past any
        reasonable length."""
        busy0, total0, steal0 = cpu_times()
        mine, jobs = own_cgroup(), self.setup.job_cgroup()
        floor = self.spec["abort_available_bytes"]
        short_since: float | None = None
        limit = self.spec["job_timeout_s"]
        while not self.stop.wait(1.0):
            busy, total, steal = cpu_times()
            mem, vm, pm, pc = meminfo(), vmstat(), psi("memory"), psi("cpu")
            capped = psi("memory", jobs or mine)
            row = {
                "t": time.time(),
                "cpu_busy": 100 * (busy - busy0) / max(1, total - total0),
                "cpu_steal": 100 * (steal - steal0) / max(1, total - total0),
                "load1": float(Path("/proc/loadavg").read_text().split()[0]),
                "mem_available": mem["MemAvailable"],
                "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
                "pswpin": vm.get("pswpin", 0),
                "pswpout": vm.get("pswpout", 0),
                "oom_kill": vm.get("oom_kill", 0),
                "mem_some": pm.get("some_avg10"),
                "mem_full": pm.get("full_avg10"),
                "cpu_some": pc.get("some_avg10"),
                "cap_mem_some": capped.get("some_avg10"),
                "cap_mem_full": capped.get("full_avg10"),
                "bench_mem": (read_int(mine / "memory.current") or 0)
                + ((read_int(jobs / "memory.current") or 0) if jobs else 0),
            }
            busy0, total0, steal0 = busy, total, steal
            self.samples.append(row)
            short = row["mem_available"] < floor or (row["mem_full"] or 0) > 60
            short_since = (short_since or row["t"]) if short else None
            if short_since is not None and row["t"] - short_since >= 10:
                self.abort("the machine ran short of memory")
            with self.lock:
                overdue = [p for p, issued in self.live.values() if row["t"] - issued > limit]
            for proc in overdue:
                _kill_group(proc, signal.SIGKILL)

    def abort(self, why: str) -> None:
        if self.aborted:
            return
        self.aborted = why
        self.stop.set()
        if self.parker:
            self.parker.thaw_all()  # a frozen job acts on no signal but SIGKILL
        with self.lock:
            procs = [p for p, _ in self.live.values()]
        for proc in procs:
            _kill_group(proc, signal.SIGTERM)
        if procs:
            time.sleep(5)
        for proc in procs:
            _kill_group(proc, signal.SIGKILL)
        self.setup.kill_jobs()

    def guarded(self, name: str, work, *args) -> None:
        """`work(*args)`; an exception in it aborts the run (`HARNESS_ERROR`), so a thread
        that dies never leaves a run that looks whole."""
        try:
            work(*args)
        except BaseException as exc:  # noqa: BLE001  (any failure must end the run)
            self.abort(f"{HARNESS_ERROR}: {name}: {exc!r}")

    def run(self, plan: list[list]) -> dict:
        start = time.time()
        sampler = threading.Thread(target=self.guarded, args=("sampler", self.sampler), daemon=True)
        sampler.start()
        parker = None
        if self.parker:
            parker = threading.Thread(
                target=self.guarded, args=("parker", self.parker.run, self.stop), daemon=True
            )
            parker.start()
        agents = [
            threading.Thread(
                target=self.guarded, args=(f"agent {i}", self.agent, i, steps), daemon=True
            )
            for i, steps in enumerate(plan)
        ]
        for t in agents:
            t.start()
        for t in agents:
            t.join()
        end = time.time()
        self.stop.set()
        sampler.join()
        if parker:
            parker.join()
        out = {"start": start, "end": end, "aborted": self.aborted}
        if self.parker:
            out["park"] = self.parker.summary()
        if self.setup.cond.hold:
            out["start_hold"] = {
                "held": self.starts_held,
                "s": round(self.start_hold_s, 1),
                "post_thaw_s": round(self.post_thaw_hold_s, 1),
            }
        return out


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def run_condition(
    spec_path: Path, condition: str, workdir: Path, seed: int, git_repo: str | None
) -> int:
    """One condition, from inside the benchmark's own scope: set up, run the plan of `seed`
    with the in-cgroup probes alongside, take down, and write jobs.csv, samples.csv and
    run.json."""
    spec = json.loads(spec_path.read_text())
    setup = Setup(condition, spec, workdir)
    probes: list[subprocess.Popen] = []
    run: Run | None = None
    if setup.cond.parker:
        # So that a stopped run still reaches the `finally` that thaws its parked jobs.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        setup.start()
        probes.append(_probe("wakeup", workdir / "wakeup_same.csv.gz"))
        if git_repo:
            probes.append(_probe("git", workdir / "git_same.csv.gz", "--repo", git_repo))
        run = Run(setup, spec, workdir)
        events_before = {}
        jobs_cg = setup.job_cgroup()
        cgroups = {"harness": own_cgroup(), **({"jobs": jobs_cg} if jobs_cg else {})}
        for name, cg in cgroups.items():
            events_before[name] = read_kv(cg / "memory.events")
        result = run.run(spec_plan(spec, seed))
        result["seed"] = seed
        for proc in probes:
            proc.send_signal(signal.SIGTERM)
        result["cgroups"] = {}
        for name, cg in cgroups.items():
            after = read_kv(cg / "memory.events")
            result["cgroups"][name] = {
                "path": str(cg),
                "oom_kill": after.get("oom_kill", 0) - events_before[name].get("oom_kill", 0),
                "high": after.get("high", 0) - events_before[name].get("high", 0),
                "max": after.get("max", 0) - events_before[name].get("max", 0),
                "peak": read_int(cg / "memory.peak"),
            }
        _write_csv(workdir / "jobs.csv", run.jobs)
        _write_csv(workdir / "samples.csv", run.samples)
        if run.parker and run.parker.events:
            keys = list(dict.fromkeys(k for e in run.parker.events for k in e))
            _write_csv(workdir / "park.csv", [dict.fromkeys(keys) | e for e in run.parker.events])
        (workdir / "run.json").write_text(json.dumps(result, indent=1))
    finally:
        for proc in probes:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(30)
        if run is not None and run.parker:
            run.parker.thaw_all()
        setup.stop()
    return 0


def _probe(what: str, out: Path, *extra: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(PROBE), what, "--out", str(out), *extra],
        stdin=subprocess.DEVNULL,
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------------------------------------
# The whole run, from the person's session


def scope_argv(unit: str, spec: dict) -> list[str]:
    return [
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={spec['cap_bytes']}",
        "-p",
        f"MemorySwapMax={spec['swap_bytes']}",
        "-p",
        "OOMPolicy=continue",
        "--",
    ]


def slots(spec: dict, only: int | None = None) -> list[schedule.Slot]:
    """The runs of the schedule `spec` describes (of round `only` alone, if given)."""
    names = spec["conditions"]
    seed_base = spec.get("seed_base", spec.get("seed", 1) - 1)
    slugs = [conds.parse(c).slug for c in names]
    return schedule.schedule(names, spec.get("rounds", 1), seed_base, only, slugs)


def runs(conditions: list[str], rounds: int, seed_base: int = 0) -> list[tuple[str, str, int]]:
    """Each run as (condition, label, seed): every condition once a round, a round's seed
    `seed_base + round` shared by every condition in it, in the order of `bench/schedule.py`."""
    spec = {"conditions": conditions, "rounds": rounds, "seed_base": seed_base}
    return [(s.condition, s.label, s.seed) for s in slots(spec)]


def run_one(args, spec: dict, outdir: Path, workdir: Path, slot: schedule.Slot, before: dict):
    """One run, in the benchmark's scope; its summary row."""
    outside = _probe("wakeup", workdir / "wakeup_outside.csv.gz")
    vm0 = vmstat()
    unit = f"lhbench-{conds.parse(slot.condition).base}-{os.urandom(3).hex()}.scope"
    inner = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_condition",
        "--spec",
        str(outdir / "spec.json"),
        "--condition",
        slot.condition,
        "--dir",
        str(workdir),
        "--seed",
        str(slot.seed),
        *(["--git-repo", args.git_repo] if args.git_repo else []),
    ]
    argv = inner if args.no_scope else [*scope_argv(unit, spec), *inner]
    t0 = time.monotonic()
    try:
        done = subprocess.run(argv, check=False)
    finally:
        outside.send_signal(signal.SIGTERM)
        outside.wait(30)
        if not args.no_scope:
            systemctl("kill", "--signal=KILL", unit)
        if conds.parse(slot.condition).parker:
            for cg in park.thaw_leftovers(slice_cgroup("leasehold.slice")):
                print(f"thawed {cg.name}, left frozen by its run", flush=True)
    vm1 = vmstat()
    extra = {
        "before": before,
        "inner_rc": done.returncode,
        "wall_s": time.monotonic() - t0,
        "pswpin": vm1.get("pswpin", 0) - vm0.get("pswpin", 0),
        "pswpout": vm1.get("pswpout", 0) - vm0.get("pswpout", 0),
        "oom_kill_machine": vm1.get("oom_kill", 0) - vm0.get("oom_kill", 0),
    }
    (workdir / "outer.json").write_text(json.dumps(extra, indent=1))
    return summarize_condition(workdir, slot.label)


def run_all(args, spec: dict, outdir: Path, log, only: int | None = None) -> list[dict]:
    """Every run of the schedule (or of round `only`), each after a washout; the summary rows
    of the runs that ran."""
    now = {
        "git": manifest.git_state(),
        "leasehold_version": manifest.leasehold_version(),
        "notes": args.note or [],
    }
    rows = []
    for slot in slots(spec, only):
        row = run_slot(args, spec, outdir, log, slot, now)
        if row is not None:
            rows.append(row)
            log("  " + ", ".join(f"{k} {v}" for k, v in row.items() if k != "condition"))
    return rows


def run_slot(
    args, spec: dict, outdir: Path, log, slot: schedule.Slot, now: dict, wait=None, run=run_one
) -> dict | None:
    """One run of the schedule after a washout (`wait`, by default `washout.wait_quiet`), with
    its manifest; its summary row, or None if the machine was never quiet enough to run it. A
    run whose directory exists from an earlier attempt runs again, the old directory moved
    under `superseded/`."""
    cond = conds.parse(slot.condition)
    workdir = outdir / slot.label
    if workdir.exists():
        old = outdir / "superseded" / f"{slot.label}-{time.strftime('%Y%m%d-%H%M%S')}"
        old.parent.mkdir(exist_ok=True)
        workdir.rename(old)
    workdir.mkdir(parents=True)
    log(f"{slot.label}: round {slot.round}, seed {slot.seed}: {cond.describe}")
    config = condition_config(spec, cond)
    attempts: list[dict] = []

    def write(valid: bool | None, why: str = "") -> None:
        m = manifest.build(
            spec=spec | now,
            slot=slot,
            condition=cond,
            config=config,
            washout=attempts,
            valid=bool(valid),
            why_invalid=why,
        )
        if valid is None:
            m["valid"] = None
        (workdir / "manifest.json").write_text(json.dumps(m, indent=1))

    def quiet() -> dict:
        if args.drop_caches:
            washout.drop_caches(log)
        if args.no_wait:
            return {"quiet": True, "skipped": True}
        return washout.wait_quiet(spec["machine"]["cpus"], args.max_wait, args.settle, log)

    def invalid(seen: dict, attempt: int) -> None:
        attempts.append(seen | {"attempt": attempt, "ran": False})
        log(
            "  invalid: the machine was not quiet; "
            + ("waiting again" if attempt == 1 else "not run")
        )
        with (outdir / "invalid.jsonl").open("a") as f:
            f.write(json.dumps({"label": slot.label, "attempt": attempt, "seen": seen}) + "\n")

    def go(seen: dict, attempt: int) -> dict:
        attempts.append(seen | {"attempt": attempt, "ran": True})
        write(None)
        return run(args, spec, outdir, workdir, slot, seen)

    row = washout.washed(wait or quiet, go, invalid)
    if row is None:
        write(False, "the machine was not quiet after two washouts")
        return None
    outer = workdir / "outer.json"
    inner_rc = json.loads(outer.read_text()).get("inner_rc", 0) if outer.exists() else 0
    why = run_problem(row, len(spec_jobs(spec, slot.seed)), inner_rc)
    if why:
        log(f"  invalid: {why}")
        with (outdir / "invalid.jsonl").open("a") as f:
            f.write(json.dumps({"label": slot.label, "attempt": len(attempts), "why": why}) + "\n")
    write(not why, why)
    return row


def spec_jobs(spec: dict, seed: int) -> list:
    """Every job of the plan of `seed`, in the run `spec` describes."""
    return [step for steps in spec_plan(spec, seed) for step in steps]


def run_problem(row: dict, planned: int, inner_rc: int) -> str:
    """Why a run that ran is not valid ("": it is): it stopped on an error, the harness failed,
    or fewer or more jobs ran than its plan has."""
    if inner_rc != 0:
        return f"the run exited {inner_rc}"
    if str(row.get("aborted") or "").startswith(HARNESS_ERROR):
        return row["aborted"]
    if row.get("jobs") != planned:
        return f"{row.get('jobs')} jobs ran of the {planned} planned"
    return ""


# ---------------------------------------------------------------------------------------------
# Summaries


def pct(values: list[float], p: float) -> float | None:
    """The nearest-rank `p`th percentile, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _num(v: str | None) -> float | None:
    return None if v in (None, "", "None") else float(v)


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as f:
        return list(csv.DictReader(f))


def summarize_condition(workdir: Path, condition: str) -> dict:
    run = json.loads((workdir / "run.json").read_text()) if (workdir / "run.json").exists() else {}
    outer = json.loads((workdir / "outer.json").read_text())
    jobs = _read_rows(workdir / "jobs.csv")
    samples = _read_rows(workdir / "samples.csv") or _read_rows(workdir / "samples.csv.gz")
    start = run.get("start", 0)
    ok = [j for j in jobs if j["rc"] == "0" and _num(j["ended"]) is not None]
    failed = len(jobs) - len(ok)
    completion = [float(j["done"]) - float(j["issued"]) for j in ok]
    wait = [float(j["started"]) - float(j["issued"]) for j in ok]
    running = [float(j["ended"]) - float(j["started"]) for j in ok]
    ends = [float(j["done"]) for j in jobs]
    same = [float(r["overshoot_us"]) / 1000 for r in _read_rows(workdir / "wakeup_same.csv.gz")]
    out = [float(r["overshoot_us"]) / 1000 for r in _read_rows(workdir / "wakeup_outside.csv.gz")]
    git = [float(r["ms"]) for r in _read_rows(workdir / "git_same.csv.gz")]
    some = [v for r in samples if (v := _num(r["mem_some"])) is not None]
    full = [v for r in samples if (v := _num(r["mem_full"])) is not None]
    # Absent from runs sampled before the cap's own pressure was.
    cap_some = [v for r in samples if (v := _num(r.get("cap_mem_some"))) is not None]
    cap_full = [v for r in samples if (v := _num(r.get("cap_mem_full"))) is not None]
    busy = [float(r["cpu_busy"]) for r in samples]
    steal = [float(r.get("cpu_steal") or 0) for r in samples]
    contained = sum("/lease-" in (j.get("cgroup") or "") for j in jobs)
    ooms = sum(c.get("oom_kill", 0) for c in run.get("cgroups", {}).values())
    # Absent from runs without `park`, and from runs made before it existed.
    parked = run.get("park") or {}
    held = run.get("start_hold") or {}
    swapout = outer.get("pswpout")
    peak = max((c.get("peak") or 0 for c in run.get("cgroups", {}).values()), default=0)

    def r(v, nd=1):
        return None if v is None else round(v, nd)

    return {
        "condition": condition,
        "jobs": len(jobs),
        "failed": failed,
        "makespan_s": r(max(ends) - start if ends and start else None, 0),
        "job_mean_s": r(mean(completion)),
        "job_p95_s": r(pct(completion, 95)),
        "wait_mean_s": r(mean(wait)),
        "wait_p95_s": r(pct(wait, 95)),
        "run_mean_s": r(mean(running)),
        "wake_same_p50_ms": r(pct(same, 50), 2),
        "wake_same_p99_ms": r(pct(same, 99), 2),
        "wake_same_max_ms": r(max(same) if same else None, 0),
        "wake_out_p50_ms": r(pct(out, 50), 2),
        "wake_out_p99_ms": r(pct(out, 99), 2),
        "git_p50_ms": r(pct(git, 50), 0),
        "git_p95_ms": r(pct(git, 95), 0),
        "oom_kills": ooms,
        "oom_kills_machine": outer.get("oom_kill_machine"),
        "swapin_mib": r(outer.get("pswpin", 0) * PAGE / MIB, 0),
        "swapin_gib": r(outer["pswpin"] * PAGE / GIB if "pswpin" in outer else None, 2),
        "swapout_gib": r(swapout * PAGE / GIB if swapout is not None else None, 2),
        "parks": parked.get("parks"),
        "reparks": parked.get("reparks"),
        "spurious_parks": parked.get("spurious"),
        "forced_thaws": parked.get("forced_thaws"),
        "park_s": parked.get("park_s"),
        "reclaimed_gib": r(parked["reclaimed_bytes"] / GIB, 2) if parked else None,
        "park_cap_reached": ("yes" if parked["cap_reached"] else "no") if parked else None,
        "overflow_gib": r(parked["overflowed_bytes"] / GIB, 2) if parked else None,
        "overflow_full": parked.get("swap_max_hits"),
        "starts_held": held.get("held"),
        "start_hold_s": held.get("s"),
        "post_thaw_hold_s": held.get("post_thaw_s"),
        "brake_s": parked.get("brake_s"),
        "mem_some_max": r(max(some) if some else None),
        "mem_some_mean": r(mean(some)),
        "mem_full_max": r(max(full) if full else None),
        "cap_mem_some_max": r(max(cap_some) if cap_some else None),
        "cap_mem_some_mean": r(mean(cap_some)),
        "cap_mem_full_max": r(max(cap_full) if cap_full else None),
        "bench_mem_peak_gib": r(peak / GIB if peak else None, 2),
        "cpu_busy_mean": r(mean(busy), 0),
        "cpu_steal_mean": r(mean(steal)),
        "jobs_in_lease_scope": contained,
        "load_before": outer.get("before", {}).get("load1"),
        "aborted": run.get("aborted") or ("" if run else "no result"),
    }


COLUMNS = [
    ("condition", "condition"),
    ("jobs", "jobs"),
    ("failed", "failed"),
    ("makespan_s", "makespan s"),
    ("job_mean_s", "job mean s"),
    ("job_p95_s", "job p95 s"),
    ("wait_mean_s", "wait mean s"),
    ("run_mean_s", "run mean s"),
    ("wake_same_p50_ms", "wake same p50 ms"),
    ("wake_same_p99_ms", "wake same p99 ms"),
    ("wake_out_p99_ms", "wake outside p99 ms"),
    ("git_p95_ms", "git status p95 ms"),
    ("oom_kills", "OOM kills"),
    ("swapin_mib", "swap-in MiB"),
    ("swapin_gib", "swap-in GiB"),
    ("swapout_gib", "swap-out GiB"),
    ("parks", "parks"),
    ("reparks", "re-parks"),
    ("spurious_parks", "spurious parks"),
    ("forced_thaws", "forced thaws"),
    ("park_s", "parked s"),
    ("reclaimed_gib", "reclaimed GiB"),
    ("park_cap_reached", "park cap reached"),
    ("overflow_gib", "overflow swap GiB"),
    ("overflow_full", "jobs that filled their overflow"),
    ("starts_held", "starts held"),
    ("start_hold_s", "start hold s"),
    ("post_thaw_hold_s", "post-thaw hold s"),
    ("brake_s", "brake s"),
    ("mem_some_max", "mem PSI some max"),
    ("mem_some_mean", "mem PSI some mean"),
    ("mem_full_max", "mem PSI full max"),
    ("cap_mem_some_max", "mem PSI some max in MemoryMax cgroup"),
    ("cap_mem_some_mean", "mem PSI some mean in MemoryMax cgroup"),
    ("cap_mem_full_max", "mem PSI full max in MemoryMax cgroup"),
    ("cpu_busy_mean", "CPU busy %"),
    ("jobs_in_lease_scope", "in lease scope"),
]


def markdown_table(rows: list[dict]) -> str:
    cols = [(k, h) for k, h in COLUMNS if any(r.get(k) is not None for r in rows)]
    lines = ["| " + " | ".join(h for _, h in cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        cells = ["" if row.get(k) is None else str(row[k]) for k, _ in cols]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def completed_rows(outdir: Path, spec: dict) -> list[dict]:
    """The summary row of every run of the schedule that has results, in schedule order."""
    return [
        summarize_condition(outdir / s.label, s.label)
        for s in slots(spec)
        if (outdir / s.label / "outer.json").exists()
    ]


def write_summary(outdir: Path, spec: dict, rows: list[dict]) -> str:
    _write_csv(outdir / "summary.csv", rows)
    m = spec["machine"]
    w = spec["weights"]
    if "seed_base" in spec:
        first = spec["seed_base"] + 1
        plan = spec_plan(spec, first)
        rounds = spec.get("rounds", 1)
        seeds = f"seed {first}" if rounds == 1 else f"seeds {first} to {first + rounds - 1}"
        plan_of = f" (seed {first}: " if rounds > 1 else " ("
    else:
        plan, seeds, plan_of = spec["plan"], f"seed {spec['seed']}", " ("
    invalid = _read_rows_jsonl(outdir / "invalid.jsonl")
    text = "\n".join(
        [
            f"# leasehold benchmark: {m['class']}, {spec['agents']} agents",
            "",
            f"- Machine: {m['cpus']} CPUs, {m['memory_bytes'] / GIB:.1f} GiB memory, "
            f"{m['swap_bytes'] / GIB:.1f} GiB swap; virtualisation: {m['virt']}; "
            f"kernel {m['kernel']}.",
            f"- Plan: {seeds}, {spec['agents']} agents, planned over "
            f"{spec['preset']['duration_s']:g} s, mean think {spec['preset']['think_s']:g} s"
            f"{plan_of}{sum(len(s) for s in plan)} jobs, "
            f"{sum(k == 'mem' for s in plan for _, k in s)} mem).",
            f"- Jobs: cpu {spec['preset']['cpu_s']:g} s alone on every CPU; mem "
            f"{spec['mem_bytes'] / GIB:.2f} GiB touched, then {spec['preset']['mem_s']:g} s "
            "of hashing alone on every CPU.",
            f"- Memory limit on everything the benchmark runs: {spec['cap_bytes'] / GIB:.1f} GiB"
            f" (swap {spec['swap_bytes'] / GIB:.1f} GiB).",
            "- leasehold: `leasehold init --user` config"
            + (
                f" with `[memory] budget = {spec['memory_budget']!r}`"
                if spec.get("memory_budget")
                else ""
            )
            + f", `hold --weight` cpu {w['cpu']}, mem {w['mem']} unless the condition sets `w` "
            f"(`lh-enforced-v020`: 0.2.0's config, weight {WEIGHT_V020}); slice condition "
            f"CPUWeight {spec['slice_weight']}.",
            *([f"- Notes: {'; '.join(spec['notes'])}."] if spec.get("notes") else []),
            "",
            markdown_table(rows),
            "",
            "Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the "
            "harness's scope), `outside` in the session the benchmark was started from.",
            "",
            *(
                [
                    "Invalid (the machine was not quiet before it): "
                    + ", ".join(f"{i['label']} (attempt {i['attempt']})" for i in invalid)
                    + ".",
                    "",
                ]
                if invalid
                else []
            ),
        ]
    )
    (outdir / "summary.md").write_text(text)
    return text


def _read_rows_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------------------------
# The command line


def parse_weights(text: str, init: str) -> dict[str, int]:
    """`cpu=N,mem=M`; a kind left out keeps the built-in weight of a test run (`heavy_weight`
    in the config `init` writes, less the two-at-once weights `conditions.neutral` drops)."""
    heavy = heavy_weight(init)
    out = {"cpu": heavy, "mem": heavy}
    for part in filter(None, text.split(",")):
        key, _, value = part.partition("=")
        if key not in out:
            raise argparse.ArgumentTypeError(f"unknown job kind {key!r}")
        out[key] = int(value)
    return out


#: What a resumed run must be given again exactly as it was first: the schedule, the plan, the
#: sizes (`args`) and the code (`git`, `leasehold_version`).
RESUME_KEYS = (
    "conditions",
    "seed_base",
    "agents",
    "preset_name",
    "workload",
    "args",
    "harness",
)

#: The machine facts a resumed run must find again: its class and what it is made of.
MACHINE_KEYS = ("class", "cpus", "memory_bytes", "swap_bytes", "kernel", "virt")
#: How far `memory_bytes` may move between sessions of one run. A guest booted afresh reports a
#: few KiB more or less MemTotal; the sizes a run uses come from its own flags.
MEMORY_DRIFT = 0.01


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--duration", type=float, help="seconds of planned work per agent")
    seeds = parser.add_mutually_exclusive_group()
    seeds.add_argument("--seed", type=int, default=1, help="round 1's seed (default 1)")
    seeds.add_argument("--seed-base", type=int, help="round N runs the plan of seed SEED_BASE + N")
    preset = parser.add_mutually_exclusive_group()
    preset.add_argument("--quick", action="store_true", help="about 2 minutes a condition")
    preset.add_argument("--tiny", action="store_true", help="a seconds-long smoke run")
    parser.add_argument(
        "--conditions",
        default=",".join(DEFAULT_CONDITIONS),
        help="comma-separated; leasehold's take parameters, e.g. lh-enforced@w=2-4,h=1",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="run every condition this many times, each round on its own seed, in a Williams order",
    )
    parser.add_argument(
        "--round", type=int, help="run only round N of the schedule (into --dir, to resume)"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        help="the results directory itself; one that exists is run into again, with the "
        "schedule, sizes and code it was started with",
    )
    parser.add_argument(
        "--workload",
        help="a name for this mix of jobs, which gate files refer to (default: the preset)",
    )
    parser.add_argument("--mem-gib", type=float, help="a mem job's size (default: 0.7 x RAM / 3)")
    parser.add_argument(
        "--cap-gib",
        type=float,
        help="the memory limit on everything the benchmark runs (default: "
        "the lower of 65%% of RAM and what is available less 2 GiB)",
    )
    parser.add_argument("--swap-gib", type=float, default=2.0)
    parser.add_argument(
        "--p-mem", type=float, help="the share of jobs that are mem jobs (default: the preset's)"
    )
    parser.add_argument(
        "--weights",
        default="",
        help="hold --weight per job kind, as cpu=N,mem=M (default: the built-in weight of a "
        "test run, 4; the bench leaves out the weights init writes to run two at once)",
    )
    parser.add_argument(
        "--memory-budget",
        help="the [memory] budget of the leasehold conditions' config, a size such as 7g "
        "(default: the config's own, auto)",
    )
    parser.add_argument("--slice-weight", type=int, default=20)
    parser.add_argument("--git-repo", help="time `git status` in this repository every 5 s")
    parser.add_argument("--out", type=Path, default=Path("bench-results"))
    parser.add_argument(
        "--machine-class",
        help="the machine's label in results (default: <CPUs>cpu-<GiB>g), e.g. kvm-8cpu-16g",
    )
    parser.add_argument(
        "--note",
        action="append",
        help="a free-form note for every run's manifest, e.g. 'boost off'; may repeat",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="also copy the summary to bench/results/<machine class>/",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    parser.add_argument(
        "--no-scope",
        action="store_true",
        help="run without the benchmark's systemd scope (no memory limit)",
    )
    parser.add_argument("--no-wait", action="store_true", help="do not wait for a quiet machine")
    parser.add_argument("--max-wait", type=float, default=600)
    parser.add_argument("--settle", type=float, default=20)
    parser.add_argument(
        "--drop-caches",
        action="store_true",
        help="empty the page cache before each run's wait (needs root or passwordless sudo)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run although tracked files have uncommitted changes (recorded in spec.json)",
    )
    return parser


def preset_name(args) -> str:
    return "tiny" if args.tiny else "quick" if args.quick else "full"


def run_args(args) -> dict:
    """What decides a run's sizes, from its parsed command line, as `spec.json` records it and
    a gate's `[[workloads]] args` must match: the preset and its duration, the agents, the
    memory sizes, the weights, the memory budget, the slice weight, and whether `git status`
    is timed."""
    weights = args.weights if isinstance(args.weights, str) else ""
    return {
        "preset": preset_name(args),
        "duration_s": args.duration or PRESETS[preset_name(args)].duration_s,
        "agents": args.agents,
        "mem_gib": args.mem_gib,
        "cap_gib": args.cap_gib,
        "swap_gib": args.swap_gib,
        "weights": ",".join(sorted(filter(None, weights.split(",")))),
        "memory_budget": args.memory_budget,
        "slice_weight": args.slice_weight,
        "git_repo": bool(args.git_repo),
    }


def workload_args(text: str) -> dict:
    """`run_args` of the command-line arguments `text`, as a gate file writes them."""
    import shlex

    return run_args(build_parser().parse_args(shlex.split(text)))


def code_version() -> dict:
    """The code a run ran: the checkout's commit, whether its tracked files had uncommitted
    changes, and the leasehold version."""
    git = manifest.git_state()
    return {
        "git_sha": git and git["sha"],
        "git_dirty": git and git["dirty"],
        "leasehold_version": manifest.leasehold_version(),
    }


def harness_settings(args) -> dict:
    """The harness's own settings, which change what a run measures: whether the jobs ran
    without the benchmark's memory limit, without the washout, after dropping the page cache,
    and whether a checkout with uncommitted changes was allowed."""
    return {
        "no_scope": bool(args.no_scope),
        "no_wait": bool(args.no_wait),
        "drop_caches": bool(args.drop_caches),
        "allow_dirty": bool(args.allow_dirty),
    }


def resume(spec_path: Path, fresh: dict, rounds: int, parser) -> dict:
    """The spec of a results directory to run more rounds into, checked against what this
    invocation would have written (`fresh`): the schedule, the sizes and the code must be the
    same."""
    spec = json.loads(spec_path.read_text())
    for key in RESUME_KEYS:
        if spec.get(key) != fresh[key]:
            parser.error(
                f"{spec_path.parent} was started with {key} {spec.get(key)!r}, not "
                f"{fresh[key]!r}; pass the same schedule and sizes to run more of it"
            )
    for key, value in code_version().items():
        if spec.get(key) != value:
            parser.error(
                f"{spec_path.parent} was started with {key} {spec.get(key)!r}; this is "
                f"{value!r}. Run more of it on the code it started with"
            )
    now = fresh["machine"]
    for key in MACHINE_KEYS:
        then, here = spec.get("machine", {}).get(key), now.get(key)
        drifted = (
            key == "memory_bytes" and then and here and abs(here - then) <= MEMORY_DRIFT * then
        )
        if then != here and not drifted:
            parser.error(
                f"{spec_path.parent} was started on a machine with {key} "
                f"{then!r}; this one has {here!r}. Run more "
                "of it on the machine, and with the --machine-class, it started with"
            )
    if (spec.get("rounds", 1) == 1) != (rounds == 1):
        parser.error(f"{spec_path.parent} was started with --rounds {spec.get('rounds', 1)}")
    spec["rounds"] = max(spec.get("rounds", 1), rounds)
    spec_path.write_text(json.dumps(spec, indent=1))
    return spec


#: The only files `--publish` copies: the summaries, which name no path of the machine.
PUBLISHED = ("summary.md", "summary.csv")


def publish(outdir: Path, dest: Path, stem: str) -> list[Path]:
    """Copy the summaries of `outdir` into `dest` as `stem.md` and `stem.csv`; nothing else
    (logs and job rows hold the runtime directory and cgroup paths)."""
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in PUBLISHED:
        target = dest / f"{stem}{Path(name).suffix}"
        shutil.copy(outdir / name, target)
        out.append(target)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_condition"]:
        inner = argparse.ArgumentParser()
        inner.add_argument("--spec", type=Path, required=True)
        inner.add_argument("--condition", required=True)
        inner.add_argument("--dir", type=Path, required=True)
        inner.add_argument("--seed", type=int, required=True)
        inner.add_argument("--git-repo")
        a = inner.parse_args(argv[1:])
        return run_condition(a.spec, a.condition, a.dir, a.seed, a.git_repo)
    if argv[:1] == ["summarize"]:
        outdir = Path(argv[1])
        spec = json.loads((outdir / "spec.json").read_text())
        print(write_summary(outdir, spec, completed_rows(outdir, spec)))
        return 0
    if argv[:1] == ["compare"]:
        import gate

        return gate.main(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    sizes = run_args(args)

    try:
        conditions = conds.parse_list(args.conditions)
    except ValueError as exc:
        parser.error(f"--conditions: {exc}")
    if not conditions:
        parser.error("--conditions: none given")
    if any(c.elastic for c in conditions) and not elastic_supported():
        parser.error(
            "lh-elastic needs a leasehold with elastic leases (hold --elastic); the installed "
            f"leasehold {manifest.leasehold_version()} has none"
        )
    if args.machine_class and not _CLASS.match(args.machine_class):
        parser.error("--machine-class: letters, digits, '.', '_' and '-' only")
    if args.rounds < 1:
        parser.error("--rounds: at least 1")
    if args.round is not None and not 1 <= args.round <= args.rounds:
        parser.error(f"--round: one of 1 to --rounds ({args.rounds})")
    if args.round is not None and not args.dir and not args.dry_run:
        parser.error("--round needs --dir, the results directory every round goes into")
    if args.tiny:
        args.no_wait = True
    if not args.dry_run and not args.no_wait:
        washout.check_probes()
    if args.drop_caches:
        washout.check_drop_caches()
    # A results directory (--dir, --round) is what a gate judges, whatever the preset; only
    # a throwaway --tiny smoke run into --out is exempt.
    resumable = args.dir is not None or args.round is not None
    if not args.dry_run and (resumable or not args.tiny) and not args.allow_dirty:
        git = manifest.git_state()
        if git and git["dirty"]:
            parser.error(
                "tracked files have uncommitted changes, so the commit in the results would not "
                "be the code that ran; commit them, or pass --allow-dirty (recorded)"
            )
    init = init_config()
    try:
        args.weights = parse_weights(args.weights, init)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(f"--weights: {exc}")
    name = preset_name(args)
    base = PRESETS[name]
    preset = Preset(
        **(
            asdict(base)
            | ({"duration_s": args.duration} if args.duration else {})
            | ({"p_mem": args.p_mem} if args.p_mem is not None else {})
        )
    )
    seed_base = args.seed_base if args.seed_base is not None else args.seed - 1
    names = [c.text for c in conditions]
    m = machine(args.machine_class)
    mem = meminfo()
    mem_bytes = int(args.mem_gib * GIB) if args.mem_gib else int(0.7 * m["memory_bytes"] / 3)
    if args.tiny:
        mem_bytes = int(args.mem_gib * GIB) if args.mem_gib else 16 * MIB
    cap = (
        int(args.cap_gib * GIB)
        if args.cap_gib
        else int(min(0.65 * m["memory_bytes"], mem["MemAvailable"] - 2 * GIB))
    )
    # Swap the benchmark may use: what was asked, leaving the session at least half the free
    # swap or 3 GiB of it, whichever is less.
    free_swap = mem.get("SwapFree", 0)
    swap = int(min(args.swap_gib * GIB, max(free_swap / 2, free_swap - 3 * GIB)))
    if swap < args.swap_gib * GIB:
        print(
            f"--swap-gib {args.swap_gib:g}: only {swap / GIB:.2f} GiB of swap is free to use",
            file=sys.stderr,
        )
    for c in conditions:
        try:
            condition_config(
                {
                    "init_config": init,
                    "machine": m,
                    "weights": args.weights,
                    "mem_bytes": mem_bytes,
                    "memory_budget": args.memory_budget,
                },
                c,
            )
        except ValueError as exc:
            parser.error(f"--conditions: {c.text}: {exc}")

    first = {"conditions": names, "rounds": args.rounds, "seed_base": seed_base}
    if args.dry_run:
        rate, touch = 1e9, 2.0  # fixed: the plan must not depend on this machine's speed
        nominal = {"cpu": preset.cpu_s, "mem": preset.mem_s + touch * mem_bytes / GIB}
        print(
            json.dumps(
                {
                    "seed": seed_base + (args.round or 1),
                    "agents": args.agents,
                    "preset": asdict(preset),
                    "nominal_s": nominal,
                    "plan": make_plan(seed_base + (args.round or 1), args.agents, preset, nominal),
                    "schedule": [asdict(s) for s in slots(first, args.round)],
                    "args": sizes,
                }
            )
        )
        return 0

    fresh = {
        "conditions": names,
        "seed_base": seed_base,
        "agents": args.agents,
        "preset_name": name,
        "workload": args.workload or name,
        "args": sizes,
        "harness": harness_settings(args),
    }
    if args.dir and (args.dir / "spec.json").exists():
        outdir = args.dir
        spec = resume(outdir / "spec.json", fresh | {"machine": m}, args.rounds, parser)
        print(f"results in {outdir} (run again with the schedule it was started with)", flush=True)
    else:
        if args.tiny:
            rate, touch = 1e9, 2.0
        else:
            print("calibrating...", flush=True)
            rate, touch = calibrate(m["cpus"])
        nominal = {"cpu": preset.cpu_s, "mem": preset.mem_s + touch * mem_bytes / GIB}
        work = {
            "cpu": max(1, int(rate * m["cpus"] * preset.cpu_s)),
            "mem": max(0, int(rate * m["cpus"] * preset.mem_s)),
        }
        stamp = time.strftime("%Y%m%d-%H%M%S")
        outdir = args.dir or (
            args.out / f"{m['class']}-k{args.agents}-{name}-d{preset.duration_s:g}-{stamp}"
        )
        outdir.mkdir(parents=True, exist_ok=True)
        spec = (
            fresh
            | code_version()
            | {
                "git_repo": sizes["git_repo"],
                "machine": m,
                "seed": seed_base + 1,
                "preset": asdict(preset),
                "rounds": args.rounds,
                "nominal_s": nominal,
                "rate_bytes_per_cpu_s": rate,
                "touch_s_per_gib": touch,
                "work": work,
                "mem_bytes": mem_bytes,
                "cap_bytes": cap,
                "swap_bytes": swap,
                "weights": args.weights,
                "memory_budget": args.memory_budget,
                "slice_weight": args.slice_weight,
                "init_config": init,
                "notes": args.note or [],
                "abort_available_bytes": 512 * MIB,
                "job_timeout_s": max(60.0, 40 * max(nominal.values())),
            }
        )
        (outdir / "spec.json").write_text(json.dumps(spec, indent=1))
        print(f"results in {outdir}", flush=True)

    def log(message: str) -> None:
        print(message, flush=True)
        with (outdir / "log.txt").open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    # A stopped benchmark still takes down the condition it was running (run_one's finally).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    run_all(args, spec, outdir, log, args.round)
    text = write_summary(outdir, spec, completed_rows(outdir, spec))
    print(text)
    if args.publish:
        dest = HERE / "results" / spec["machine"]["class"]
        stem = f"k{spec['agents']}-{spec['preset_name']}-d{spec['preset']['duration_s']:g}"
        publish(outdir, dest, stem)
        print(f"summary copied to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
