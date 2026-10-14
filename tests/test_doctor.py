"""`leasehold doctor`'s checks (leasehold.doctor), against fake kernel trees and configs."""

from __future__ import annotations

import os
from pathlib import Path

from support import machine_config

from leasehold import contain, doctor, paths


def fake_roots(tmp_path, *, psi=("cpu", "memory", "io"), v2=True, slices=()):
    proc, cgroup = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "pressure").mkdir(parents=True)
    cgroup.mkdir()
    for name in psi:
        (proc / "pressure" / name).write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    if v2:
        (cgroup / "cgroup.controllers").write_text("cpu memory io\n")
    for name in slices:
        (cgroup / name).mkdir()
    return doctor.Roots(proc=proc, cgroup=cgroup)


def test_psi_present_and_absent(tmp_path):
    assert doctor.check_psi(fake_roots(tmp_path)).level == "ok"
    missing = doctor.check_psi(fake_roots(tmp_path / "b", psi=("cpu",)))
    assert missing.level == "warn" and "memory, io" in missing.detail and missing.fix


def test_cgroup_v1_is_a_warning(tmp_path):
    (check,) = doctor.check_cgroups(fake_roots(tmp_path, v2=False), None)
    assert check.level == "warn" and "cgroup v2" in check.detail


def test_configured_slices_that_do_not_exist_are_named(tmp_path):
    config = machine_config(
        tmp_path, lend={"pressure_cgroups": ["work.slice", "user.slice"]}, load={"slices": []}
    )
    checks = doctor.check_cgroups(fake_roots(tmp_path, slices=("user.slice",)), config)
    assert [c.level for c in checks] == ["ok", "warn"]
    assert checks[1].detail.endswith("work.slice")


def test_a_broken_config_fails_and_says_where(tmp_path):
    bad = tmp_path / "m.toml"
    bad.write_text("[ceiling]\nslots = 0\n")
    check, config = doctor.check_config(str(bad), {"HOME": str(tmp_path)})
    assert check.level == "fail" and str(bad) in check.detail and config is None


def test_no_config_is_fine_and_says_what_runs_instead(tmp_path):
    check, config = doctor.check_config(None, {"HOME": str(tmp_path)})
    assert check.level == "ok" and "defaults" in check.detail and config is None


def test_a_user_ledger_directory_this_user_cannot_write_fails(tmp_path):
    if os.getuid() == 0:
        return
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    config = machine_config(locked / "state")
    config = config.model_copy(update={"mode": "user"})
    check = doctor.check_ledger(config)
    assert check.level == "fail" and "not writable" in check.detail
    system = doctor.check_ledger(config.model_copy(update={"mode": "system"}))
    assert system.level == "warn" and "cannot verify" in system.detail  # not yours to write
    default = config.model_copy(update={"mode": "system", "ledger_dir": paths.SYSTEM_LEDGER_DIR})
    assert doctor.check_ledger(default).level == "ok"


def test_the_daemon_check_reports_the_machine_and_a_skewed_clock(daemon):
    checks = doctor.check_daemon(daemon.path)
    assert [c.name for c in checks] == ["daemon"] and checks[0].level == "ok"
    assert "test-machine" in checks[0].detail
    skewed = doctor.check_daemon(daemon.path, now=lambda: 0.0)
    assert [c.name for c in skewed] == ["daemon", "clock"] and skewed[1].level == "warn"


def test_a_daemon_that_does_not_answer_fails_with_a_fix(sock_dir):
    (check,) = doctor.check_daemon(sock_dir / "none.sock")
    assert check.level == "fail" and check.fix


def test_doctor_warns_when_proc_hides_other_users(tmp_path):
    (tmp_path / "proc").mkdir()
    roots = doctor.Roots(proc=tmp_path / "proc", cgroup=tmp_path)
    rules = [{"name": "p", "match": {"comm": "x"}, "weight": 1}]
    config = machine_config(tmp_path, observe=rules)
    mounts = tmp_path / "proc" / "mounts"
    mounts.write_text("proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n")
    assert doctor.check_proc(roots, config).level == "ok"
    mounts.write_text("proc /proc proc rw,relatime,hidepid=invisible 0 0\n")
    check = doctor.check_proc(roots, config)
    if os.geteuid() == 0:
        assert check.level == "ok"
    else:
        assert check.level == "warn" and "hidepid=invisible" in check.detail and check.fix
        assert "[[observe]]" in check.detail
        plain = doctor.check_proc(roots, machine_config(tmp_path))
        assert plain.level == "warn" and "[[observe]]" not in plain.detail


def test_a_socket_path_too_long_for_a_unix_socket_fails_with_a_fix(tmp_path):
    fits = tmp_path / "s.sock"
    assert doctor.check_socket_paths([fits]) is None
    long = Path("/tmp") / ("d" * 100) / "leasehold.sock"
    check = doctor.check_socket_paths([fits, long, long])
    assert check.level == "fail" and check.detail.endswith(f"{long} (120 bytes)")
    assert "shorter path" in check.fix


def test_doctor_names_a_configured_socket_too_long(tmp_path):
    long = tmp_path / ("d" * 120) / "s.sock"
    config = tmp_path / "m.toml"
    config.write_text(f'mode = "user"\nsocket = "{long}"\n')
    checks = doctor.run(socket=None, config=str(config), env={"HOME": str(tmp_path)})
    assert any(c.level == "fail" and str(long) in c.detail and "unix socket" in c.detail
               for c in checks)  # fmt: skip


def test_a_socket_mode_open_to_every_user_is_a_warning(tmp_path):
    assert doctor.check_socket_mode(None) is None
    assert doctor.check_socket_mode(machine_config(tmp_path)) is None
    for mode in ("0660", "0600", "0640"):
        assert doctor.check_socket_mode(machine_config(tmp_path, socket_mode=mode)) is None
    check = doctor.check_socket_mode(machine_config(tmp_path, socket_mode="0666"))
    assert check.level == "warn" and "0666" in check.detail and "0660" in check.fix


def test_containment_is_reported_with_a_hint_when_advisory(tmp_path):
    def probe(answer):
        return lambda env: answer

    enforced = doctor.check_contain(None, {}, probe(contain.Containment(True, "")))
    assert enforced.level == "ok" and enforced.detail.startswith("enforced (systemd)")
    reason = "user manager is not delegated memory"
    advisory = doctor.check_contain(None, {}, probe(contain.Containment(False, reason)))
    assert advisory.level == "warn" and advisory.detail.startswith(f"advisory: {reason}")
    assert "Delegate=cpu memory" in advisory.fix
    odd = doctor.check_contain(None, {}, probe(contain.Containment(False, "something new")))
    assert odd.fix == "see Containment in docs/reference.md"
    off = machine_config(tmp_path, contain={"mode": "off"})
    assert doctor.check_contain(off, {}, probe(None)).detail.startswith("off")
