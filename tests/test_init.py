"""What `init` writes for a user's own daemon, and what `setup --upgrade-config` makes of a
config 0.2 wrote: the heavy rules' weight each one gives, and the two-at-once weights offered
as comments."""

from __future__ import annotations

import json

import pytest
from support import cli

from leasehold import machine, template, upgrade


@pytest.mark.parametrize(
    ("cpus", "offered"),
    [
        (4, None),  # 3 slots: one at a time, at full width
        (8, 3),  # 6 slots: one at a time; two at once is a commented opt-in (memtight bench)
        (12, None),  # 10 slots: weight 4 already runs two at once
    ],
)
def test_a_new_config_keeps_the_built_in_heavy_weight(tmp_path, cpus, offered):
    path = tmp_path / "machine.toml"
    text = template.sized("user", {"cpus_logical": cpus, "memory_bytes": 16 * 2**30})
    path.write_text(text)
    assert machine.load(path).commands.heavy_weight == 4
    assert ("# heavy_weight = " in text) == (offered is not None)
    if offered is not None:
        path.write_text(text.replace(f"# heavy_weight = {offered}", f"heavy_weight = {offered}"))
        assert machine.load(path).commands.heavy_weight == offered
    doctor = cli(tmp_path / "none.sock", "--config", str(path), "doctor", "--json")
    checks = json.loads(doctor.stdout)
    warned = [c for c in checks if c["name"] == "two_at_once"]
    assert [c["level"] for c in warned] == (["warn"] if offered is not None else [])


def test_an_upgraded_0_2_config_keeps_the_built_in_weights(tmp_path):
    path = tmp_path / "machine.toml"
    path.write_text(
        template.render_user_0_2(name="box", slots=6, cpus=6, memory="12g", about="A box.")
    )
    planned = upgrade.plan(path, machine.load(path), memory_bytes=16 * 2**30)
    assert planned.pristine and "# heavy_weight = 3" in planned.text
    (tmp_path / "new.toml").write_text(planned.text)
    assert machine.load(tmp_path / "new.toml").commands.heavy_weight == 4
