"""bench/sim: the policy simulator's speedup and machine model, its use of the library's own
admission, its measures, and its calibration against the measured runs. Skipped without SimPy
(the `bench` dependency group)."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("simpy")

from bench.sim import engine, metrics, study
from bench.sim.__main__ import main
from bench.sim.calibrate import discover, overheads, simulate
from bench.sim.engine import Overheads, Simulation, Unsupported
from bench.sim.model import Job, Kind, Machine, Speedup
from bench.sim.workload import (
    GIB,
    WORKLOADS,
    Params,
    Weights,
    config_for,
    plan_steps,
    setup_for,
)
from leasehold import admission

RESULTS = Path(__file__).resolve().parent.parent / "bench" / "results" / "8cpu-16g"
SHORT = replace(WORKLOADS["light"], agents=4, duration_s=60)


def _run(condition="lh-enforced", weights="4", workload=SHORT, cpus=8, seed=1, **kw):
    params = Params()
    machine = params.machine(cpus, 16 * GIB, 1.0)
    steps, mem_bytes = plan_steps(workload, seed, params, machine, Weights.parse(weights))
    setup = setup_for(condition, cpus=cpus, memory_bytes=16 * GIB, **kw)
    sim = Simulation(setup, machine, params.kinds(mem_bytes), steps, seed=seed)
    return sim, sim.run()


# --- the model ----------------------------------------------------------------------------------


@pytest.mark.parametrize("sigma", [0.0, 0.5, 1.0, 2.0])
def test_a_speedup_starts_at_one_never_passes_the_workers_and_levels_off_at_a(sigma):
    s = Speedup(A=6, sigma=sigma)
    assert s(1) == pytest.approx(1)
    values = [s(n) for n in range(1, 40)]
    assert all(v <= n + 1e-9 for n, v in zip(range(1, 40), values, strict=True))
    assert all(b >= a - 1e-9 for a, b in itertools.pairwise(values))
    assert values[-1] == pytest.approx(6)


def test_with_no_variance_the_speedup_is_the_workers_up_to_a():
    s = Speedup(A=5, sigma=0)
    assert [s(n) for n in (1, 3, 5, 8)] == pytest.approx([1, 3, 5, 5])


def test_smt_siblings_add_their_yield_and_threads_past_the_cpus_add_nothing():
    m = Machine(cpus=8, cores=4, smt_yield=0.5)
    assert m.capacity(3) == 3
    assert m.capacity(6) == 5
    assert m.capacity(8) == m.capacity(20) == 6
    assert Machine(cpus=8).capacity(10) == 8
    # Two jobs of 4 threads on 4 cores share 6 cores' worth evenly.
    assert m.rates([4, 4]) == pytest.approx([3, 3])


def test_a_job_alone_runs_its_work_over_its_rate():
    params = Params()
    machine = Machine(cpus=8)
    kinds = {"cpu": Kind(Speedup(A=8)), "mem": Kind(Speedup(A=8))}
    job = Job(agent=0, seq=0, kind="cpu", work=80.0, weight=4, max_weight=4)
    setup = setup_for("lh-enforced", cpus=8, memory_bytes=16 * GIB)
    sim = Simulation(setup, machine, kinds, [[(0.0, job)]], overheads=Overheads(0.5, 0.1))
    sim.run()
    assert job.started == pytest.approx(0.5)
    assert job.ended - job.started == pytest.approx(80 / 4)
    assert job.done - job.ended == pytest.approx(0.1)
    assert params.noise_cv > 0


# --- admission is the library's -----------------------------------------------------------------


def test_weight_4_on_6_slots_runs_one_job_at_a_time_and_counts_within_the_budget():
    _, result = _run(workload=replace(SHORT, agents=8))
    assert max(s.held for s in result.samples) == 4
    assert all(s.counted <= result.budget for s in result.samples)
    assert all(j.granted == 4 and j.workers == 4 for j in result.jobs)
    assert not any(result.notes)


def test_a_lone_range_is_granted_what_admission_alone_gives_it():
    config = config_for(cpus=8, memory_bytes=16 * GIB)
    policy = config.policy(memory_bytes=16 * GIB, lease_memory=12 * GIB)
    expected = admission.alone(policy, "work", 2, 6)
    params = Params()
    machine = params.machine(8, 16 * GIB, 0.0)
    job = Job(agent=0, seq=0, kind="cpu", work=10.0, weight=2, max_weight=6)
    setup = setup_for("lh-enforced", cpus=8, memory_bytes=16 * GIB)
    Simulation(setup, machine, params.kinds(GIB), [[(0.0, job)]]).run()
    assert job.granted == expected == 5  # 6 slots, headroom auto (1) kept free


def test_ranges_start_jobs_sooner_than_a_fixed_weight_on_the_same_plans():
    _, fixed = _run(weights="4", workload=replace(SHORT, agents=8))
    _, ranged = _run(weights="2-4", workload=replace(SHORT, agents=8))
    fixed_wait = metrics.summarize(fixed)["wait_mean_s"]
    ranged_wait = metrics.summarize(ranged)["wait_mean_s"]
    assert ranged_wait < fixed_wait
    assert {j.granted for j in ranged.jobs} <= {2, 3, 4}


def test_without_leasehold_every_job_starts_at_once_on_every_cpu():
    _, result = _run("unmanaged")
    bare = Overheads().start_unmanaged
    assert all(j.started - j.issued == pytest.approx(bare) for j in result.jobs)
    assert {j.workers for j in result.jobs} == {8}


def test_memory_the_jobs_use_past_what_they_count_shows_as_over_budget():
    heavy = replace(WORKLOADS["heavy"], agents=8, duration_s=90)
    _, result = _run(weights="1-4", workload=heavy, seed=3)
    s = metrics.summarize(result)
    assert s["counted_peak_gib"] <= s["budget_gib"] + 1e-9
    assert s["actual_peak_gib"] > s["counted_peak_gib"]


def test_elastic_conditions_are_refused_without_admission_targets(monkeypatch):
    monkeypatch.delattr(admission, "targets", raising=False)
    with pytest.raises(Unsupported, match="elastic"):
        _run("lh-elastic")


def test_with_admission_targets_a_lease_is_resized_after_the_settle_time(monkeypatch):
    seen = []

    def targets(state, policy):
        seen.append(len(state.held))
        return {("sim", "0.0"): 6}

    monkeypatch.setattr(admission, "targets", targets, raising=False)
    params = Params()
    machine = Machine(cpus=8)
    kinds = {"cpu": Kind(Speedup(A=8)), "mem": Kind(Speedup(A=8))}
    job = Job(agent=0, seq=0, kind="cpu", work=200.0, weight=2, max_weight=2)
    setup = replace(setup_for("lh-enforced", cpus=8, memory_bytes=16 * GIB), elastic=True)
    Simulation(setup, machine, kinds, [[(0.0, job)]], overheads=Overheads(0, 0)).run()
    assert seen
    assert job.workers == 6
    # 5 s at 2 workers, then the rest at 6.
    assert job.ended == pytest.approx(5 + (200 - 10) / 6)
    assert params.noise_cv >= 0


def test_a_run_is_the_same_for_the_same_seed():
    _, a = _run(seed=4)
    _, b = _run(seed=4)
    assert metrics.summarize(a) == metrics.summarize(b)


# --- measures -----------------------------------------------------------------------------------


def test_jain_index_and_percentiles():
    assert metrics.jain([3, 3, 3]) == pytest.approx(1)
    assert metrics.jain([1, 0, 0, 0]) == pytest.approx(0.25)
    assert metrics.pct([1, 2, 3, 4], 95) == 4
    assert metrics.pct([], 95) is None


def test_the_t_quantile_and_the_interval():
    assert metrics.t975(29) == pytest.approx(2.045, abs=0.002)
    assert metrics.t975(4) == pytest.approx(2.776, abs=0.01)
    agg = metrics.aggregate([{"jobs": 3, "x": v} for v in (1.0, 2.0, 3.0)])
    assert agg["x"] == 2.0
    assert agg["x_ci"] == pytest.approx(4.303 * 1 / math.sqrt(3), rel=1e-3)


def test_slots_idle_while_something_waits_are_counted():
    # Weight 4 on 6 slots with many agents: two slots sit idle while the line is not empty.
    _, result = _run(workload=replace(SHORT, agents=8))
    assert metrics.summarize(result)["idle_while_waiting"] > 0.05


def test_the_non_dominated_set_drops_beaten_and_over_budget_configurations():
    rows = [
        {
            "makespan_s": 1,
            "wait_p95_s": 1,
            "idle_while_waiting": 0,
            "saturated_share": 0,
            "over_budget": 0,
        },
        {
            "makespan_s": 2,
            "wait_p95_s": 2,
            "idle_while_waiting": 0,
            "saturated_share": 0,
            "over_budget": 0,
        },
        {
            "makespan_s": 0,
            "wait_p95_s": 3,
            "idle_while_waiting": 0,
            "saturated_share": 0,
            "over_budget": 0,
        },
        {
            "makespan_s": 0,
            "wait_p95_s": 0,
            "idle_while_waiting": 0,
            "saturated_share": 0,
            "over_budget": 0.1,
        },
    ]
    assert study.pareto(rows) == [rows[0], rows[2]]


def test_the_screening_design_stays_in_its_ranges():
    pytest.importorskip("scipy")
    configs = study.screen_configs(6, 16)
    assert len(configs) > 8
    for c in configs:
        w = Weights.parse(c.weights)
        assert 1 <= w.least <= 4 and w.least <= w.most <= 8 - c.kept
        assert 0 <= int(c.headroom) <= 3 and 0 <= c.kept <= 3


# --- calibration and the command line ----------------------------------------------------------


def test_the_measured_one_at_a_time_run_is_reproduced_within_15_percent():
    measured = {m.name: m for m in discover(RESULTS)}
    m = measured["k8-d240/lh-enforced"]
    got = simulate(m, Params(), overheads(list(measured.values())), noise=False)
    assert got["makespan_s"] == pytest.approx(m.summary["makespan_s"], rel=0.15)
    assert got["wait_mean_s"] == pytest.approx(m.summary["wait_mean_s"], rel=0.15)


def test_run_writes_replications_a_summary_and_a_timeline(tmp_path, capsys):
    main(["run", "--weights", "2-4", "--agents", "3", "--duration", "40", "--reps", "2",
          "--out", str(tmp_path)])  # fmt: skip
    summary = json.loads((tmp_path / "summary.json").read_text())["summary"]
    assert summary["reps"] == 2 and summary["makespan_s"] > 0
    assert (tmp_path / "runs.csv").read_text().count("\n") == 3
    assert "held" in (tmp_path / "timeline.csv").read_text().splitlines()[0]
    assert "makespan_s" in capsys.readouterr().out


def test_run_skips_an_elastic_condition_without_targets(tmp_path, capsys):
    if hasattr(engine.admission, "targets"):
        pytest.skip("this library has elastic leases")
    main(["run", "--condition", "lh-elastic", "--reps", "1", "--duration", "20",
          "--out", str(tmp_path)])  # fmt: skip
    assert "skipped" in capsys.readouterr().out


def test_time_with_at_most_one_cpu_idle_is_counted():
    # Three 2-slot jobs on 6 slots of 8 CPUs, one thread of other work: 7 busy, 1 idle.
    machine = Machine(cpus=8)
    kinds = {"cpu": Kind(Speedup(A=8)), "mem": Kind(Speedup(A=8))}
    steps = [[(0.0, Job(agent=a, seq=0, kind="cpu", work=40.0, weight=2, max_weight=2))]
             for a in range(3)]  # fmt: skip
    setup = setup_for("lh-enforced", cpus=8, memory_bytes=16 * GIB)
    result = Simulation(setup, machine, kinds, steps, overheads=Overheads(0, 0)).run()
    assert metrics.summarize(result, background=1.0)["saturated_share"] == pytest.approx(1)
    assert metrics.summarize(result, background=0.0)["saturated_share"] == pytest.approx(0)
