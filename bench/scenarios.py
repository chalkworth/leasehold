"""Short, product-shaped memory scenarios: each answers one question about leasehold's memory
admission and memory net in a minute or two, on a private user-mode daemon of its own.

    python -m bench.scenarios list
    python -m bench.scenarios run NAME [--arm KEY=VALUE,...] [--scale X] [--seed N] [--out DIR]
    python -m bench.scenarios summary DIR...

A scenario is a memory limit (a session slice with `MemoryMax` and `MemorySwapMax`, as
`leasehold run` makes), a daemon config, and a timed list of jobs (`bench/memjob.py`), each run
through `leasehold hold`. Sizes are in MiB at scale 1; `--scale` multiplies every size, so the
same case runs small on a laptop and larger on a lab machine.

An arm changes the daemon's config: `net=park|off`, `overflow=SIZE|max`, `write_per_day=SIZE`,
`budget=SIZE|cap` (cap: the scenario's limit), `aps=SIZE`, `declare=X` (every job declares X
times its real size, per slot; `none`: no job declares), `weight=N` (every job asks N slots),
`slots=N`, `tune=NAME:VALUE;...` (constants of `leasehold.net`), `cap=MiB` and `swap=MiB`
(the limit's, at scale 1).

Each run writes, under its directory: `jobs.csv` (each job's times and exit code), `events.jsonl`
(the daemon's events, with their times), `samples.csv` (the limit's memory and swap, and the
machine's swap writes, every 0.2 s), `scopes.csv` (each lease scope's memory and swap),
`status.json` (the daemon's last status), `daemon.log` and `summary.json`.

Swap matters to most cases: on a lab machine, back it with RAM (zram) and fill memory with data
that does not compress, which `memjob.py` does, so iterations wear no disk.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
MEMJOB = HERE / "memjob.py"
MIB = 1 << 20


@dataclass
class Job:
    at: float  # seconds after the scenario starts
    size: float  # MiB at scale 1
    weight: int = 3
    ramp: float = 2.0
    hold: float = 8.0
    workers: int = 2
    shared: bool = False
    write: bool = False
    late: float = 0.0  # MiB added late
    late_at: float = 0.0
    #: What the job declares per slot, as a multiple of its real size a slot (None: the
    #: arm's `declare`, else nothing).
    declare: float | None = None
    heavy: bool = True
    cache: float = 0.0  # MiB of a file the job keeps reading (page cache)


@dataclass
class Scenario:
    name: str
    question: str
    cap: float  # MiB
    swap: float  # MiB
    jobs: list[Job]
    slots: int = 6
    budget: str = "cap"
    aps: str = "auto"
    restart_at: float | None = None
    timeout: float = 240.0
    net: dict = field(default_factory=lambda: {"mode": "park"})


def _mix(seed: int) -> list[Job]:
    """Builds and tests: heavy jobs of 0.5 to 1.1 GiB with a spread, light ones of 100 MiB,
    arriving over 90 s."""
    rnd = random.Random(seed)
    jobs, t = [], 0.0
    while t < 90:
        t += rnd.expovariate(1 / 6)
        if rnd.random() < 0.55:
            size = rnd.uniform(500, 1100)
            jobs.append(
                Job(
                    at=round(t, 1),
                    size=round(size),
                    ramp=rnd.choice([1, 3, 8]),
                    hold=rnd.uniform(6, 14),
                    workers=rnd.choice([1, 2, 3]),
                    shared=rnd.random() < 0.5,
                    declare=None,
                )
            )
        else:
            jobs.append(Job(at=round(t, 1), size=100, weight=2, ramp=1, hold=6, heavy=False))
    return jobs


def scenarios(seed: int = 0) -> dict[str, Scenario]:
    S = Scenario
    return {
        s.name: s
        for s in [
            S(
                "lone-edge",
                "A lone job a little over the limit: does the net leave it alone (nothing to park "
                "it for), and does it finish in swap?",
                cap=1536, swap=1024, jobs=[Job(0, 1700, weight=6, ramp=4, hold=10)],
            ),
            S(
                "pair-edge",
                "Two jobs that together need 5% more than the limit: one park, or a short spill?",
                cap=1536, swap=1024,
                jobs=[Job(0, 800, ramp=3, hold=12), Job(1, 800, ramp=3, hold=12)],
            ),
            S(
                "under15",
                "Jobs declare 15% under what they use, three at weight 2 on a limit that "
                "fits three declared: what does the net do?",
                cap=2048, swap=1024,
                jobs=[Job(i * 2.0, 750, weight=2, ramp=3, hold=10, declare=0.85)
                      for i in range(6)],
            ),
            S(
                "slow-spill",
                "Two jobs whose sum crosses the limit slowly (over 20 s): caught by the slow "
                "window?",
                cap=1536, swap=1024,
                jobs=[Job(0, 700, ramp=2, hold=25), Job(1, 1100, ramp=20, hold=8)],
            ),
            S(
                "fast-spill",
                "A job that fills 1 GiB at once beside one already at 900 MiB.",
                cap=1536, swap=1024,
                jobs=[Job(0, 900, ramp=1, hold=20), Job(4, 1000, ramp=0, hold=8)],
            ),
            S(
                "stale-slots",
                "The bench's kill: a job parked and resumed keeps its swap slots (forked workers "
                "share its memory), then a new job starts once it settles.",
                cap=1536, swap=640,
                jobs=[Job(0, 900, ramp=1, hold=14, workers=3, shared=True),
                      Job(2, 900, ramp=1, hold=30, workers=3, shared=True),
                      Job(20, 900, ramp=1, hold=10, workers=3, shared=True),
                      Job(24, 900, ramp=1, hold=10, workers=3, shared=True)],
            ),
            S(
                "small-swap",
                "Swap too small to take a parked job whole (cap swap 400 MiB, jobs 900 MiB).",
                cap=1536, swap=400,
                jobs=[Job(0, 900, ramp=1, hold=15), Job(2, 900, ramp=2, hold=15),
                      Job(10, 900, ramp=1, hold=10)],
            ),
            S(
                "never-ends",
                "A peer that runs on for the whole scenario while the other is parked: the parked "
                "job waits; how long is the rest of the queue held?",
                cap=1536, swap=1024,
                jobs=[Job(0, 900, ramp=1, hold=80), Job(2, 900, ramp=2, hold=10),
                      Job(5, 300, weight=1, ramp=1, hold=5), Job(8, 300, weight=1, ramp=1, hold=5)],
                timeout=150,
            ),
            S(
                "restart",
                "The daemon restarts while a job is parked: is it adopted, resumed and held for?",
                cap=1536, swap=1024,
                jobs=[Job(0, 900, ramp=1, hold=20), Job(2, 900, ramp=1, hold=15),
                      Job(12, 900, ramp=1, hold=8)],
                restart_at=8.0,
            ),
            S(
                "cache-spill",
                "Two jobs whose anonymous memory fits but whose page cache does not: does the "
                "net park for memory the kernel could drop?",
                cap=1536, swap=1024,
                jobs=[Job(0, 600, ramp=2, hold=20, cache=500), Job(2, 600, ramp=2, hold=20,
                      cache=500)],
            ),
            S(
                "mix",
                "Builds and tests: heavy jobs of 0.5 to 1.1 GiB (undeclared), light ones, over 90 "
                "s, on a 2 GiB limit.",
                cap=2048, swap=1024, jobs=_mix(seed), timeout=300,
            ),
        ]
    }  # fmt: skip


# ---------------------------------------------------------------------------------------------


def _size(mib: float) -> str:
    return f"{max(1, round(mib))}m"


def _arm(text: str) -> dict[str, str]:
    out = {}
    for part in filter(None, text.split(",")):
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def config_text(sc: Scenario, arm: dict, scale: float, ledger: Path) -> str:
    cap = float(arm.get("cap", sc.cap)) * scale
    budget = arm.get("budget", sc.budget)
    budget = _size(cap) if budget == "cap" else budget
    slots = int(arm.get("slots", sc.slots))
    net = dict(sc.net)
    if "net" in arm:
        net["mode"] = arm["net"]
    for key in ("overflow", "write_per_day"):
        if key in arm:
            net[key] = arm[key]
    lines = [
        'name = "scenario"',
        'mode = "user"',
        f'ledger_dir = "{ledger}"',
        "[ceiling]",
        f"slots = {slots}",
        f"cpus = {slots}",
        f'memory = "{_size(cap)}"',
        "[pools.work]",
        "reserved = 0",
        f"max = {slots}",
        "[kinds]",
        'work = "work"',
        "[lend]",
        "pools = []",
        "[contain]",
        'mode = "auto"',
        "[memory]",
        f'budget = "{budget}"',
        'kept = "0b"',
        f'assumed_per_slot = "{arm.get("aps", sc.aps)}"',
        'learn = "off"',
        "[guard]",
        "memory_pressure_on = 100.0",
        "memory_pressure_off = 99.0",
        "[memory_net]",
        *(f'{k} = "{v}"' for k, v in net.items()),
    ]
    return "\n".join(lines) + "\n"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def _cgroup_of_slice(name: str) -> Path:
    done = _systemctl("show", "-P", "ControlGroup", name)
    return Path("/sys/fs/cgroup" + done.stdout.strip())


def _int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _kv(path: Path) -> dict[str, int]:
    out = {}
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            k, _, v = line.partition(" ")
            if v.strip().isdigit():
                out[k] = int(v)
    return out


def _vmstat() -> dict[str, int]:
    return _kv(Path("/proc/vmstat"))


def _cache_file(mib: float, n: int) -> Path:
    """A file of `mib` MiB for a job to read, made once and kept for later runs."""
    path = Path.home() / ".cache" / "lhscn" / f"cache-{n}-{round(mib)}m.bin"
    if not path.exists() or path.stat().st_size != round(mib) * MIB:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            for _ in range(round(mib)):
                f.write(os.urandom(MIB))
    return path


def _cli() -> list[str]:
    return [sys.executable, "-m", "leasehold"]


_TUNED = """
import json, sys
from leasehold import net
for name, value in json.loads(sys.argv[1]).items():
    assert hasattr(net, name), name
    setattr(net, name, type(getattr(net, name))(value))
from leasehold.cli import main
sys.argv[1:2] = []
sys.exit(main())
"""


def _daemon_cli(tune: str) -> list[str]:
    """The daemon's command line; `tune` (NAME:VALUE;...) sets `leasehold.net` constants."""
    if not tune:
        return _cli()
    pairs = dict(part.split(":", 1) for part in tune.split(";") if part)
    return [sys.executable, "-c", _TUNED, json.dumps(pairs)]


class Runner:
    def __init__(self, sc: Scenario, arm: dict, scale: float, out: Path) -> None:
        self.sc, self.arm, self.scale, self.out = sc, arm, scale, out
        self.token = os.urandom(3).hex()
        self.slice = f"leasehold-scn_{self.token}.slice"
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.dir = Path(tempfile.mkdtemp(prefix="lhscn-", dir=runtime))
        self.sock = str(self.dir / "s.sock")
        self.config = self.dir / "machine.toml"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("LEASEHOLD_")}
        self.env["LEASEHOLD_SLICE"] = self.slice
        self.env["LEASEHOLD_NET_TRACE"] = "1"
        self.daemon: subprocess.Popen | None = None
        self.stop = threading.Event()
        self.t0 = 0.0
        self.jobs: list[dict] = []
        self.events: list[dict] = []
        self.lock = threading.Lock()

    # --- daemon ---------------------------------------------------------------------------

    def start_daemon(self) -> None:
        log = (self.out / "daemon.log").open("a")
        self.daemon = subprocess.Popen(
            [*_daemon_cli(self.arm.get("tune", "")), "serve", "--user", "--config",
             str(self.config), "--socket", self.sock],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=self.env,
        )  # fmt: skip
        from leasehold.client import Client

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                Client(self.sock, timeout=2).status()
                return
            time.sleep(0.1)
        raise SystemExit("the scenario's daemon did not start; see daemon.log")

    def stop_daemon(self) -> None:
        if self.daemon is None:
            return
        self.daemon.terminate()
        try:
            self.daemon.wait(15)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
            self.daemon.wait()
        self.daemon = None

    def status(self) -> dict | None:
        from leasehold.client import Client

        with contextlib.suppress(Exception):
            return Client(self.sock, timeout=2).status()
        return None

    # --- watchers -------------------------------------------------------------------------

    def watch_events(self) -> None:
        from leasehold.client import Client

        since = 0
        f = (self.out / "events.jsonl").open("w")
        while not self.stop.is_set():
            try:
                got = Client(self.sock, timeout=2).events(since)
            except Exception:  # noqa: BLE001 -- the daemon may be restarting
                since = 0
                time.sleep(0.2)
                continue
            for e in got.get("events", []):
                e["rel"] = round(time.time() - self.t0, 3)
                with self.lock:
                    self.events.append(e)
                f.write(json.dumps(e) + "\n")
            f.flush()
            nxt = got.get("next", since)
            since = 0 if nxt < since else nxt  # a restarted daemon starts a new sequence
            self.stop.wait(0.1)
        f.close()

    def sample(self) -> None:
        cg = _cgroup_of_slice(self.slice)
        base = _vmstat()
        f = (self.out / "samples.csv").open("w", newline="")
        g = (self.out / "scopes.csv").open("w", newline="")
        w = csv.writer(f)
        ws = csv.writer(g)
        w.writerow(["t", "mem", "swap", "pswpout_mib", "pswpin_mib", "oom_kill", "max", "high"])
        ws.writerow(["t", "scope", "mem", "swap", "frozen"])
        while not self.stop.is_set():
            if not cg.is_dir() or cg == Path("/sys/fs/cgroup"):
                cg = _cgroup_of_slice(self.slice)
            t = round(time.time() - self.t0, 2)
            vm = _vmstat()
            ev = _kv(cg / "memory.events")
            w.writerow([
                t, _int(cg / "memory.current"), _int(cg / "memory.swap.current"),
                round((vm.get("pswpout", 0) - base.get("pswpout", 0)) * 4096 / MIB, 1),
                round((vm.get("pswpin", 0) - base.get("pswpin", 0)) * 4096 / MIB, 1),
                ev.get("oom_kill"), ev.get("max"), ev.get("high"),
            ])  # fmt: skip
            for scope in cg.glob("lease-*.scope"):
                frozen = (scope / "cgroup.freeze").read_text().strip() if scope.exists() else ""
                ws.writerow([
                    t, scope.name[6:14], _int(scope / "memory.current"),
                    _int(scope / "memory.swap.current"), frozen,
                ])  # fmt: skip
            self.stop.wait(0.2)
        f.close()
        g.close()

    # --- jobs -----------------------------------------------------------------------------

    def argv(self, i: int, job: Job) -> list[str]:
        s = self.scale
        size = job.size * s
        cmd = [
            sys.executable, str(MEMJOB), "--size", str(int(size * MIB)), "--ramp", str(job.ramp),
            "--hold", str(job.hold), "--workers", str(job.workers),
        ]  # fmt: skip
        if job.shared:
            cmd.append("--shared")
        if job.write:
            cmd.append("--write")
        if job.cache:
            cmd += ["--cache-file", str(_cache_file(job.cache * s, i))]
        if job.late:
            cmd += ["--late", str(int(job.late * s * MIB)), "--late-at", str(job.late_at)]
        weight = int(self.arm.get("weight", job.weight))
        declare = job.declare
        if "declare" in self.arm:
            declare = None if self.arm["declare"] == "none" else float(self.arm["declare"])
        mem = []
        if declare is not None:
            per_slot = (size + job.late * s) * declare / weight
            mem = ["--memory-per-slot", _size(per_slot)]
        return [
            *_cli(), "hold", "--socket", self.sock, "--weight", str(weight), *mem,
            "--session", f"j{i}", "--if-unavailable", "fail", "--", *cmd,
        ]  # fmt: skip

    def run_job(self, i: int, job: Job) -> None:
        delay = self.t0 + job.at - time.time()
        if delay > 0 and self.stop.wait(delay):
            return
        row = {"job": i, "size_mib": round(job.size * self.scale), "weight": job.weight,
               "heavy": job.heavy, "issued": round(time.time() - self.t0, 2)}  # fmt: skip
        proc = subprocess.Popen(
            self.argv(i, job), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.env, start_new_session=True,
        )  # fmt: skip
        row["pid"] = proc.pid
        with self.lock:
            self.jobs.append(row)
        err_lines: list[str] = []
        t = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)
        t.start()
        for line in proc.stdout:
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                row[msg["event"]] = round(msg["t"] - self.t0, 2)
        row["rc"] = proc.wait()
        row["done"] = round(time.time() - self.t0, 2)
        t.join(2)
        row["stderr"] = " | ".join(x.strip() for x in err_lines)[-600:]

    def run(self) -> dict:
        sc, s = self.sc, self.scale
        self.out.mkdir(parents=True, exist_ok=True)
        self.config.write_text(config_text(sc, self.arm, s, self.dir / "ledger"))
        (self.out / "config.toml").write_text(self.config.read_text().replace(str(self.dir), "."))
        cap, swap = float(self.arm.get("cap", sc.cap)), float(self.arm.get("swap", sc.swap))
        props = [f"MemoryMax={int(cap * s * MIB)}", f"MemorySwapMax={int(swap * s * MIB)}"]
        done = _systemctl("set-property", "--runtime", self.slice, *props)
        if done.returncode:
            raise SystemExit(f"cannot set up {self.slice}: {done.stderr}")
        swap0 = _vmstat().get("pswpout", 0)
        try:
            self.start_daemon()
            self.t0 = time.time()
            watchers = [threading.Thread(target=f, daemon=True)
                        for f in (self.watch_events, self.sample)]  # fmt: skip
            for w in watchers:
                w.start()
            threads = [threading.Thread(target=self.run_job, args=(i, j), daemon=True)
                       for i, j in enumerate(sc.jobs)]  # fmt: skip
            for t in threads:
                t.start()
            deadline = self.t0 + sc.timeout
            restarted = False
            while any(t.is_alive() for t in threads) and time.time() < deadline:
                due = sc.restart_at is not None and time.time() >= self.t0 + sc.restart_at
                if due and not restarted:
                    restarted = True
                    self.stop_daemon()
                    self.start_daemon()
                time.sleep(0.1)
            timed_out = any(t.is_alive() for t in threads)
            st = self.status()
            (self.out / "status.json").write_text(json.dumps(st, indent=1))
        finally:
            self.stop.set()
            for row in list(self.jobs):
                if "rc" not in row:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(row["pid"], signal.SIGTERM)
            _systemctl("kill", "--signal=KILL", self.slice)
            time.sleep(0.5)
            self.stop_daemon()
            _systemctl("stop", self.slice)
            _systemctl("revert", self.slice)
            shutil.rmtree(self.dir, ignore_errors=True)
        written = (_vmstat().get("pswpout", 0) - swap0) * 4096
        return self.summarize(st, written, timed_out)

    def summarize(self, st: dict | None, written: int, timed_out: bool) -> dict:
        jobs = sorted(self.jobs, key=lambda r: r["job"])
        with (self.out / "jobs.csv").open("w", newline="") as f:
            keys = ["job", "size_mib", "weight", "heavy", "issued", "full", "end", "done", "rc",
                    "stderr"]  # fmt: skip
            w = csv.DictWriter(f, keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(jobs)
        net = (st or {}).get("memory_net") or {}
        kinds: dict[str, int] = {}
        for e in self.events:
            kinds[e["type"]] = kinds.get(e["type"], 0) + 1
        killed = [r["job"] for r in jobs if "OOM killer" in r.get("stderr", "")]
        failed = [r["job"] for r in jobs if r.get("rc") not in (0, None)]
        ends = [r["done"] for r in jobs if "done" in r]
        summary = {
            "scenario": self.sc.name,
            "arm": self.arm,
            "scale": self.scale,
            "jobs": len(self.sc.jobs),
            "finished": sum(1 for r in jobs if r.get("rc") == 0),
            "killed": killed,
            "kills": len(killed),
            "failed": failed,
            "timed_out": timed_out,
            "makespan_s": max(ends) if ends else None,
            "parks": kinds.get("parked", 0),
            "resumes": kinds.get("resumed", 0),
            "net_written_mib": round((net.get("written_bytes") or 0) / MIB),
            "swap_written_mib": round(written / MIB),
            "wait_mean_s": _mean([r["full"] - r["issued"] for r in jobs if "full" in r]),
        }
        (self.out / "summary.json").write_text(json.dumps(summary, indent=1))
        return summary


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bench.scenarios")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("--arm", default="")
    r.add_argument("--scale", type=float, default=1.0)
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--out", type=Path, default=Path("bench/results/scenarios"))
    r.add_argument("--repeat", type=int, default=1)
    sm = sub.add_parser("summary")
    sm.add_argument("dirs", nargs="+", type=Path)
    args = p.parse_args(argv)
    if args.cmd == "list":
        for sc in scenarios().values():
            print(f"{sc.name:12} cap {sc.cap:.0f} MiB, swap {sc.swap:.0f}: {sc.question}")
        return 0
    if args.cmd == "summary":
        for d in args.dirs:
            for path in sorted(d.rglob("summary.json")):
                print(json.dumps(json.loads(path.read_text())))
        return 0
    sc = scenarios(args.seed)[args.name]
    arm = _arm(args.arm)
    tag = "_".join(f"{k}{v}" for k, v in arm.items()) or "default"
    tag = "".join(c if c.isalnum() or c in "._-" else "-" for c in tag)
    for n in range(args.repeat):
        out = args.out / sc.name / f"{tag}-s{args.seed}-{time.strftime('%H%M%S')}-{n}"
        summary = Runner(sc, arm, args.scale, out).run()
        print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
