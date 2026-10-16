"""The benchmark's harness: conditions with parameters and their configs, the round schedule,
the washout, the manifest, jobserver-client jobs, and `compare` on gate files."""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
from argparse import Namespace
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
sys.path.insert(0, str(BENCH))

import conditions as conds  # noqa: E402
import gate  # noqa: E402
import run as bench  # noqa: E402
import schedule  # noqa: E402
import washout  # noqa: E402

from leasehold.machine import load  # noqa: E402

RUN = BENCH / "run.py"
JOB = BENCH / "job.py"


def _bench(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), *args], capture_output=True, text=True, timeout=120, check=check
    )


# ---------------------------------------------------------------------------------------------
# Conditions


def test_a_condition_takes_parameters_and_has_one_spelling():
    c = conds.parse("lh-enforced@kept=3,h=1,w=3-3")
    assert (c.base, c.weight, c.headroom, c.kept) == ("lh-enforced", (3, 3), 1, 3)
    assert c.text == "lh-enforced@w=3,h=1,kept=3"
    assert c.slug == "lh-enforced_w3_h1_kept3"
    assert conds.parse(c.text) == c
    d = conds.parse("lh-enforced@w=2-4,mem=declared,arm=b")
    assert d.declared and d.arm == "b" and d.text == "lh-enforced@w=2-4,mem=declared,arm=b"
    assert conds.parse("lh-enforced-declared").declared
    assert conds.parse("unmanaged").text == "unmanaged"


def test_a_list_keeps_each_conditions_parameters_together():
    text = "unmanaged,lh-enforced@w=3-3,h=1,kept=3,lh-enforced@w=2-4,nice"
    assert conds.split(text) == [
        "unmanaged",
        "lh-enforced@w=3-3,h=1,kept=3",
        "lh-enforced@w=2-4",
        "nice",
    ]
    assert [c.text for c in conds.parse_list(text)][1] == "lh-enforced@w=3,h=1,kept=3"


@pytest.mark.parametrize(
    "text",
    [
        "nope",
        "unmanaged@w=4",
        "lh-enforced-v020@w=4",
        "lh-enforced@w=0",
        "lh-enforced@w=4-2",
        "lh-enforced@x=1",
        "lh-enforced@h=-1",
        "lh-enforced@mem=some",
        "lh-enforced@w=2,w=3",
        "lh-enforced@budget=lots",
    ],
)
def test_a_bad_condition_is_refused(text):
    with pytest.raises(ValueError):
        conds.parse(text)


def test_a_condition_listed_twice_or_a_stray_parameter_is_refused():
    with pytest.raises(ValueError, match="twice"):
        conds.parse_list("lh-enforced@w=4,lh-enforced@w=4")
    with pytest.raises(ValueError, match="follows no condition"):
        conds.parse_list("unmanaged,h=1")
    assert len(conds.parse_list("lh-enforced@w=4,arm=a,lh-enforced@w=4,arm=b")) == 2


@pytest.fixture(scope="module")
def init_text() -> str:
    return bench.init_config()


def _config(init_text: str, condition: str, tmp: Path, **kw) -> tuple[str, object]:
    text = conds.config(init_text, conds.parse(condition), cpus=8, ledger=str(tmp / "l"), **kw)
    path = tmp / "machine.toml"
    path.write_text(text)
    return text, load(path)


def test_the_config_takes_the_conditions_slots_headroom_and_weight(init_text, tmp_path):
    text, cfg = _config(init_text, "lh-enforced@w=3,h=1,kept=3,budget=7g", tmp_path)
    raw = tomllib.loads(text)
    assert raw["name"] == "bench"
    assert raw["ceiling"]["slots"] == 5
    assert raw["pools"]["work"]["max"] == 5
    assert raw["pools"]["work"]["headroom"] == 1
    assert raw["commands"]["heavy_weight"] == 3
    assert raw["memory"]["budget"] == "7g"
    assert raw["ledger_dir"] == str(tmp_path / "l")
    assert cfg.ceiling.slots == 5
    assert conds.summary(text)["work_headroom"] == 1


def test_a_ranged_condition_leaves_the_rules_and_kept_defaults_alone(init_text, tmp_path):
    plain, _ = _config(init_text, "lh-enforced", tmp_path)
    ranged, _ = _config(init_text, "lh-enforced@w=2-4", tmp_path)
    assert tomllib.loads(ranged)["ceiling"] == tomllib.loads(plain)["ceiling"]
    assert "heavy_weight" not in tomllib.loads(ranged)["commands"]
    assert "hold --weight 2-4" in ranged
    hard, _ = _config(init_text, "lh-hardcpu-greedy", tmp_path)
    assert tomllib.loads(hard)["contain"]["hard_cpu"] is True
    old, _ = _config(init_text, "lh-enforced-v020", tmp_path, memory_budget="7g")
    assert "memory" not in tomllib.loads(old)


def test_a_kept_count_that_leaves_no_slot_is_refused(init_text, tmp_path):
    with pytest.raises(ValueError, match="no slot"):
        _config(init_text, "lh-enforced@kept=8", tmp_path)


def test_the_config_never_names_the_host(init_text):
    host = socket.gethostname().split(".")[0].lower()
    raw = "# leasehold config for your own daemon on h0st.\n" + init_text.split("\n", 1)[1]
    raw = raw.replace('name = "bench"', 'name = "h0st"')
    assert "h0st" not in conds.neutral(raw)
    if host not in ("bench", "machine"):
        assert host not in init_text.lower()


# ---------------------------------------------------------------------------------------------
# The schedule


@pytest.mark.parametrize("n", range(1, 8))
def test_the_williams_orders_are_balanced(n):
    rows = schedule.williams(n)
    assert len(rows) == (2 * n if n % 2 else n)
    assert all(sorted(r) == list(range(n)) for r in rows)
    for pos in range(n):  # every arm in every position equally often
        assert len(set(Counter(r[pos] for r in rows).values())) == 1
    if n > 1:  # every arm follows every other arm equally often
        follows = Counter((r[i], r[i + 1]) for r in rows for i in range(n - 1))
        assert len(follows) == n * (n - 1)
        assert len(set(follows.values())) == 1


def test_two_arms_alternate_and_a_round_shares_one_seed():
    a, b = "lh-enforced@w=4", "lh-enforced@w=2-4"
    out = bench.runs([a, b], 4, seed_base=100)
    assert [c for c, _, _ in out] == [a, b, b, a, a, b, b, a]
    assert [s for _, _, s in out] == [101, 101, 102, 102, 103, 103, 104, 104]
    assert out[2][1] == "lh-enforced_w2-4-r2"
    assert bench.runs(["nice"], 1) == [("nice", "nice", 1)]


def test_one_round_runs_what_the_whole_schedule_runs_in_it():
    names = ["a", "b", "c", "d", "e"]
    whole = schedule.schedule(names, 12, 7)
    for r in range(1, 13):
        alone = schedule.schedule(names, 12, 7, only=r)
        assert alone == [s for s in whole if s.round == r]
        assert alone == [s for s in schedule.schedule(names, 20, 7) if s.round == r]
    with pytest.raises(ValueError):
        schedule.schedule(names, 3, 7, only=4)


def test_the_dry_run_prints_the_rounds_schedule():
    conditions = "unmanaged,lh-enforced@w=2-4,h=1,nice"
    whole = json.loads(
        _bench("--dry-run", "--rounds", "6", "--seed-base", "50", "--conditions", conditions).stdout
    )
    three = json.loads(
        _bench(
            "--dry-run",
            "--rounds",
            "6",
            "--seed-base",
            "50",
            "--round",
            "3",
            "--conditions",
            conditions,
        ).stdout
    )
    assert three["schedule"] == [s for s in whole["schedule"] if s["round"] == 3]
    assert three["seed"] == 53
    assert {s["seed"] for s in three["schedule"]} == {53}


def test_the_command_line_refuses_what_it_cannot_run():
    bad = _bench("--dry-run", "--conditions", "unmanaged@w=4", check=False)
    assert bad.returncode == 2 and "takes no parameters" in bad.stderr
    bad = _bench("--dry-run", "--machine-class", "a/b", check=False)
    assert bad.returncode == 2 and "--machine-class" in bad.stderr
    bad = _bench("--dry-run", "--rounds", "2", "--round", "3", check=False)
    assert bad.returncode == 2 and "--round" in bad.stderr


def test_an_elastic_condition_fails_clearly_without_elastic_leases():
    if bench.elastic_supported():
        pytest.skip("this leasehold has elastic leases")
    done = _bench("--dry-run", "--conditions", "unmanaged,lh-elastic", check=False)
    assert done.returncode == 2
    assert "lh-elastic needs a leasehold with elastic leases" in done.stderr


# ---------------------------------------------------------------------------------------------
# The washout


class FakeMachine:
    """Readings 5 s apart, from a list of (load, mem_some, pswpin, dirty MiB)."""

    def __init__(self, values):
        self.values = list(values)
        self.t = 0.0
        self.i = 0

    def sleep(self, s: float) -> None:
        self.t += s

    def read(self) -> washout.Reading:
        load, mem, swapin, dirty = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return washout.Reading(self.t, load, 0.0, mem, swapin, dirty << 20)


def test_the_wait_ends_once_swap_in_has_been_quiet_for_30_s():
    fake = FakeMachine([(0.5, 0.0, 1000, 0)])
    seen = washout.wait_quiet(8, 600, 0, lambda _: None, fake.read, fake.sleep)
    assert seen["quiet"] and not seen["timed_out"]
    assert fake.t == 30


@pytest.mark.parametrize(
    "noise",
    [(9.0, 0.0, None, 0), (0.5, 0.6, None, 0), (0.5, 0.0, "swap", 0), (0.5, 0.0, None, 100)],
)
def test_the_wait_times_out_on_load_pressure_swap_in_or_dirty_pages(noise):
    load, mem, swap, dirty = noise
    values = [(load, mem, 1000 + 100 * i if swap else 1000, dirty) for i in range(400)]
    fake = FakeMachine(values)
    seen = washout.wait_quiet(8, 120, 0, lambda _: None, fake.read, fake.sleep)
    assert not seen["quiet"] and seen["timed_out"]
    assert seen["why"]


def _slot_env(tmp_path: Path, jobs: int | None = None, row: dict | None = None):
    spec = {
        "machine": bench.machine("test"),
        "conditions": ["unmanaged"],
        "agents": 2,
        "preset": asdict(bench.PRESETS["tiny"]),
        "nominal_s": {"cpu": 0.5, "mem": 0.5},
    }
    planned = len(bench.spec_jobs(spec, 11))
    args = Namespace(note=["n"], drop_caches=False, no_wait=False, max_wait=1, settle=0)
    slot = schedule.Slot(1, 1, "unmanaged", "unmanaged-r1", 11)
    ran: list[int] = []

    def fake_run(_args, _spec, _outdir, workdir, _slot, _seen):
        ran.append(1)
        return {"condition": "unmanaged-r1", "makespan_s": 1.0, "jobs": planned} | (row or {})

    return spec, args, slot, ran, fake_run


def test_a_washout_that_times_out_twice_leaves_the_run_invalid_and_not_run(tmp_path):
    spec, args, slot, ran, fake_run = _slot_env(tmp_path)
    seen = {"quiet": False, "timed_out": True, "why": ["load 9.00"]}
    row = bench.run_slot(args, spec, tmp_path, lambda _: None, slot, {}, lambda: seen, fake_run)
    assert row is None and ran == []
    m = json.loads((tmp_path / "unmanaged-r1" / "manifest.json").read_text())
    assert m["valid"] is False and "not quiet" in m["why_invalid"]
    assert [a["attempt"] for a in m["washout"]] == [1, 2]
    lines = (tmp_path / "invalid.jsonl").read_text().splitlines()
    assert [json.loads(x)["attempt"] for x in lines] == [1, 2]


def test_a_washout_that_times_out_once_is_retried(tmp_path):
    spec, args, slot, ran, fake_run = _slot_env(tmp_path)
    waits = iter([{"quiet": False, "timed_out": True}, {"quiet": True}])
    row = bench.run_slot(
        args, spec, tmp_path, lambda _: None, slot, {}, lambda: next(waits), fake_run
    )
    assert row is not None and ran == [1]
    m = json.loads((tmp_path / "unmanaged-r1" / "manifest.json").read_text())
    assert m["valid"] is True
    assert [(a["attempt"], a["ran"]) for a in m["washout"]] == [(1, False), (2, True)]
    # Run again, the old directory is kept aside.
    bench.run_slot(
        args, spec, tmp_path, lambda _: None, slot, {}, lambda: {"quiet": True}, fake_run
    )
    assert len(list((tmp_path / "superseded").iterdir())) == 1


@pytest.mark.parametrize(
    ("row", "why"),
    [
        ({"jobs": 1}, "jobs ran of the"),
        ({"aborted": "harness error: agent 0: RuntimeError('x')"}, "harness error"),
    ],
)
def test_a_run_short_of_its_plan_or_with_a_harness_error_is_invalid(tmp_path, row, why):
    spec, args, slot, _ran, fake_run = _slot_env(tmp_path, row=row)
    quiet = lambda: {"quiet": True}  # noqa: E731
    bench.run_slot(args, spec, tmp_path, lambda _: None, slot, {}, quiet, fake_run)
    m = json.loads((tmp_path / "unmanaged-r1" / "manifest.json").read_text())
    assert m["valid"] is False and why in m["why_invalid"]
    (line,) = (tmp_path / "invalid.jsonl").read_text().splitlines()
    assert why in json.loads(line)["why"]


def test_a_run_whose_inner_process_failed_is_invalid():
    assert bench.run_problem({"jobs": 3}, 3, 0) == ""
    assert "exited 1" in bench.run_problem({"jobs": 3}, 3, 1)


def test_an_exception_in_a_harness_thread_aborts_the_run():
    run = bench.Run.__new__(bench.Run)
    run.aborted, run.stop, run.lock, run.live = None, threading.Event(), threading.Lock(), {}
    run.setup = Namespace(kill_jobs=lambda: None)

    def boom():
        raise RuntimeError("sampler fell over")

    t = threading.Thread(target=run.guarded, args=("sampler", boom))
    t.start()
    t.join(5)
    assert run.stop.is_set()
    assert run.aborted.startswith(bench.HARNESS_ERROR) and "sampler fell over" in run.aborted


def test_an_unreadable_probe_is_never_quiet():
    unreadable = washout.Reading(100.0, None, None, None, None, None)
    why = washout.not_quiet(unreadable, [washout.Reading(0.0, 0, 0, 0, 0, 0)], 8)
    assert len(why) == 5 and all("unreadable" in w for w in why)
    no_psi = washout.Reading(100.0, 0.1, None, None, 0, 0)
    with pytest.raises(SystemExit, match="/proc/pressure/cpu, /proc/pressure/memory"):
        washout.check_probes(lambda: no_psi)
    washout.check_probes(lambda: washout.Reading(0.0, 0.1, 0.0, 0.0, 0, 0))


def test_the_runs_sizes_are_recorded_the_same_from_a_command_line_or_a_gate():
    text = "--agents 8 --duration 240 --mem-gib 2.5 --cap-gib 7 --git-repo /var/tmp/bigrepo"
    sizes = bench.workload_args(text)
    assert sizes == {
        "preset": "full",
        "duration_s": 240.0,
        "agents": 8,
        "mem_gib": 2.5,
        "cap_gib": 7.0,
        "swap_gib": 2.0,
        "weights": "",
        "memory_budget": None,
        "slice_weight": 20,
        "git_repo": True,
    }
    assert bench.workload_args(text.replace("2.5", "3")) != sizes
    assert bench.workload_args("--weights mem=4,cpu=2")["weights"] == "cpu=2,mem=4"


def test_publish_copies_only_the_summaries(tmp_path):
    out = tmp_path / "res"
    out.mkdir()
    for name in ("summary.md", "summary.csv", "jobs.csv", "daemon.log", "log.txt", "spec.json"):
        (out / name).write_text("/run/user/1000/leasehold\n")
    copied = bench.publish(out, tmp_path / "pub", "k8-full-d240")
    assert sorted(p.name for p in copied) == ["k8-full-d240.csv", "k8-full-d240.md"]
    assert sorted(p.name for p in (tmp_path / "pub").iterdir()) == [
        "k8-full-d240.csv",
        "k8-full-d240.md",
    ]


def test_dropping_the_cache_without_permission_fails_clearly(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(SystemExit, match="--drop-caches needs root"):
        washout.check_drop_caches()


# ---------------------------------------------------------------------------------------------
# Jobserver-client jobs


def _jobserver(tokens: int):
    d = Path(tempfile.mkdtemp())
    fifo = d / "fifo"
    os.mkfifo(fifo)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    os.write(fd, b"+" * tokens)
    return fifo, fd


def _drain(fd: int) -> int:
    n = 0
    while True:
        try:
            n += len(os.read(fd, 64))
        except BlockingIOError:
            return n


@pytest.mark.parametrize("tokens", [0, 2])
def test_a_jobserver_job_runs_on_its_tokens_and_gives_them_back(tokens):
    fifo, fd = _jobserver(tokens)
    env = os.environ | {"MAKEFLAGS": f"-j{tokens + 1} --jobserver-auth=fifo:{fifo}"}
    for kind, extra in (("cpu", []), ("mem", ["--size", str(4 << 20)])):
        done = subprocess.run(
            [sys.executable, str(JOB), kind, "--work", str(8 << 20), "--jobserver", *extra],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        start = next(x for x in done.stdout.splitlines() if x.startswith("BENCH start"))
        end = next(x for x in done.stdout.splitlines() if x.startswith("BENCH end"))
        assert start.split()[3] == str(tokens + 1)
        assert 1 <= int(end.split()[3]) <= tokens + 1
    assert _drain(fd) == tokens
    os.close(fd)


def test_a_jobserver_job_without_a_jobserver_exits_2():
    env = {k: v for k, v in os.environ.items() if k != "MAKEFLAGS"}
    done = subprocess.run(
        [sys.executable, str(JOB), "cpu", "--work", "1000", "--jobserver"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2 and "MAKEFLAGS" in done.stderr


def test_makeflags_are_read_as_make_reads_them():
    assert gate.math  # the module imported cleanly
    sys.path.insert(0, str(BENCH))
    import job

    assert job.jobserver_auth("-j4 --jobserver-auth=fifo:/x -j2 --jobserver-auth=3,4") == (
        "3,4",
        2,
    )
    assert job.jobserver_auth("-k") == ("", 0)


# ---------------------------------------------------------------------------------------------
# Student's t and compare


@pytest.mark.parametrize(
    ("p", "df", "t"),
    [
        (0.95, 1, 6.313752),
        (0.95, 3, 2.353363),
        (0.95, 7, 1.894579),
        (0.95, 11, 1.795885),
        (0.975, 10, 2.228139),
        (0.975, 3, 3.182446),
        (0.99, 2, 6.964557),
        (0.05, 5, -2.015048),
        (0.95, 1000, 1.646379),
    ],
)
def test_the_t_quantile_matches_the_tables(p, df, t):
    assert gate.t_quantile(p, df) == pytest.approx(t, abs=1e-5)
    assert gate.t_cdf(t, df) == pytest.approx(p, abs=1e-6)


def test_the_sign_permutation_test_is_exact_for_few_rounds():
    p, exact = gate.sign_permutation_p([-0.1, -0.2, -0.15, -0.05])
    assert exact and p == 1 / 16
    p, exact = gate.sign_permutation_p([-0.1] * 13)
    assert not exact and p < 0.001


GOOD = {
    "makespan_s": 600,
    "job_mean_s": 40.0,
    "wake_same_p99_ms": 4.0,
    "git_p95_ms": 120,
    "failed": 0,
    "oom_kills": 0,
    "oom_kills_machine": 0,
    "mem_some_max": 1.0,
    "aborted": "",
}
SCALED = ("makespan_s", "job_mean_s", "wake_same_p99_ms", "git_p95_ms")
CONTROL = "lh-enforced@w=4"
CAND = "lh-enforced@w=2-4"
FULL = "--agents 8 --duration 240 --mem-gib 2.5 --cap-gib 7 --git-repo /var/tmp/bigrepo"


def _records(rounds: int, scale: dict | None = None, noise: float = 0.01, cand=CAND, **cand_row):
    """The control as GOOD each round; the candidate as GOOD scaled by `scale` and by a wobble
    of +/- `noise` that alternates round by round, then with `cand_row` on top."""
    out = []
    for r in range(1, rounds + 1):
        wobble = 1 + noise * (1 if r % 2 else -1)
        scaled = {k: GOOD[k] * (scale or {}).get(k, 1.0) * wobble for k in SCALED}
        for cond, row in ((CONTROL, dict(GOOD)), (cand, GOOD | scaled | cand_row)):
            out.append(
                {
                    "dir": "d",
                    "workload": "full",
                    "seed_base": 1000,
                    "round": r,
                    "seed": 1000 + r,
                    "condition": cond,
                    "valid": True,
                    "why_invalid": "",
                    "row": row,
                    "git_sha": "a" * 40,
                    "leasehold": "0.4.0",
                    "machine_class": "kvm-8cpu-16g",
                }
            )
    return out


def _info(**kw) -> dict:
    return {
        "name": "d",
        "workload": "full",
        "seed_base": 1000,
        "args": bench.workload_args(FULL),
        "arms": 2,
        "rounds_seen": 8,
    } | kw


def _gate(kind="confirmatory", candidates=(CAND,), **extra) -> dict:
    g = {
        "gate": {
            "name": "t",
            "kind": kind,
            "intent": "i",
            "objective": "o",
            "stop": "s",
            "decision": "d",
        },
        "arms": {"control": CONTROL, "candidates": list(candidates)},
        "rounds": {"planned": 8, "look_after": 4, "extend_to": 12, "seed_base": 1000},
        "plan": {
            "joint_power": 0.8,
            "min_rounds": 8,
            "max_rounds": 12,
            "widen": {"makespan_s": 1.10, "job_mean_s": 1.10},
        },
        "test": {"alpha": 0.05},
        "metrics": [
            {"name": "makespan_s", "margin": 1.05},
            {"name": "job_mean_s", "margin": 1.05},
            {"name": "wake_same_p99_ms", "margin": 1.5},
            {"name": "git_p95_ms", "margin": 1.5},
        ],
        "hard": {"failed_jobs": 0, "oom_kills": 0, "psi_max": 10.0, "psi_over_control": 5.0},
        "workloads": [{"name": "full", "args": FULL, "sizes": bench.workload_args(FULL)}],
        "order": {"sequential": True},
    }
    return g | extra


def _verdict(records, g=None) -> dict:
    return gate.evaluate(g or _gate(), records)["results"][0]


def test_a_candidate_as_good_as_the_control_passes():
    r = _verdict(_records(8))
    assert r["verdict"] == "pass", r["why"]
    (w,) = r["workloads"]
    assert all(m["pass"] and m["permutation_pass"] for m in w["metrics"])


@pytest.mark.parametrize(
    ("metric", "factor"),
    [("makespan_s", 1.06), ("job_mean_s", 1.08), ("wake_same_p99_ms", 1.6), ("git_p95_ms", 2.0)],
)
def test_a_candidate_fails_on_any_one_metric(metric, factor):
    r = _verdict(_records(8, {metric: factor}))
    assert r["verdict"] == "fail"
    failed = [m["metric"] for m in r["workloads"][0]["metrics"] if not m["pass"]]
    assert failed == [metric]


@pytest.mark.parametrize(
    "row", [{"failed": 1}, {"oom_kills": 2}, {"oom_kills_machine": 1}, {"mem_some_max": 12.0}]
)
def test_a_candidate_that_breaks_a_hard_rule_fails(row):
    r = _verdict(_records(8, **row))
    assert r["verdict"] == "fail"
    assert r["workloads"][0]["hard"]


def test_memory_pressure_breaks_only_above_the_limit_and_the_controls_plus_the_slack():
    records = _records(8)
    for x in records:
        x["row"] = x["row"] | {"mem_some_max": 9.0 if x["condition"] == CONTROL else 12.0}
    r = _verdict(records)
    assert r["verdict"] == "pass", r["why"]
    assert r["workloads"][0]["psi"][0] == {"round": 1, "control": 9.0, "candidate": 12.0}
    records[1]["row"] = records[1]["row"] | {"mem_some_max": 14.5}
    r = _verdict(records)
    assert r["verdict"] == "fail" and "memory PSI max 14.5%" in r["workloads"][0]["hard"][0]


def test_a_hard_rule_counts_in_a_round_that_does_not_count():
    records = _records(9)
    records[0]["valid"] = False  # the control of round 1
    records[1]["row"] = records[1]["row"] | {"failed": 2}
    r = _verdict(records)
    assert r["workloads"][0]["rounds"] == list(range(2, 10))
    assert r["verdict"] == "fail" and r["workloads"][0]["hard"] == ["round 1: 2 failed jobs"]


def test_a_control_that_breaks_a_hard_rule_makes_its_round_invalid_and_says_so():
    records = _records(9)
    records[4]["row"] = records[4]["row"] | {"oom_kills": 1}  # the control of round 3
    r = _verdict(records)
    w = r["workloads"][0]
    assert w["invalid"] == [{"round": 3, "why": "the control broke a hard rule: 1 OOM kills"}]
    assert w["rounds"] == [1, 2, 4, 5, 6, 7, 8, 9] and r["verdict"] == "pass"


def test_the_verdict_uses_exactly_the_first_planned_rounds():
    records = _records(8) + [
        x | {"round": r, "seed": 1000 + r}
        for r in (9, 10)
        for x in _records(1, {"makespan_s": 3.0})
    ]
    r = _verdict(records)
    assert r["verdict"] == "pass", r["why"]
    w = r["workloads"][0]
    assert w["rounds"] == list(range(1, 9)) and w["extra"] == [9, 10]
    assert all(m["n"] == 8 for m in w["metrics"])


def test_a_metric_missing_in_a_used_round_makes_the_verdict_incomplete():
    records = _records(8)
    records[3]["row"] = records[3]["row"] | {"git_p95_ms": None}
    r = _verdict(records)
    assert r["verdict"] == "incomplete"
    assert "git_p95_ms missing in rounds [2]" in " ".join(r["why"])


def test_too_few_rounds_is_incomplete_and_noise_asks_for_more():
    assert _verdict(_records(6))["verdict"] == "incomplete"
    noisy = gate.evaluate(_gate(), _records(8, noise=0.08))
    assert noisy["rounds_needed"] == 12
    assert noisy["results"][0]["verdict"] == "incomplete"


def test_the_second_candidate_is_tested_only_if_the_first_passes():
    other = "lh-enforced@w=3,h=1"
    records = _records(8, {"makespan_s": 1.2}) + [
        x for x in _records(8, cand=other) if x["condition"] == other
    ]
    out = gate.evaluate(_gate(candidates=(CAND, other)), records)["results"]
    assert [r["verdict"] for r in out] == ["fail", "not tested"]
    out = gate.evaluate(_gate(candidates=(other, CAND)), records)["results"]
    assert [r["verdict"] for r in out] == ["pass", "fail"]


def test_an_invalid_run_drops_its_round_from_the_pairs():
    records = _records(8)
    records[0]["valid"] = False
    records[0]["why_invalid"] = "not quiet"
    r = _verdict(records)
    assert r["workloads"][0]["rounds"] == list(range(2, 9))
    assert r["workloads"][0]["invalid"] == [
        {"round": 1, "why": "the control run is invalid: not quiet"}
    ]
    assert r["verdict"] == "incomplete"


def test_the_blind_look_gives_the_pooled_sds_and_not_the_leader():
    slower = gate.blind_sd(_gate(), _records(8, {"makespan_s": 1.3}, noise=0.02))
    faster = gate.blind_sd(_gate(), _records(8, {"makespan_s": 0.7}, noise=0.02))
    assert slower["pooled_sd"] == pytest.approx(faster["pooled_sd"], rel=1e-2)
    assert set(slower["pooled_sd"]) == set(SCALED)
    assert slower["ready"] and slower["rounds_needed"] == 8
    assert not any("mean" in k or "ratio" in k for k in slower)
    noisy = gate.blind_sd(_gate(), _records(8, noise=0.1))
    assert noisy["rounds_needed"] == 12
    # The latency metrics count too: noise on wake p99 alone asks for more rounds.
    laggy = _records(8)
    for x in laggy:
        if x["condition"] == CAND:
            x["row"] = x["row"] | {"wake_same_p99_ms": 4.0 * (1.6 if x["round"] % 2 else 0.6)}
    assert gate.blind_sd(_gate(), laggy)["rounds_needed"] > 8


def test_power_matches_a_simulation():
    import random

    for n, sd, margin in ((8, 0.06, 1.05), (12, 0.29, 1.5)):
        rng, q, hits = random.Random(7), gate.t_quantile(0.95, n - 1), 0
        for _ in range(4000):
            d = [rng.gauss(0, sd) for _ in range(n)]
            m, s = gate.mean_sd(d)
            hits += m + q * s / math.sqrt(n) < math.log(margin)
        assert gate.pass_power(n, sd, margin, 0.05) == pytest.approx(hits / 4000, abs=0.025)
    assert gate.pass_power(8, 0.06, 1.05, 0.05) == pytest.approx(0.665, abs=0.005)


def _sds(mk: float, jm: float, lat: float = 0.29) -> dict:
    return {"makespan_s": mk, "job_mean_s": jm, "wake_same_p99_ms": lat, "git_p95_ms": lat}


def test_joint_power_is_the_product_over_metrics_and_workloads():
    sds = _sds(0.06, 0.04)
    margins = {"makespan_s": 1.05, "job_mean_s": 1.05, "wake_same_p99_ms": 1.5, "git_p95_ms": 1.5}
    one = math.prod(gate.pass_power(10, sds[k], margins[k], 0.05) for k in margins)
    assert gate.joint_power(10, sds, margins, 0.05, 2) == pytest.approx(one**2)


def test_the_plan_rule_targets_joint_power_over_both_workloads():
    g = _gate()
    out = gate.plan(_sds(0.03, 0.03), g["metrics"], g["plan"], 0.05, 2)
    assert out["rounds"] == 8 and out["reached"] and out["joint_power"] >= 0.8
    assert not any(m["widened"] for m in out["metrics"])
    # At SD 0.06 each throughput metric has about 0.84 power at 12 rounds, so the joint power
    # over four metrics and two workloads stays near 0.5: the throughput margins widen.
    out = gate.plan(_sds(0.06, 0.06), g["metrics"], g["plan"], 0.05, 2)
    assert [m["margin"] for m in out["metrics"]] == [1.10, 1.10, 1.5, 1.5]
    assert out["rounds"] == 8 and out["reached"]
    out = gate.plan(_sds(0.08, 0.08), g["metrics"], g["plan"], 0.05, 2)
    assert out["rounds"] == 10 and out["reached"] and out["joint_power"] >= 0.8


def test_widening_job_mean_is_what_reaches_the_target():
    g = _gate()
    makespan_only = g["plan"] | {"widen": {"makespan_s": 1.10}}
    out = gate.plan(_sds(0.02, 0.06), g["metrics"], makespan_only, 0.05, 2)
    assert not out["reached"] and out["rounds"] == 12 and out["joint_power"] < 0.8
    out = gate.plan(_sds(0.02, 0.06), g["metrics"], g["plan"], 0.05, 2)
    assert out["reached"] and out["metrics"][1]["margin"] == 1.10
    assert out["metrics"][1]["widened"]


def test_latency_margins_never_widen_and_a_shortfall_is_stated():
    g = _gate()
    out = gate.plan(_sds(0.03, 0.03, lat=0.6), g["metrics"], g["plan"], 0.05, 2)
    assert out["rounds"] == 12 and not out["reached"] and out["joint_power"] < 0.8
    assert [m["margin"] for m in out["metrics"]][2:] == [1.5, 1.5]
    text = gate.plan_text(out, "#")
    assert "Known limit: even at 12 rounds" in text and f"{out['joint_power']:.2f}" in text


def test_the_screen_drops_slow_or_laggy_arms_and_keeps_two_at_most():
    arms = [
        "lh-enforced@w=3,h=1",
        "lh-enforced@w=2-4",
        "lh-enforced@w=2-3,kept=3",
        "lh-enforced@w=4-8",
        "lh-enforced@w=5",
    ]
    scales = [
        {"wake_same_p99_ms": 2.5},
        {"makespan_s": 0.9},
        {"makespan_s": 0.95},
        {"makespan_s": 1.2},
        {"makespan_s": 0.97},
    ]
    records = []
    for arm, scale in zip(arms, scales, strict=True):
        records += [x for x in _records(3, scale, cand=arm) if x["condition"] == arm]
    records += [x for x in _records(3) if x["condition"] == CONTROL]
    g = _gate(
        "screen",
        arms,
        rounds={"planned": 3, "seed_base": 1000},
        screen={
            "wake_ratio_above": 2.0,
            "makespan_ratio_above": 1.15,
            "max_survivors": 2,
            "rank_by": "makespan_s",
        },
    )
    out = {r["candidate"]: r["verdict"] for r in gate.evaluate(g, records)["results"]}
    assert out == {
        arms[0]: "drop",
        arms[1]: "survives",
        arms[2]: "survives",
        arms[3]: "drop",
        arms[4]: "drop",
    }
    short = [x for x in records if x["round"] < 3]
    out = {r["candidate"]: r["verdict"] for r in gate.evaluate(g, short)["results"]}
    assert out[arms[1]] == "incomplete"


def _aa_gate() -> dict:
    a, b = "lh-enforced@w=4,arm=a", "lh-enforced@w=4,arm=b"
    return _gate(
        "aa",
        [b],
        arms={"control": a, "candidates": [b]},
        rounds={"planned": 4, "seed_base": 1000},
        aa={"metric": "makespan_s", "ci": 0.95, "max_paired_sd": 0.06},
    )


def _aa_records(rounds, scale, noise):
    a, b = "lh-enforced@w=4,arm=a", "lh-enforced@w=4,arm=b"
    out = _records(rounds, scale, noise, cand=b)
    for x in out:
        x["condition"] = a if x["condition"] == CONTROL else b
    return out


def test_the_aa_check_passes_on_noise_alone_and_fails_on_a_shift():
    g = _aa_gate()
    ok = gate.evaluate(g, _aa_records(4, None, 0.01))
    assert ok["results"][0]["verdict"] == "pass"
    assert set(ok["sds"]) == set(SCALED) and all(v is not None for v in ok["sds"].values())
    shift = gate.evaluate(g, _aa_records(4, {"makespan_s": 1.1}, 0.01))
    assert shift["results"][0]["verdict"] == "fail"
    assert gate.evaluate(g, _aa_records(4, None, 0.1))["results"][0]["verdict"] == "fail"
    assert gate.evaluate(g, _aa_records(3, None, 0.01))["results"][0]["verdict"] == "incomplete"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"info": {"seed_base": 100}}, "seed base 100"),
        ({"info": {"workload": "other"}}, "does not name"),
        ({"info": {"args": bench.workload_args(FULL.replace("2.5", "3"))}}, "other args"),
        ({"duplicate": True}, "is in both"),
        ({"record": {"git_sha": "b" * 40}}, "2 commits"),
        ({"record": {"leasehold": "0.3.0"}}, "2 leasehold versions"),
        ({"record": {"machine_class": "other"}}, "2 machine classes"),
    ],
)
def test_compare_refuses_results_it_cannot_pool(change, why):
    records, infos = _records(8), [_info()]
    infos[0] |= change.get("info", {})
    records[0] |= change.get("record", {})
    if change.get("duplicate"):
        records.append(records[0] | {"dir": "d2"})
    with pytest.raises(gate.Refused, match=why):
        gate.check_provenance(_gate(), records, infos)
    gate.check_provenance(_gate(), _records(8), [_info()])


def test_a_pair_with_two_seeds_is_refused():
    records = _records(8)
    records[1]["seed"] = 5
    with pytest.raises(gate.Refused, match="round 1: the control ran seed 1001, the candidate 5"):
        _verdict(records)


def test_compare_says_when_the_order_is_not_a_whole_williams_cycle():
    assert gate.balance_notes([_info(arms=3, rounds_seen=12)]) == []
    (note,) = gate.balance_notes([_info(arms=3, rounds_seen=8)])
    assert "not a whole number of Williams cycles (6 rounds)" in note
    assert "unbalanced positions" in note and "screen" not in note
    (note,) = gate.balance_notes([_info(arms=5, rounds_seen=3)], "screen")
    assert "the screen's thresholds are coarse for that reason" in note


def test_the_gate_files_load_and_pin_their_workloads():
    pinned = {}
    for name in ("aa", "pilot"):
        g = gate.load_gate(BENCH / "gates" / f"{name}.toml")
        assert g["gate"]["kind"] in gate.KINDS
        pinned |= {(name, w["name"]): w["sizes"] for w in g["workloads"]}
    with pytest.raises(SystemExit, match="candidates is empty"):
        gate.load_gate(BENCH / "gates" / "defaults.toml")
    raw = tomllib.loads((BENCH / "gates" / "defaults.toml").read_text())
    assert [m["margin"] for m in raw["metrics"]] == [1.05, 1.05, 1.5, 1.5]
    assert raw["plan"] == {
        "joint_power": 0.8,
        "min_rounds": 8,
        "max_rounds": 12,
        "widen": {"makespan_s": 1.10, "job_mean_s": 1.10},
        "frozen": "defaults.plan.toml",
    }
    loads = {w["name"]: w for w in raw["workloads"]}
    assert "params" not in loads["memheavy"]
    full = bench.workload_args(loads["full"]["args"])
    assert pinned[("aa", "full")] == pinned[("pilot", "full")] == full
    assert (full["agents"], full["duration_s"], full["mem_gib"], full["cap_gib"]) == (
        8,
        240.0,
        2.5,
        7.0,
    )
    heavy = bench.workload_args(loads["memheavy"]["args"])
    assert heavy == full | {"mem_gib": 3.0}
    assert (
        gate.arm_in(
            {"workloads": [{"name": "m", "params": "mem=declared"}]},
            "m",
            CONTROL,
        )
        == "lh-enforced@w=4,mem=declared"
    )


def test_plans_choose_whole_williams_cycles():
    g = _gate()
    for sd in (0.03, 0.06, 0.08):
        three = gate.plan(_sds(sd, sd), g["metrics"], g["plan"], 0.05, 2, cycle=6)
        assert three["rounds"] == 12 and three["counts"] == [12]
        two = gate.plan(_sds(sd, sd), g["metrics"], g["plan"], 0.05, 2, cycle=2)
        assert two["rounds"] % 2 == 0 and 8 <= two["rounds"] <= 12
    short = gate.plan(_sds(0.03, 0.03, lat=0.6), g["metrics"], g["plan"], 0.05, 2, cycle=6)
    assert short["rounds"] == 12 and not short["reached"]
    with pytest.raises(ValueError, match="raise max_rounds to at least 12"):
        gate.plan(_sds(0.03, 0.03), g["metrics"], g["plan"] | {"max_rounds": 10}, 0.05, cycle=6)
    assert gate.round_counts(8, 12, 2) == [8, 10, 12]
    assert gate.round_counts(8, 12, 6) == [12]
    assert gate.round_counts(8, 12, 2, base=8) == [8, 10, 12]
    assert gate.round_counts(8, 12, 6, base=12) == [12]


def test_the_blind_look_adds_only_whole_cycles():
    noisy = gate.blind_sd(_gate(), _records(8, noise=0.1))
    assert noisy["rounds_needed"] == 12 and noisy["plan"]["counts"] == [8, 10, 12]
    three = _gate(
        candidates=(CAND, "lh-enforced@w=3,h=1"),
        rounds={"planned": 12, "look_after": 4, "extend_to": 12, "seed_base": 1000},
    )
    records = _records(12, noise=0.1) + [
        x for x in _records(12, noise=0.1, cand="lh-enforced@w=3,h=1") if x["condition"] != CONTROL
    ]
    look = gate.blind_sd(three, records)
    assert look["plan"]["counts"] == [12] and look["rounds_needed"] == 12


def test_the_blind_look_is_fixed_the_first_time_it_is_ready(tmp_path):
    g = _gate()
    records = _records(8, noise=0.01)
    first = gate.look_once(g, [tmp_path], records)
    assert first["frozen"] and (tmp_path / "blind-t.json").exists()
    later = gate.look_once(g, [tmp_path], _records(8, noise=0.1))
    assert later["rounds_needed"] == first["rounds_needed"] == 8
    assert later["pooled_sd"] == first["pooled_sd"]
    unready = tmp_path / "unready"
    unready.mkdir()
    look = gate.look_once(g, [unready], _records(3))
    assert not look["ready"] and not (unready / "blind-t.json").exists()


def test_psi_without_the_controls_is_never_a_break():
    g = _gate()
    assert gate.psi_break(g, {"mem_some_max": 12.0}, None) == ""
    assert gate.psi_break(g, {"mem_some_max": 12.0}, {"mem_some_max": None}) == ""
    records = _records(9)
    records[0]["row"] = records[0]["row"] | {"mem_some_max": None}  # the control of round 1
    records[1]["row"] = records[1]["row"] | {"mem_some_max": 12.0}
    r = _verdict(records)
    w = r["workloads"][0]
    assert not w["hard"] and r["verdict"] == "pass", r["why"]
    assert w["invalid"][0]["round"] == 1 and "cannot be judged" in w["invalid"][0]["why"]


def test_a_candidate_without_psi_is_missing_not_a_pass():
    records = _records(8)
    records[1]["row"] = records[1]["row"] | {"mem_some_max": None}
    r = _verdict(records)
    assert r["verdict"] == "incomplete"
    assert r["workloads"][0]["invalid"] == [
        {"round": 1, "why": "the candidate's memory PSI is missing"}
    ]


def _superseded(record: dict, **row) -> dict:
    return record | {
        "superseded": True,
        "replaced_why": "it stopped before it finished",
        "row": record["row"] | row,
    }


def test_a_rerun_does_not_hide_a_superseded_runs_break():
    records = _records(8)
    records.append(_superseded(records[3], oom_kills=1))  # the candidate of round 2
    r = _verdict(records)
    w = r["workloads"][0]
    assert r["verdict"] == "fail"
    assert w["hard"] == ["round 2: a superseded run: 1 OOM kills"]
    assert w["superseded"] == [
        {"round": 2, "arm": "candidate", "why": "it stopped before it finished"}
    ]
    records = _records(9)
    records.append(_superseded(records[2], failed=1))  # the control of round 2
    w = _verdict(records)["workloads"][0]
    assert w["invalid"] == [
        {"round": 2, "why": "a superseded control run broke a hard rule: 1 failed jobs"}
    ]
    report = gate.report(_gate(), gate.evaluate(_gate(), records), [Path("d")])
    assert "round 2: a superseded control run, replaced because it stopped" in report


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("git_dirty", True, "states of the checkout"),
        ("machine", [16, 1, 1, "6.1", "none"], "2 machines"),
    ],
)
def test_compare_refuses_mixed_checkouts_and_machines(field, value, why):
    records = [r | {"git_dirty": False, "machine": [8, 2, 2, "6.12", "kvm"]} for r in _records(8)]
    records[0][field] = value
    with pytest.raises(gate.Refused, match=why):
        gate.check_provenance(_gate(), records, [_info()])


def test_compare_refuses_mixed_harness_settings():
    a = _info(name="a", harness={"no_scope": False})
    b = _info(name="b", harness={"no_scope": True})
    with pytest.raises(gate.Refused, match="different harness settings"):
        gate.check_provenance(_gate(), _records(8), [a, b])


def test_a_gate_run_refuses_a_dirty_checkout(monkeypatch, capsys, tmp_path):
    import manifest

    monkeypatch.setattr(manifest, "git_state", lambda *a: {"sha": "a" * 40, "dirty": True})
    with pytest.raises(SystemExit) as stop:
        bench.main(["--quick", "--no-wait", "--no-scope", "--dir", str(tmp_path / "r")])
    assert stop.value.code == 2 and "--allow-dirty" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()
    args = bench.build_parser().parse_args(["--allow-dirty", "--no-scope"])
    assert bench.harness_settings(args)["allow_dirty"] is True


def _gate_file(tmp_path: Path, frozen: str = "g.plan.toml") -> Path:
    heavy = FULL.replace("--mem-gib 2.5", "--mem-gib 3")
    path = tmp_path / "g.toml"
    path.write_text(
        '[gate]\nname = "g"\nkind = "confirmatory"\nintent = "i"\nobjective = "o"\n'
        'stop = "s"\ndecision = "d"\n'
        f'[arms]\ncontrol = "{CONTROL}"\ncandidates = ["{CAND}"]\n'
        f'[[workloads]]\nname = "full"\nargs = "{FULL}"\n'
        f'[[workloads]]\nname = "memheavy"\nargs = "{heavy}"\n'
        "[rounds]\nplanned = 8\nlook_after = 4\nextend_to = 12\nseed_base = 1000\n"
        "[plan]\njoint_power = 0.8\nmin_rounds = 8\nmax_rounds = 12\n"
        "widen = { makespan_s = 1.10, job_mean_s = 1.10 }\n"
        f'frozen = "{frozen}"\n'
        + "".join(
            f'[[metrics]]\nname = "{m}"\nmargin = {margin}\n'
            for m, margin in zip(SCALED, (1.05, 1.05, 1.5, 1.5), strict=True)
        )
        + "[hard]\nfailed_jobs = 0\noom_kills = 0\npsi_max = 10.0\npsi_over_control = 5.0\n"
    )
    return path


def test_the_gate_runs_by_its_frozen_plan(tmp_path, monkeypatch, capsys):
    aa_dir, full_dir = tmp_path / "aa", tmp_path / "full"
    for d in (aa_dir, full_dir):
        d.mkdir()
        (d / "spec.json").write_text("{}")
    heavy = bench.workload_args(FULL.replace("--mem-gib 2.5", "--mem-gib 3"))
    cand = _records(8, {"makespan_s": 1.07}, noise=0.005)
    cand += [r | {"workload": "memheavy", "dir": "m"} for r in cand]

    def fake_collect(dirs):
        if dirs[0] == aa_dir:
            return _aa_records(4, None, 0.045), [_info(name="aa", seed_base=200, rounds_seen=4)]
        return cand, [_info(name="d"), _info(name="m", workload="memheavy", args=heavy)]

    monkeypatch.setattr(gate, "collect", fake_collect)
    g = _gate_file(tmp_path)
    with pytest.raises(SystemExit, match="gives no verdict without its frozen plan"):
        gate.load_gate(g)
    aa = [str(aa_dir), "--gate", str(BENCH / "gates" / "aa.toml"), "--plan", str(g)]
    assert gate.main([*aa, "--write", str(tmp_path / "g.plan.toml")]) == 0
    frozen = tomllib.loads((tmp_path / "g.plan.toml").read_text())
    assert frozen["arms"] == 2 and frozen["rounds"] == 8 and frozen["workloads"] == 2
    assert frozen["margins"] == {
        "makespan_s": 1.1,
        "job_mean_s": 1.1,
        "wake_same_p99_ms": 1.5,
        "git_p95_ms": 1.5,
    }
    assert frozen["aa_data_sha256"] == gate.data_digest([aa_dir]) and frozen["aa_git_sha"]
    capsys.readouterr()
    assert gate.main([str(full_dir), "--gate", str(g)]) == 0
    out = capsys.readouterr().out
    assert "## `lh-enforced@w=2-4`: pass" in out
    assert "| makespan_s | 8 | 1.0" in out and "| 1.1 |" in out
    assert "Planned rounds and margins come from `g.plan.toml`." in out
    assert "Joint power at 8 rounds, at the frozen plan's A/A SDs" in out
    # A plan for another arm count is refused.
    with pytest.raises(SystemExit, match="planned for 2 arms, and the gate has 3"):
        gate.apply_frozen(
            _gate(candidates=(CAND, "lh-enforced@w=3,h=1")) | {"gate": {"name": "g"}},
            tmp_path / "g.plan.toml",
        )


def test_no_plan_is_written_from_an_aa_that_failed(tmp_path, monkeypatch):
    aa_dir = tmp_path / "aa"
    aa_dir.mkdir()
    (aa_dir / "spec.json").write_text("{}")
    monkeypatch.setattr(
        gate,
        "collect",
        lambda dirs: (_aa_records(4, None, 0.1), [_info(seed_base=200, rounds_seen=4)]),
    )
    argv = [str(aa_dir), "--gate", str(BENCH / "gates" / "aa.toml")]
    argv += ["--plan", str(_gate_file(tmp_path)), "--write", str(tmp_path / "g.plan.toml")]
    assert gate.main(argv) == 2
    assert not (tmp_path / "g.plan.toml").exists()


FIXTURE = Path(__file__).parent / "fixtures" / "bench-c04635f"


def test_results_written_by_c04635f_still_load_and_compare(tmp_path):
    """The A/A and pilot ran on c04635f; their results must still be judged the same way."""
    gate_file = tmp_path / "aa.toml"
    gate_file.write_text(
        '[gate]\nname = "old"\nkind = "aa"\nintent = "i"\nobjective = "o"\nstop = "s"\n'
        'decision = "d"\n[arms]\ncontrol = "lh-enforced@w=4,arm=a"\n'
        'candidates = ["lh-enforced@w=4,arm=b"]\n'
        '[[workloads]]\nname = "tiny"\nargs = "--tiny --agents 1"\n'
        "[rounds]\nplanned = 2\nseed_base = 200\n"
        "[aa]\nmax_paired_sd = 10.0\n"
        + "".join(f'[[metrics]]\nname = "{m}"\nmargin = 1.5\n' for m in SCALED[:2])
        + "[hard]\npsi_max = 10.0\npsi_over_control = 5.0\n"
    )
    records, infos = gate.collect([FIXTURE])
    assert len(records) == 4 and not any(r["superseded"] for r in records)
    assert infos[0]["harness"] is None and records[0]["git_dirty"] is None
    out = tmp_path / "report"
    done = _bench("compare", str(FIXTURE), "--gate", str(gate_file), "--out", str(out))
    result = json.loads(out.with_suffix(".json").read_text())
    assert result["results"][0]["workloads"][0]["rounds"] == [1, 2], done.stdout
    assert [m["n"] for m in result["results"][0]["metrics"]] == [2, 2]
    planned = _bench(
        "compare",
        str(FIXTURE),
        "--gate",
        str(gate_file),
        "--out",
        str(out),
        "--plan",
        str(BENCH / "gates" / "defaults.toml"),
        "--arms",
        "3",
    ).stdout
    assert "Rounds: 12, chosen from whole Williams cycles of 6 rounds (12)." in planned
    assert not list(FIXTURE.glob("compare-*")) and not list(FIXTURE.glob("blind-*"))


# ---------------------------------------------------------------------------------------------
# A tiny run, end to end


def test_rounds_run_one_at_a_time_into_one_directory_and_compare(tmp_path):
    out = tmp_path / "res"
    common = [
        "--tiny",
        "--no-scope",
        "--agents",
        "1",
        "--rounds",
        "2",
        "--seed-base",
        "40",
        "--conditions",
        "unmanaged,lh-advisory@w=2-4,h=1",
        "--dir",
        str(out),
        "--machine-class",
        "kvm-test",
        "--note",
        "boost off",
    ]
    _bench(*common, "--round", "1")
    _bench(*common, "--round", "2")
    labels = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert labels == [
        "lh-advisory_w2-4_h1-r1",
        "lh-advisory_w2-4_h1-r2",
        "unmanaged-r1",
        "unmanaged-r2",
    ]
    host = socket.gethostname().split(".")[0].lower()
    home = str(Path.home())
    for label in labels:
        text = (out / label / "manifest.json").read_text()
        m = json.loads(text)
        assert m["valid"] is True and m["machine"]["class"] == "kvm-test"
        assert m["seed"] == 40 + m["round"] and m["notes"] == ["boost off"]
        assert m["git"] is None or len(m["git"]["sha"]) == 40
        assert m["leasehold"]
        if len(host) > 3:
            assert host not in text.lower()
        assert home not in text
    lh = json.loads((out / "lh-advisory_w2-4_h1-r1" / "manifest.json").read_text())
    assert lh["config"]["summary"]["work_headroom"] == 1
    assert lh["config"]["hold_weight"] == {"cpu": "2-4", "mem": "2-4"}
    assert 'ledger_dir = "ledger"' in (out / "lh-advisory_w2-4_h1-r1" / "config.toml").read_text()
    spec = (out / "spec.json").read_text()
    assert home not in spec and (len(host) <= 3 or host not in spec.lower())
    # Another invocation must ask for the same schedule.
    bad = _bench(*[a if a != "40" else "41" for a in common], "--round", "1", check=False)
    assert bad.returncode == 2 and "seed_base" in bad.stderr
    gate_file = tmp_path / "g.toml"
    gate_file.write_text(
        '[gate]\nname = "smoke"\nkind = "aa"\nintent = "i"\nobjective = "o"\nstop = "s"\n'
        'decision = "d"\n[arms]\ncontrol = "unmanaged"\ncandidates = ["lh-advisory@w=2-4,h=1"]\n'
        '[[workloads]]\nname = "tiny"\nargs = "--tiny --agents 1"\n'
        "[rounds]\nplanned = 2\nseed_base = 40\n"
        '[[metrics]]\nname = "makespan_s"\nmargin = 1.05\n'
    )
    done = _bench("compare", str(out), "--gate", str(gate_file))
    assert done.stdout.startswith("# Gate `smoke` (aa)\n\n- Intent: i")
    result = json.loads((out / "compare-smoke.json").read_text())
    assert result["results"][0]["metrics"][0]["n"] == 2
    assert math.isfinite(result["results"][0]["metrics"][0]["sd_log"])
    blind = _bench("compare", str(out), "--gate", str(gate_file), "--blind-sd").stdout
    assert "Pooled paired SDs" in blind and "lh-advisory" not in blind
    planned = _bench(
        "compare",
        str(out),
        "--gate",
        str(gate_file),
        "--plan",
        str(BENCH / "gates/defaults.toml"),
        "--arms",
        "2",
    ).stdout
    assert "## Plan for `defaults.toml`" in planned and "No SD for: job_mean_s" in planned
    assert "its SDs stand for every workload of the gate (`full`, `memheavy`)" in planned
    # Results run with other sizes are refused.
    other = gate_file.read_text().replace("--agents 1", "--agents 2")
    gate_file.write_text(other)
    refused = _bench("compare", str(out), "--gate", str(gate_file), check=False)
    assert refused.returncode == 2 and "other args" in refused.stderr
    # A resume on other sizes is refused.
    bad = _bench(*common, "--mem-gib", "0.02", "--round", "2", check=False)
    assert bad.returncode == 2 and "args" in bad.stderr
    # ... or on another machine class, or other harness settings.
    other = [a if a != "kvm-test" else "kvm-other" for a in common]
    bad = _bench(*other, "--round", "2", check=False)
    assert bad.returncode == 2 and "machine with class 'kvm-test'" in bad.stderr
    bad = _bench(*[a for a in common if a != "--no-scope"], "--round", "2", check=False)
    assert bad.returncode == 2 and "harness" in bad.stderr
    spec = json.loads((out / "spec.json").read_text())
    assert spec["harness"] == {
        "no_scope": True,
        "no_wait": True,
        "drop_caches": False,
        "allow_dirty": False,
    }
    assert "git_dirty" in spec
