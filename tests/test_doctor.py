"""`leasehold doctor`'s warning about a negative `oom_score_adj`."""

from __future__ import annotations

import pytest

from leasehold import doctor


@pytest.mark.parametrize(
    ("value", "level"), [("-1000\n", "warn"), ("-1\n", "warn"), ("0\n", None), ("200\n", None)]
)
def test_a_negative_oom_score_adj_is_warned_about(tmp_path, value, level):
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "oom_score_adj").write_text(value)
    found = doctor.check_oom_score(doctor.Roots(proc=tmp_path))
    assert (found.level if found else None) == level
    if found:
        assert "reset to 0" in found.detail


def test_no_oom_score_adj_file_is_no_warning(tmp_path):
    assert doctor.check_oom_score(doctor.Roots(proc=tmp_path)) is None
