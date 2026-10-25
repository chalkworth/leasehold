"""`leasehold doctor`'s warnings: a negative `oom_score_adj`, and a memory cap that is set but
cannot hold; and a `memory_max` that is no cap, which is a config error."""

from __future__ import annotations

import pydantic
import pytest
from support import machine_config

from leasehold import doctor, host
from leasehold.host.posix import Posix


def test_a_negative_oom_score_adj_is_warned_about(tmp_path):
    (tmp_path / "self").mkdir()
    score = tmp_path / "self" / "oom_score_adj"
    roots = doctor.Roots(proc=tmp_path)
    score.write_text("-1000\n")
    found = doctor.check_oom_score(roots)
    assert found is not None and found.level == "warn" and "reset to 0" in found.detail
    score.write_text("0\n")
    assert doctor.check_oom_score(roots) is None


EXACT = host.Support("exact", via="MemoryMax")
MACOS = host.Support("none", "macOS has no per-command memory limit without root")
#: Commands get a scope here.
SCOPES = host.Support("exact", via="systemd")
#: case: ([commands], [contain] mode, how a cap works here, the check's level, in its detail).
CAP_CASES = {
    "none set": ({}, "auto", EXACT, "ok", "none is set"),
    "set, and it holds": ({"memory_max": "50%"}, "auto", EXACT, "ok", "exact"),
    "set, with no scopes": ({"memory_max": "8g"}, "off", EXACT, "warn", "no scope"),
    "a rule's, on macOS": (
        {"rule": [{"programs": ["pytest"], "weight": 1, "memory_max": "8g"}]},
        "auto", MACOS, "warn", "macOS has no per-command memory limit",
    ),
}  # fmt: skip


@pytest.mark.parametrize(
    ("commands", "mode", "cap", "level", "says"), CAP_CASES.values(), ids=CAP_CASES.keys()
)
def test_a_memory_cap_that_cannot_hold_is_warned_about(tmp_path, commands, mode, cap, level, says):
    config = machine_config(tmp_path, commands=commands, contain={"mode": mode})
    found = doctor.check_memory_cap(config, {}, start=SCOPES, cap=cap)
    assert (found.name, found.level) == ("memory_max", level)
    assert says in found.detail


def test_doctor_says_how_each_operation_works_on_this_host():
    # The fallback host, as on a system leasehold has no port for: it signals process groups
    # and does nothing else.
    found = doctor.check_host(Posix().capabilities())
    assert found.level == "ok"
    said = dict(part.rsplit(" ", 1) for part in found.detail.split(": ", 1)[1].split(", "))
    assert list(said) == list(host.OPERATIONS)
    assert {op for op, how in said.items() if how != "none"} == {"pause", "resume", "kill"}


@pytest.mark.parametrize("cap", ["0g", "150%", "lots"])
def test_a_memory_max_that_is_no_cap_is_a_config_error(tmp_path, cap):
    rule = {"programs": ["x"], "weight": 1, "memory_max": cap}
    for commands in ({"memory_max": cap}, {"rule": [rule]}):
        with pytest.raises(pydantic.ValidationError, match="give a size such as"):
            machine_config(tmp_path, commands=commands)
