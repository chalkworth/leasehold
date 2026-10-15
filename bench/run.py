"""The leasehold benchmark: several coding agents each starting heavy jobs on one Linux machine,
run under each way of keeping the machine usable that a skeptic would name.

    uv run python bench/run.py --quick                # about 2 minutes a condition
    uv run python bench/run.py --agents 8 --duration 300
    uv run python bench/run.py --dry-run              # print the seeded schedule and stop
    uv run python bench/run.py summarize DIR          # rebuild the summary of a finished run

Each of K simulated agents works through its own seeded list of steps: think for a random
(exponential) time, then run one job and wait for it, as an agent waits for its tool call. The
jobs (`bench/job.py`) are either `cpu` (hashing spread over as many workers as the job may use)
or `mem` (the same, over a large block of touched memory). Every condition runs the same lists,
so every condition does the same work; only how the jobs are started changes:

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
  for this machine (no memory counted) with every job at 0.2.0's built-in weight, 4.

leasehold's conditions run a private user-mode daemon on a socket of their own, with the
config `leasehold init --user` writes for this machine (its `[memory] budget` replaced by
`--memory-budget` when given), and `hold --weight` as the built-in `[commands]` rules weigh a
test run in it (`--weights`, by default the config's `heavy_weight`).

Safety: everything the benchmark starts is held to one memory limit (`MemoryMax`, with a
bounded `MemorySwapMax`): the harness's own scope for the conditions whose jobs run in it, and
the slice the jobs run in for `slice` and `lh-enforced`. Running out of that memory makes the
kernel kill a job (jobs raise their OOM score), never the person's session. The run also stops
every job if the machine itself runs short of memory.

What is measured, per condition: makespan; each job's completion time from when its agent asked
(and, under leasehold, the wait before it started); a 10 ms wakeup probe in the jobs' own cgroup
(a shell in the agents' terminal, an editor running the agent) and one in the session the
benchmark was started from; optionally `git status` time; OOM kills; swap-in; memory pressure.
Raw rows go to CSV files beside a summary table.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import gzip
import json
import math
import os
import platform
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
JOB = HERE / "job.py"
PROBE = HERE / "probe.py"
GIB = 1 << 30
MIB = 1 << 20

CONDITIONS = {
    "unmanaged": "jobs started as they are, in the same cgroup as the person's shell",
    "nice": "jobs under nice -n 19, same cgroup",
    "slice": "jobs in one systemd user slice with a low CPUWeight",
    "lh-advisory": "leasehold hold, containment off: lease and job count only",
    "lh-enforced": "leasehold hold, containment on: weighted scope per lease",
    "lh-advisory-greedy": "as lh-advisory, jobs ignore the job count and use every CPU",
    "lh-enforced-greedy": "as lh-enforced, jobs ignore the job count and use every CPU",
    "lh-hardcpu-greedy": "as lh-enforced-greedy, with [contain] hard_cpu = true",
    "static-jobs": "unmanaged, each job told the job count its lease would give",
    "lh-enforced-declared": "as lh-enforced, mem jobs declare their memory (hold --memory)",
    "lh-enforced-v020": "as lh-enforced, on leasehold 0.2.0's generated config and weights",
}
#: The built-in weight of a test run or build in leasehold 0.2.0.
WEIGHT_V020 = 4
#: What `lh-enforced-declared` declares for a mem job beyond the block it touches.
DECLARED_EXTRA = 512 * (1 << 20)
DEFAULT_CONDITIONS = list(CONDITIONS)


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


def psi(resource: str) -> dict[str, float]:
    """`some_avg10`, `full_avg10` of /proc/pressure/RESOURCE (missing: empty)."""
    out = {}
    try:
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
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


def machine() -> dict:
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
        "class": f"{cpus}cpu-{round(mem['MemTotal'] / GIB)}g",
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


class Setup:
    """How one condition starts a job, and what it sets up and takes down around the run."""

    def __init__(self, condition: str, spec: dict, workdir: Path) -> None:
        self.condition = condition
        self.spec = spec
        self.workdir = workdir
        self.token = os.urandom(3).hex()
        self.slice: str | None = None
        self.daemon: subprocess.Popen | None = None
        self.sockdir: Path | None = None
        self.env = clean_env()
        self.leasehold = condition.startswith("lh-")
        self.greedy = condition.endswith("-greedy")
        self.hard_cpu = condition == "lh-hardcpu-greedy"
        self.v020 = condition == "lh-enforced-v020"
        self.declared = condition == "lh-enforced-declared"

    def start(self) -> None:
        caps = [f"MemoryMax={self.spec['cap_bytes']}", f"MemorySwapMax={self.spec['swap_bytes']}"]
        if self.condition == "slice":
            self.slice = f"lhbenchjobs_{self.token}.slice"
            weight = f"CPUWeight={self.spec['slice_weight']}"
            self._set_property(self.slice, weight, *caps)
        elif self.condition.startswith(("lh-enforced", "lh-hardcpu")):
            # A session slice as `leasehold run` makes, under leasehold.slice; it carries the
            # benchmark's memory limit, as the harness's scope does for the other conditions.
            self.slice = f"leasehold-bench_{self.token}.slice"
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
        config = self.workdir / "machine.toml"
        text = subprocess.run(
            [*leasehold_cli(), "init", "--user", "--print"],
            capture_output=True,
            text=True,
            check=True,
            env=self.env,
        ).stdout
        marker = 'mode = "user"'
        if marker not in text:
            raise SystemExit("leasehold init --print gave a config the benchmark cannot place")
        if self.v020:
            text = config_v020(text)
        elif self.spec.get("memory_budget"):
            line = 'budget = "auto"'
            if line not in text:
                raise SystemExit("leasehold init --print has no [memory] budget to replace")
            text = text.replace(line, f'budget = "{self.spec["memory_budget"]}"', 1)
        ledger = self.workdir / "ledger"
        text = text.replace(marker, f'{marker}\nledger_dir = "{ledger}"', 1)
        if self.hard_cpu:
            if "hard_cpu = false" not in text:
                raise SystemExit("leasehold init --print has no hard_cpu line to turn on")
            text = text.replace("hard_cpu = false", "hard_cpu = true", 1)
        config.write_text(text)
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
            weight = WEIGHT_V020 if self.v020 else self.spec["weights"][kind]
            # A size rounded up past the job's touched block, as a person declares one: the job's
            # interpreter and workers need a little more, and MemoryHigh is set to it.
            size = self.spec["mem_bytes"] + DECLARED_EXTRA
            declared = ["--memory", f"{size}b"] if self.declared else []
            return [
                *leasehold_cli(),
                "hold",
                "--socket",
                self.sock,
                "--weight",
                str(weight),
                *(declared if kind == "mem" else []),
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
    """The config `leasehold init --user` writes for this machine."""
    return subprocess.run(
        [*leasehold_cli(), "init", "--user", "--print"],
        capture_output=True,
        text=True,
        check=True,
        env=clean_env(),
    ).stdout


def config_v020(text: str) -> str:
    """What leasehold 0.2.0's `init --user` wrote for the machine `text` (today's config for it)
    describes: the same name, slots, CPUs, per-lease memory and description."""
    import tomllib

    from leasehold.template import render_user_0_2

    raw = tomllib.loads(text)
    about = text.splitlines()[1].removeprefix("# ")
    c = raw["ceiling"]
    return render_user_0_2(
        name=raw["name"], slots=c["slots"], cpus=c["cpus"], memory=c["memory"], about=about,
        release="0.2.0",
    )  # fmt: skip


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

    def agent(self, index: int, steps: list) -> None:
        for seq, (think, kind) in enumerate(steps):
            if self.stop.wait(think):
                return
            argv = self.setup.argv(index, kind)
            issued = time.time()
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
                elif parts[:2] == ["BENCH", "end"]:
                    ended = float(parts[2])
            code = proc.wait()
            done = time.time()
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

    def sampler(self) -> None:
        """Once a second: the machine's CPU, memory, swap and pressure, and the benchmark's own
        memory; stops everything if the machine itself runs short of memory, and kills a job
        that has run far past any reasonable length."""
        busy0, total0, steal0 = cpu_times()
        mine, jobs = own_cgroup(), self.setup.job_cgroup()
        floor = self.spec["abort_available_bytes"]
        short_since: float | None = None
        limit = self.spec["job_timeout_s"]
        while not self.stop.wait(1.0):
            busy, total, steal = cpu_times()
            mem, vm, pm, pc = meminfo(), vmstat(), psi("memory"), psi("cpu")
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
        with self.lock:
            procs = [p for p, _ in self.live.values()]
        for proc in procs:
            _kill_group(proc, signal.SIGTERM)
        time.sleep(5)
        for proc in procs:
            _kill_group(proc, signal.SIGKILL)
        self.setup.kill_jobs()

    def run(self, plan: list[list]) -> dict:
        start = time.time()
        sampler = threading.Thread(target=self.sampler, daemon=True)
        sampler.start()
        agents = [
            threading.Thread(target=self.agent, args=(i, steps), daemon=True)
            for i, steps in enumerate(plan)
        ]
        for t in agents:
            t.start()
        for t in agents:
            t.join()
        end = time.time()
        self.stop.set()
        sampler.join()
        return {"start": start, "end": end, "aborted": self.aborted}


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


def run_condition(spec_path: Path, condition: str, workdir: Path) -> int:
    """One condition, from inside the benchmark's own scope: set up, run the plan with the
    in-cgroup probes alongside, take down, and write jobs.csv, samples.csv and run.json."""
    spec = json.loads(spec_path.read_text())
    setup = Setup(condition, spec, workdir)
    probes: list[subprocess.Popen] = []
    try:
        setup.start()
        probes.append(_probe("wakeup", workdir / "wakeup_same.csv.gz"))
        if spec.get("git_repo"):
            probes.append(_probe("git", workdir / "git_same.csv.gz", "--repo", spec["git_repo"]))
        run = Run(setup, spec, workdir)
        events_before = {}
        jobs_cg = setup.job_cgroup()
        cgroups = {"harness": own_cgroup(), **({"jobs": jobs_cg} if jobs_cg else {})}
        for name, cg in cgroups.items():
            events_before[name] = read_kv(cg / "memory.events")
        result = run.run(spec["plan"])
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
        (workdir / "run.json").write_text(json.dumps(result, indent=1))
    finally:
        for proc in probes:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(30)
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


def wait_quiet(cpus: int, max_wait_s: float, settle_s: float, log) -> dict:
    """Wait (at most `max_wait_s`) until the machine looks idle: load under a quarter of the
    CPUs (at least 2), CPU pressure under 10%, memory pressure under 1%. What it saw."""
    time.sleep(settle_s)
    deadline = time.monotonic() + max_wait_s
    while True:
        load = float(Path("/proc/loadavg").read_text().split()[0])
        cpu, mem = psi("cpu").get("some_avg10", 0), psi("memory").get("some_avg10", 0)
        quiet = load <= max(2, cpus / 4) and cpu < 10 and mem < 1
        if quiet or time.monotonic() > deadline:
            seen = {"load1": load, "cpu_some": cpu, "mem_some": mem, "quiet": quiet}
            log(
                f"  machine before: load {load:.2f}, cpu some {cpu}%, mem some {mem}%"
                + ("" if quiet else " (not quiet; going ahead)")
            )
            return seen
        time.sleep(5)


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


def runs(conditions: list[str], rounds: int) -> list[tuple[str, str]]:
    """Each run as (condition, label): the conditions `rounds` times, every other round in
    reverse so that no condition always runs first or last; labelled `<condition>-r<n>` when
    there is more than one round."""
    out = []
    for n in range(1, rounds + 1):
        order = conditions if n % 2 else conditions[::-1]
        out += [(c, c if rounds == 1 else f"{c}-r{n}") for c in order]
    return out


def run_all(args, spec: dict, outdir: Path, log) -> list[dict]:
    rows = []
    vm0 = None
    for condition, label in runs(spec["conditions"], spec.get("rounds", 1)):
        workdir = outdir / label
        workdir.mkdir(parents=True, exist_ok=True)
        log(f"{label}: {CONDITIONS[condition]}")
        before = (
            wait_quiet(spec["machine"]["cpus"], args.max_wait, args.settle, log)
            if not args.no_wait
            else {}
        )
        outside = _probe("wakeup", workdir / "wakeup_outside.csv.gz")
        vm0 = vmstat()
        unit = f"lhbench-{condition}-{os.urandom(3).hex()}.scope"
        inner = [
            sys.executable,
            str(Path(__file__).resolve()),
            "_condition",
            "--spec",
            str(outdir / "spec.json"),
            "--condition",
            condition,
            "--dir",
            str(workdir),
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
        row = summarize_condition(workdir, label)
        rows.append(row)
        log("  " + ", ".join(f"{k} {v}" for k, v in row.items() if k != "condition"))
    return rows


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


def _num(v: str) -> float | None:
    return None if v in ("", "None") else float(v)


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
    busy = [float(r["cpu_busy"]) for r in samples]
    steal = [float(r.get("cpu_steal") or 0) for r in samples]
    contained = sum("/lease-" in (j.get("cgroup") or "") for j in jobs)
    ooms = sum(c.get("oom_kill", 0) for c in run.get("cgroups", {}).values())
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
        "swapin_mib": r(outer.get("pswpin", 0) * 4096 / MIB, 0),
        "mem_some_max": r(max(some) if some else None),
        "mem_some_mean": r(mean(some)),
        "mem_full_max": r(max(full) if full else None),
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
    ("mem_some_max", "mem PSI some max"),
    ("mem_some_mean", "mem PSI some mean"),
    ("mem_full_max", "mem PSI full max"),
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


def write_summary(outdir: Path, spec: dict, rows: list[dict]) -> str:
    _write_csv(outdir / "summary.csv", rows)
    m = spec["machine"]
    w = spec["weights"]
    text = "\n".join(
        [
            f"# leasehold benchmark: {m['class']}, {spec['agents']} agents",
            "",
            f"- Machine: {m['cpus']} CPUs, {m['memory_bytes'] / GIB:.1f} GiB memory, "
            f"{m['swap_bytes'] / GIB:.1f} GiB swap; virtualisation: {m['virt']}; "
            f"kernel {m['kernel']}.",
            f"- Plan: seed {spec['seed']}, {spec['agents']} agents, planned over "
            f"{spec['preset']['duration_s']:g} s, mean think {spec['preset']['think_s']:g} s, "
            f"{sum(len(s) for s in spec['plan'])} jobs "
            f"({sum(k == 'mem' for s in spec['plan'] for _, k in s)} mem).",
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
            + f", `hold --weight` cpu {w['cpu']}, mem {w['mem']} "
            f"(`lh-enforced-v020`: 0.2.0's config, weight {WEIGHT_V020}); slice condition "
            f"CPUWeight {spec['slice_weight']}.",
            "",
            markdown_table(rows),
            "",
            "Wakeup latency is how late a 10 ms sleep wakes: `same` in the jobs' own cgroup (the "
            "harness's scope), `outside` in the session the benchmark was started from.",
            "",
        ]
    )
    (outdir / "summary.md").write_text(text)
    return text


# ---------------------------------------------------------------------------------------------
# The command line


def parse_weights(text: str) -> dict[str, int]:
    """`cpu=N,mem=M`; a kind left out keeps the built-in weight of a test run (`heavy_weight`
    in the config `init` writes)."""
    heavy = heavy_weight(init_config())
    out = {"cpu": heavy, "mem": heavy}
    for part in filter(None, text.split(",")):
        key, _, value = part.partition("=")
        if key not in out:
            raise argparse.ArgumentTypeError(f"unknown job kind {key!r}")
        out[key] = int(value)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_condition"]:
        inner = argparse.ArgumentParser()
        inner.add_argument("--spec", type=Path, required=True)
        inner.add_argument("--condition", required=True)
        inner.add_argument("--dir", type=Path, required=True)
        a = inner.parse_args(argv[1:])
        return run_condition(a.spec, a.condition, a.dir)
    if argv[:1] == ["summarize"]:
        outdir = Path(argv[1])
        spec = json.loads((outdir / "spec.json").read_text())
        rows = [
            summarize_condition(outdir / label, label)
            for _c, label in runs(spec["conditions"], spec.get("rounds", 1))
            if (outdir / label / "outer.json").exists()
        ]
        print(write_summary(outdir, spec, rows))
        return 0

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--duration", type=float, help="seconds of planned work per agent")
    parser.add_argument("--seed", type=int, default=1)
    preset = parser.add_mutually_exclusive_group()
    preset.add_argument("--quick", action="store_true", help="about 2 minutes a condition")
    preset.add_argument("--tiny", action="store_true", help="a seconds-long smoke run")
    parser.add_argument("--conditions", default=",".join(DEFAULT_CONDITIONS))
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="run the conditions this many times on one plan, every other round in reverse",
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
        "--weights",
        default="",
        help="hold --weight per job kind, as cpu=N,mem=M (default: the built-in weight of a "
        "test run in the config init writes, its heavy_weight)",
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
    args = parser.parse_args(argv)

    try:
        args.weights = parse_weights(args.weights)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.error(f"--weights: {exc}")
    conditions = [c for c in args.conditions.split(",") if c]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        parser.error(f"unknown conditions: {', '.join(unknown)}")
    name = "tiny" if args.tiny else "quick" if args.quick else "full"
    base = PRESETS[name]
    preset = Preset(**(asdict(base) | ({"duration_s": args.duration} if args.duration else {})))
    m = machine()
    mem = meminfo()
    mem_bytes = int(args.mem_gib * GIB) if args.mem_gib else int(0.7 * m["memory_bytes"] / 3)
    if args.tiny:
        mem_bytes = int(args.mem_gib * GIB) if args.mem_gib else 16 * MIB
    cap = (
        int(args.cap_gib * GIB)
        if args.cap_gib
        else int(min(0.65 * m["memory_bytes"], mem["MemAvailable"] - 2 * GIB))
    )
    swap = int(min(args.swap_gib * GIB, mem.get("SwapFree", 0) / 2))

    if args.dry_run or args.tiny:
        rate, touch = 1e9, 2.0  # fixed: the plan must not depend on this machine's speed
    else:
        print("calibrating...", flush=True)
        rate, touch = calibrate(m["cpus"])
    nominal = {"cpu": preset.cpu_s, "mem": preset.mem_s + touch * mem_bytes / GIB}
    plan = make_plan(args.seed, args.agents, preset, nominal)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "seed": args.seed,
                    "agents": args.agents,
                    "preset": asdict(preset),
                    "nominal_s": nominal,
                    "plan": plan,
                }
            )
        )
        return 0

    work = {
        "cpu": max(1, int(rate * m["cpus"] * preset.cpu_s)),
        "mem": max(0, int(rate * m["cpus"] * preset.mem_s)),
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = args.out / f"{m['class']}-k{args.agents}-{name}-d{preset.duration_s:g}-{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    spec = {
        "machine": m,
        "seed": args.seed,
        "agents": args.agents,
        "preset_name": name,
        "preset": asdict(preset),
        "conditions": conditions,
        "rounds": args.rounds,
        "plan": plan,
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
        "git_repo": args.git_repo,
        "abort_available_bytes": 512 * MIB,
        "job_timeout_s": max(60.0, 40 * max(nominal.values())),
    }
    (outdir / "spec.json").write_text(json.dumps(spec, indent=1))
    print(f"results in {outdir}", flush=True)

    def log(message: str) -> None:
        print(message, flush=True)
        with (outdir / "log.txt").open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    if args.tiny:
        args.no_wait = True
    # A stopped benchmark still takes down the condition it was running (run_all's finally).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    rows = run_all(args, spec, outdir, log)
    text = write_summary(outdir, spec, rows)
    print(text)
    if args.publish:
        dest = HERE / "results" / m["class"]
        dest.mkdir(parents=True, exist_ok=True)
        stem = f"k{args.agents}-{name}-d{preset.duration_s:g}"
        shutil.copy(outdir / "summary.md", dest / f"{stem}.md")
        shutil.copy(outdir / "summary.csv", dest / f"{stem}.csv")
        print(f"summary copied to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
