"""What `init` writes for a user's own daemon, and what `setup --upgrade-config` makes of a
config 0.2 wrote: the heavy rules' weight each one gives."""

from __future__ import annotations

import tomllib

import pytest

from bench import conditions
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
    for kept in (None, 1):
        for weight in (None, (4, 4), (2, 4)):
            text = conditions.config(
                path.read_text(),
                conditions.Condition("lh-enforced", kept=kept, weight=weight),
                cpus=cpus,
                ledger=str(tmp_path),
            )
            raw = tomllib.loads(text)["commands"]
            assert raw.get("heavy_weight") == (4 if weight == (4, 4) else None)
            assert "light_weight" not in raw
            path.with_name("bench.toml").write_text(text)
            commands = machine.load(path.with_name("bench.toml")).commands
            assert commands.heavy_weight == 4
            assert commands.light_weight == 2


def test_an_upgraded_0_2_config_keeps_the_built_in_weights(tmp_path):
    path = tmp_path / "machine.toml"
    path.write_text(
        template.render_user_0_2(name="box", slots=6, cpus=6, memory="12g", about="A box.")
    )
    planned = upgrade.plan(path, machine.load(path), memory_bytes=16 * 2**30)
    assert planned.pristine and "# heavy_weight = 3" in planned.text
    assert "leaves the two-at-once weights commented" in planned.changes[0]
    (tmp_path / "new.toml").write_text(planned.text)
    assert machine.load(tmp_path / "new.toml").commands.heavy_weight == 4

    wide = tmp_path / "wide.toml"
    wide.write_text(
        template.render_user_0_2(name="box", slots=8, cpus=8, memory="16g", about="A box.")
    )
    planned = upgrade.plan(wide, machine.load(wide), memory_bytes=16 * 2**30)
    assert planned.pristine and "commented" not in planned.changes[0]
