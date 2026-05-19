"""Characterization tests for TUI event rendering.

Drives ``cli.tui.TUI.event_handler`` with synthetic session-runtime and
team-orchestration events and snapshots the resulting display-state
mutations (``_active_tools``, ``_status_lines``, ``_step``). Locks the
rendered behavior so the Step12 dispatch split between SessionRuntimeEvent
and TeamEvent keeps the user-visible output byte-equivalent.
"""

from __future__ import annotations

from opencollab.domain.events import SessionRuntimeEvent, TeamEvent
from opencollab.cli.tui import TUI


def _make_tui() -> TUI:
    return TUI()


def _status_plains(tui: TUI) -> list[str]:
    return [line.plain for line in tui._status_lines]


# ---------------------------------------------------------------------------
# Session runtime events
# ---------------------------------------------------------------------------


def test_text_delta_appends_to_current_text():
    tui = _make_tui()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "hello "}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "world"}))

    assert tui._current_text == "hello world"
    assert tui._active_tools == {}


def test_tool_start_and_tool_end_for_generic_tool_tracks_active_tools():
    tui = _make_tui()
    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "bash", "args": {"command": "ls -la"}},
        )
    )
    assert "bash" in tui._active_tools

    tui.event_handler(
        SessionRuntimeEvent("tool_end", {"tool": "bash", "latency": 0.42}),
    )
    assert "bash" not in tui._active_tools


def test_tool_start_for_delegate_task_promotes_role_from_args():
    """The session emits tool_start when the LLM calls DelegateTaskTool;
    role gets lifted from the tool args even though it's not at top level."""
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "delegate_task", "args": {"role": "analyst", "task": "study"}},
        )
    )

    assert "analyst:delegate_task" in tui._active_tools
    statuses = _status_plains(tui)
    assert any(s == "Lead delegated to analyst" for s in statuses)


def test_step_start_updates_step_counter_and_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("step_start", {"step": 4}))
    assert tui._step == 4
    statuses = _status_plains(tui)
    assert any("Lead thinking..." in s and "step 4" in s for s in statuses)


def test_compaction_event_emits_status_line():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("compaction", {}))
    statuses = _status_plains(tui)
    assert any(s == "Context compacted" for s in statuses)


def test_loop_detected_event_emits_warning_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SessionRuntimeEvent("loop_detected", {"tool": "bash", "count": 5}),
    )
    statuses = _status_plains(tui)
    assert any("Loop detected: bash called 5x" in s for s in statuses)


def test_budget_warning_emits_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("budget_warning", {}))
    statuses = _status_plains(tui)
    assert any("Token budget running low" in s for s in statuses)


def test_error_event_emits_status_with_reason():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("error", {"reason": "boom"}))
    statuses = _status_plains(tui)
    assert any("Error: boom" in s for s in statuses)


# ---------------------------------------------------------------------------
# Team orchestration events
# ---------------------------------------------------------------------------


def test_delegation_started_emits_teammate_status_with_task_suffix():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        TeamEvent(
            "delegation_started",
            {"tool": "delegate", "role": "coder", "task": "implement X"},
        )
    )

    # Active-tools key uses the role:delegate composite, matching old behavior.
    assert "coder:delegate" in tui._active_tools
    statuses = _status_plains(tui)
    assert any(
        s.startswith("Teammate coder started") and "implement X" in s for s in statuses
    )


def test_delegation_completed_clears_active_tool_and_emits_finished_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        TeamEvent(
            "delegation_started",
            {"tool": "delegate", "role": "coder", "task": "implement X"},
        )
    )
    tui.event_handler(
        TeamEvent(
            "delegation_completed",
            {"tool": "delegate", "role": "coder", "latency": 1.5},
        )
    )

    assert "coder:delegate" not in tui._active_tools
    statuses = _status_plains(tui)
    assert any(s.startswith("Teammate coder finished") for s in statuses)


def test_review_started_tracks_review_loop_without_teammate_status():
    """Review loop is a team-orchestration boundary; it should not produce
    a 'Teammate started' line — that fires only on the inner delegations."""
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        TeamEvent(
            "review_started",
            {"tool": "review_loop", "iteration": 1, "max": 3},
        )
    )

    assert "review_loop" in tui._active_tools
    statuses = _status_plains(tui)
    assert not any("Teammate" in s for s in statuses)
    assert not any("Lead delegated to" in s for s in statuses)


def test_review_completed_clears_review_loop_from_active_tools():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        TeamEvent("review_started", {"iteration": 1, "max": 3}),
    )
    tui.event_handler(
        TeamEvent("review_completed", {"iteration": 1, "verdict": "PASS"}),
    )

    assert "review_loop" not in tui._active_tools


def test_team_events_do_not_use_legacy_tool_start_dispatch():
    """Sanity: a TeamEvent must not be routed through _handle_session_event.

    A SessionRuntimeEvent('tool_start', {tool: 'delegate', role: 'x'}) used to
    drive the Teammate-started status line. After Step12 that synthetic event
    no longer fires; instead, TeamEvent('delegation_started') does. Driving the
    TUI with the legacy shape must therefore *not* produce the Teammate line
    via the team-event handler — only via the session handler (which has had
    that branch removed)."""
    tui = _make_tui()
    tui._live_paused = True

    # Legacy shape, kept here intentionally to assert that the team-shaped
    # branches were removed from the session-runtime handler.
    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "delegate", "role": "coder", "task": "implement X"},
        )
    )

    # Active-tools is still tracked (it's a generic tool), but no "Teammate"
    # status line is added by the session-runtime handler anymore.
    statuses = _status_plains(tui)
    assert not any(s.startswith("Teammate coder started") for s in statuses)
