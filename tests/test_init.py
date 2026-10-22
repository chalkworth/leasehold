"""What `init` writes for a user's own daemon, and what `setup --upgrade-config` makes of a
config 0.2 wrote: the heavy rules' weight each one gives."""

from __future__ import annotations

import pytest

from leasehold import machine, template, upgrade


@pytest.mark.parametrize(
    ("cpus", "heavy"),
    [
        (4, 4),  # 3 slots: one at a time, at full width
        (8, 3),  # 6 slots: two at once (the defaults-h1 bench gate)
        (12, 4),  # 10 slots: weight 4 already runs two at once
    ],
)
def test_a_new_config_runs_two_heavy_commands_at_once(tmp_path, cpus, heavy):
    path = tmp_path / "machine.toml"
    path.write_text(template.sized("user", {"cpus_logical": cpus, "memory_bytes": 16 * 2**30}))
    assert machine.load(path).commands.heavy_weight == heavy


def test_an_upgraded_0_2_config_keeps_the_built_in_weights(tmp_path):
    path = tmp_path / "machine.toml"
    path.write_text(
        template.render_user_0_2(name="box", slots=6, cpus=6, memory="12g", about="A box.")
    )
    planned = upgrade.plan(path, machine.load(path), memory_bytes=16 * 2**30)
    assert planned.pristine and "# heavy_weight = 3" in planned.text
    (tmp_path / "new.toml").write_text(planned.text)
    assert machine.load(tmp_path / "new.toml").commands.heavy_weight == 4
