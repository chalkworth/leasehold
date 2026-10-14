"""Bringing a 0.2 user config to today's defaults (`leasehold.upgrade`, `setup
--upgrade-config`, and what `doctor` and `setup` say): a config exactly as 0.2's `init` wrote it
is rendered afresh for the same machine; an edited one keeps every line and gains only what it
lacks; the old file is kept beside it; nothing is written without the flag, nor with
`--dry-run`."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from support import CHILD_ENV

from leasehold import doctor, machine, template, upgrade
from leasehold.machine import MachineConfig

ABOUT = (
    "Sized for 8 CPUs and 16 GiB: one slot per CPU; 2 CPUs are kept for you, and at most three "
    "quarters of the memory to any one lease."
)


def old_config(path: Path) -> Path:
    path.write_text(
        template.render_user_0_2(name="box", slots=6, cpus=6, memory="11g", about=ABOUT)
    )
    return path


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "leasehold", *args],
        env=CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_0_2_config_is_known_by_what_it_lacks(tmp_path):
    config = machine.load(old_config(tmp_path / "m.toml"))
    assert upgrade.outdated(config) == [
        "memory is not counted (no [memory] table)",
        "the built-in build and test rules weigh 4 of 6 slots, so one runs at a time",
    ]
    fresh = template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 << 30})
    assert upgrade.outdated(MachineConfig.model_validate(tomllib.loads(fresh))) == []


def test_the_machines_own_config_is_never_called_outdated(tmp_path):
    text = template.render(name="b", mode="system", slots=6, cpus=8, memory="12g", about="x")
    assert upgrade.outdated(MachineConfig.model_validate(tomllib.loads(text))) == []


def test_an_unedited_0_2_config_is_rendered_afresh_for_the_same_machine(tmp_path):
    path = old_config(tmp_path / "m.toml")
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and plan.pristine
    assert plan.text == template.render(
        name="box", mode="user", slots=6, cpus=6, memory="11g", about=ABOUT
    )
    backup = upgrade.apply(plan)
    assert backup == tmp_path / "m.toml.0.2.bak"
    assert backup.read_text() == plan.old
    new = machine.load(path)
    assert (new.commands.heavy_weight, new.commands.light_weight) == (3, 2)
    assert new.memory is not None and upgrade.outdated(new) == []
    assert upgrade.plan(path, new) is None


def test_an_edited_config_keeps_its_lines_and_gains_only_what_it_lacks(tmp_path):
    path = old_config(tmp_path / "m.toml")
    edited = path.read_text().replace("settle_s = 30", "settle_s = 45  # mine")
    edited = edited.replace("[commands]\n", "[commands]\ndefaults = true\n")
    path.write_text(edited)
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and not plan.pristine and plan.text is not None
    assert plan.changes == (
        "[commands] gains heavy_weight = 3 and light_weight = 2",
        'a [memory] table: budget = "auto", kept = "25%", assumed_per_slot "auto"',
        "the comment above [guard], which described 0.2's guard",
    )
    removed = [
        line
        for line in plan.diff().splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    assert len(removed) == 2  # only 0.2's two-line [guard] comment
    upgrade.apply(plan)
    new = machine.load(path)
    assert new.guard.settle_s == 45 and new.memory is not None
    assert new.commands.heavy_weight == 3
    assert "settle_s = 45  # mine" in path.read_text()


def test_a_commands_table_written_inline_is_left_to_be_edited_by_hand(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(
        'mode = "user"\ncommands = { wait_s = 10 }\n[ceiling]\nslots = 6\n'
        '[pools.work]\nreserved = 3\nmax = 6\n[kinds]\nwork = "work"\n[lend]\npools = []\n'
    )
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and plan.text is None
    assert "heavy_weight = 3 and light_weight = 2 under [commands]" in (plan.manual or "")
    done = run("--config", str(path), "setup", "--upgrade-config")
    assert done.returncode == 1 and "cannot be changed automatically" in done.stdout


def test_setup_upgrade_config_dry_run_writes_nothing_and_without_it_backs_up(tmp_path):
    path = old_config(tmp_path / "m.toml")
    before = path.read_text()
    dry = run("--config", str(path), "setup", "--upgrade-config", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert "+[memory]" in dry.stdout and "nothing written (--dry-run)" in dry.stdout
    assert path.read_text() == before and not (tmp_path / "m.toml.0.2.bak").exists()
    done = run("--config", str(path), "setup", "--upgrade-config")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "m.toml.0.2.bak").read_text() == before
    assert "the old file is" in done.stdout
    again = run("--config", str(path), "setup", "--upgrade-config")
    assert "has today's defaults; nothing to change" in again.stdout
    assert not (tmp_path / "m.toml.0.2.bak.1").exists()


def test_dry_run_alone_is_refused():
    done = run("setup", "--dry-run")
    assert done.returncode != 0 and "--dry-run goes with --upgrade-config" in done.stderr


def test_doctor_names_the_old_defaults_and_the_command(tmp_path):
    path = old_config(tmp_path / "m.toml")
    check = doctor.check_upgrade(str(path), {}, machine.load(path))
    assert check is not None and check.level == "warn"
    assert "has leasehold 0.2's defaults: memory is not counted" in check.detail
    assert check.fix is not None
    assert f"leasehold setup --upgrade-config --config {path} --dry-run" in check.fix


def test_doctor_warns_when_no_heavy_rule_can_start_under_the_old_guard(tmp_path):
    config = machine.load(old_config(tmp_path / "m.toml"))
    check = doctor.check_guard_weights(config, 16 << 30)
    assert check is not None and check.level == "warn"
    assert "pool work admits only its 3 reserved slots" in check.detail
    assert "every built-in build and test rule weighs 4" in check.detail
    fresh = template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 << 30})
    new = MachineConfig.model_validate(tomllib.loads(fresh))
    assert doctor.check_guard_weights(new, 16 << 30) is None


def test_doctor_warns_when_a_heavy_rule_counts_more_than_can_ever_start(tmp_path):
    text = template.sized("user", {"cpus_logical": 8, "memory_bytes": 16 << 30}).replace(
        'assumed_per_slot = "auto"', 'assumed_per_slot = "5g"'
    )
    config = MachineConfig.model_validate(tomllib.loads(text))
    check = doctor.check_guard_weights(config, 16 << 30)
    assert check is not None and "counts 15.0 GiB, more than the 12.0 GiB" in check.detail


def test_an_unedited_0_2_1_config_is_known_as_well(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(
        template.render_user_0_2(
            name="box", slots=6, cpus=6, memory="11g", about=ABOUT, release="0.2.1"
        )
    )
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and plan.pristine
