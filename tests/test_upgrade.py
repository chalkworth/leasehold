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

import pytest
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
    assert upgrade.outdated(config) == ["no [memory] table: memory is not counted"]
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
    assert (new.commands.heavy_weight, new.commands.light_weight) == (4, 2)
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
    assert new.commands.heavy_weight == 4
    assert "settle_s = 45  # mine" in path.read_text()


def test_a_commands_table_written_inline_gains_the_memory_table_too(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(
        'mode = "user"\ncommands = { wait_s = 10 }\n[ceiling]\nslots = 6\n'
        '[pools.work]\nreserved = 3\nmax = 6\n[kinds]\nwork = "work"\n[lend]\npools = []\n'
    )
    done = run("--config", str(path), "setup", "--upgrade-config")
    assert done.returncode == 0, done.stdout + done.stderr
    new = machine.load(path)
    assert new.memory is not None and new.commands.wait_s == 10


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
    assert "lacks what today's config has: no [memory] table" in check.detail
    assert check.fix is not None and check.fix.startswith("optional: ")
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
    assert check is not None and "counts 20.0 GiB, more than the 12.0 GiB" in check.detail


def test_an_unedited_0_2_1_config_is_known_as_well(tmp_path):
    path = tmp_path / "m.toml"
    path.write_text(
        template.render_user_0_2(
            name="box", slots=6, cpus=6, memory="11g", about=ABOUT, release="0.2.1"
        )
    )
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and plan.pristine


def _user_config(path: Path, slots: int, extra: str = "") -> MachineConfig:
    path.write_text(
        f'mode = "user"\n[ceiling]\nslots = {slots}\n'
        f"[pools.work]\nreserved = {slots}\nmax = {slots}\n"
        '[kinds]\nwork = "work"\n[lend]\npools = []\n' + extra
    )
    return machine.load(path)


@pytest.mark.parametrize("slots", [3, 6, 8, 10, 14])
def test_weights_are_never_part_of_the_upgrade(tmp_path, slots):
    """The built-in weights stay as they are (4 and 2 unless set): with a [memory] table a
    config lacks nothing, whatever its slots; without one it gains only the table."""
    config = _user_config(tmp_path / "m.toml", slots, '[memory]\nbudget = "auto"\n')
    assert upgrade.outdated(config) == []
    bare = _user_config(tmp_path / "b.toml", slots)
    plan = upgrade.plan(tmp_path / "b.toml", bare)
    assert plan.changes == (
        'a [memory] table: budget = "auto", kept = "25%", assumed_per_slot "auto"',
    )
    upgrade.apply(plan)
    new = machine.load(tmp_path / "b.toml")
    assert new.memory is not None
    assert "heavy_weight" not in new.commands.model_fields_set
    assert (new.commands.heavy_weight, new.commands.light_weight) == (4, 2)


def test_a_config_that_names_no_mode_is_read_as_a_users_by_every_check(tmp_path):
    """One rule, the daemon's: no `mode` is a user's own config, for the upgrade and for
    doctor's hard_cpu warning alike; `mode = "system"` is neither."""
    body = (
        "[ceiling]\nslots = 6\n[pools.work]\nreserved = 6\nmax = 6\n"
        '[kinds]\nwork = "work"\n[lend]\npools = []\n'
    )
    path = tmp_path / "m.toml"
    path.write_text(body)
    config = machine.load(path)
    assert config.for_user
    assert upgrade.outdated(config) == ["no [memory] table: memory is not counted"]
    assert doctor.check_hard_cpu(config) is not None
    assert upgrade.plan(path, config) is not None
    system = MachineConfig.model_validate(tomllib.loads('mode = "system"\n' + body))
    assert upgrade.outdated(system) == [] and doctor.check_hard_cpu(system) is None


def test_doctor_warns_only_for_a_config_as_0_2_wrote_it(tmp_path):
    """A pristine 0.2 config has 0.2's defaults, not chosen ones: a warning. An edited one
    without [memory] was someone's choice: the same news, at ok, for information."""
    path = old_config(tmp_path / "m.toml")
    check = doctor.check_upgrade(str(path), {}, machine.load(path))
    assert check.level == "warn"
    path.write_text(path.read_text() + "# mine\n")
    check = doctor.check_upgrade(str(path), {}, machine.load(path))
    assert check.level == "ok" and "for information" in check.detail


def test_crlf_line_endings_are_kept_and_the_backup_is_byte_for_byte(tmp_path):
    path = old_config(tmp_path / "m.toml")
    original = (path.read_text() + "# mine\n").replace("\n", "\r\n").encode()
    path.write_bytes(original)
    plan = upgrade.plan(path, machine.load(path))
    assert plan is not None and plan.text is not None and not plan.pristine
    backup = upgrade.apply(plan)
    assert backup.read_bytes() == original
    new = path.read_bytes()
    assert b"\r\n# mine\r\n" in new and b"\r\n[memory]\r\n" in new
    assert new.count(b"\n") == new.count(b"\r\n")  # no bare LF added
    assert machine.load(path).memory is not None


@pytest.mark.parametrize(
    ("memory_bytes", "extra", "said"),
    [
        (
            64 << 30,
            '\n[[standing]]\nname = "db"\npool = "background"\nweight = 1\nmemory = "48g"\n',
            "all held by [[standing]] memory",
        ),
        (
            16 << 30,
            '\n[[standing]]\nname = "db"\npool = "background"\nweight = 1\nmemory = "7g"\n'
            '\n[[commands.rule]]\nprograms = ["bigbuild"]\nweight = 2\nmemory = "6g"\n',
            "commands.rule[0] (bigbuild) would never be granted",
        ),
        (
            16 << 30,
            '\n[[standing]]\nname = "db"\npool = "background"\nweight = 1\nmemory = "5g"\n',
            "the built-in build and test rules (weight 4) are cut to it",
        ),
    ],
)
def test_no_edit_that_would_stop_work_starting_on_this_machine(tmp_path, memory_bytes, extra, said):
    """The would-be budget is worked out for this machine: an upgrade that would leave nothing
    to start, a rule that could never be served, or the built-in heavy rules cut, is refused
    with what to change, and the file is left as it is."""
    path = old_config(tmp_path / "m.toml")
    path.write_text(path.read_text().replace('memory = "11g"', 'memory = "40g"') + extra)
    before = path.read_bytes()
    plan = upgrade.plan(path, machine.load(path), memory_bytes)
    assert plan is not None and plan.text is None
    assert said in plan.manual and "Change that first" in plan.manual
    with pytest.raises(ValueError, match="cannot be upgraded automatically"):
        upgrade.apply(plan)
    assert path.read_bytes() == before


def test_a_symbolic_link_is_not_written_through(tmp_path):
    real = old_config(tmp_path / "real.toml")
    link = tmp_path / "m.toml"
    link.symlink_to(real)
    before = real.read_text()
    plan = upgrade.plan(link, machine.load(link))
    assert plan.text is None and "is a symbolic link" in plan.manual
    done = run("--config", str(link), "setup", "--upgrade-config")
    assert done.returncode == 1 and "symbolic link" in done.stdout
    assert link.is_symlink() and real.read_text() == before
    assert not list(tmp_path.glob("*.bak*"))


def test_a_hard_linked_config_is_not_parted_from_its_other_name(tmp_path):
    path = old_config(tmp_path / "m.toml")
    (tmp_path / "other.toml").hardlink_to(path)
    plan = upgrade.plan(path, machine.load(path))
    assert plan.text is None and "2 hard links" in plan.manual


def test_a_link_where_the_old_temporary_file_went_is_left_alone(tmp_path):
    path = old_config(tmp_path / "m.toml")
    victim = tmp_path / "victim"
    victim.write_text("keep me\n")
    (tmp_path / ".m.toml.upgrading").symlink_to(victim)
    upgrade.apply(upgrade.plan(path, machine.load(path)))
    assert victim.read_text() == "keep me\n"
    assert machine.load(path).memory is not None
    assert {p.name for p in tmp_path.iterdir()} == {
        "m.toml", "m.toml.0.2.bak", "victim", ".m.toml.upgrading"
    }  # fmt: skip


def test_a_change_made_between_plan_and_apply_is_not_written_over(tmp_path):
    path = old_config(tmp_path / "m.toml")
    plan = upgrade.plan(path, machine.load(path))
    path.write_text(path.read_text() + "# mine\n")
    with pytest.raises(ValueError, match="changed while it was being upgraded"):
        upgrade.apply(plan)
    assert path.read_text().endswith("# mine\n")


def test_a_change_made_while_the_new_file_is_written_is_not_written_over(tmp_path, monkeypatch):
    """The file is checked again just before the new one takes its place."""
    path = old_config(tmp_path / "m.toml")
    plan = upgrade.plan(path, machine.load(path))
    real_fsync = upgrade.os.fsync
    calls = []

    def fsync(fd):
        real_fsync(fd)
        calls.append(fd)
        if len(calls) == 2:  # the new file is written: someone edits the config now
            path.write_text(plan.old + "# mine\n")

    monkeypatch.setattr(upgrade.os, "fsync", fsync)
    with pytest.raises(ValueError, match="changed while it was being upgraded"):
        upgrade.apply(plan)
    assert path.read_text() == plan.old + "# mine\n"
    assert not list(tmp_path.glob("*.upgrading"))


def test_a_rule_declaring_more_memory_than_the_new_budget_stops_the_automatic_edit(tmp_path):
    path = old_config(tmp_path / "m.toml")
    path.write_text(
        path.read_text()
        + '\n[[commands.rule]]\nprograms = ["bigbuild"]\nweight = 2\nmemory = "14g"\n'
    )
    plan = upgrade.plan(path, machine.load(path), 16 << 30)
    assert plan is not None and plan.text is None
    assert "commands.rule[0] (bigbuild) declares 14g" in plan.manual
    assert "more than [ceiling] memory (11g)" in plan.manual and "Change that first" in plan.manual
