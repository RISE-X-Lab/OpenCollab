from __future__ import annotations

from opencollab.application.session_run import _WIND_DOWN_NUDGE


def test_wind_down_nudge_announces_submit_only_toolset():
    text = _WIND_DOWN_NUDGE.lower()

    assert "only submit_findings is available" in text
    assert "do not call any other tool" in text
    assert "removed" in text
    assert "grep" in text and "file_read" in text
