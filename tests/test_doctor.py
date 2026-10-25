"""`leasehold doctor`'s warning about a negative `oom_score_adj`."""

from __future__ import annotations

from leasehold import doctor


def test_a_negative_oom_score_adj_is_warned_about(tmp_path):
    (tmp_path / "self").mkdir()
    score = tmp_path / "self" / "oom_score_adj"
    roots = doctor.Roots(proc=tmp_path)
    score.write_text("-1000\n")
    found = doctor.check_oom_score(roots)
    assert found is not None and found.level == "warn" and "reset to 0" in found.detail
    score.write_text("0\n")
    assert doctor.check_oom_score(roots) is None
