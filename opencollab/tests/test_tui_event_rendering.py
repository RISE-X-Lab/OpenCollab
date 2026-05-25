"""Characterization tests for TUI event rendering.

Drives ``adapters.tui.renderer.TUI.event_handler`` with synthetic session-runtime and
scheduler-orchestration events and snapshots the resulting display-state
mutations (``_active_tools``, ``_status_lines``, ``_step``). Locks the
rendered behavior so the Step12 dispatch split between SessionRuntimeEvent
and SchedulerEvent keeps the user-visible output byte-equivalent.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.text import Text

from opencollab.adapters.tui import TUI
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent


def _make_tui() -> TUI:
    return TUI()


def _status_plains(tui: TUI) -> list[str]:
    return [line.plain for line in tui._status_lines]


def _assert_visible_text_has_non_white_style(text: Text) -> None:
    console = Console(color_system="truecolor")
    for offset, char in enumerate(text.plain):
        if char.isspace():
            continue
        style = text.get_style_at_offset(console, offset)
        assert style.color is not None
        assert style.color.name != "white"


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
            {"tool": "bash", "args": {"command": "ls -la"}, "aid": -1},
        )
    )
    assert any("bash" in k for k in tui._active_tools)

    tui.event_handler(
        SessionRuntimeEvent("tool_end", {"tool": "bash", "latency": 0.42, "aid": -1}),
    )
    assert not any("bash" in k for k in tui._active_tools)


def test_tool_start_for_spawn_agent_promotes_role_from_args():
    """The session emits tool_start when the LLM calls SpawnAgentTool;
    role gets lifted from the tool args even though it's not at top level."""
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "spawn_agent", "args": {"role": "analyst", "task": "study"}, "aid": -1},
        )
    )

    assert any("spawn_agent" in k for k in tui._active_tools)
    statuses = _status_plains(tui)
    assert any("spawned analyst" in s for s in statuses)


def test_step_start_updates_step_counter_and_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("step_start", {"step": 4, "aid": -1}))
    assert tui._step == 4
    statuses = _status_plains(tui)
    assert any("thinking..." in s and "step 4" in s for s in statuses)


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
        SessionRuntimeEvent("loop_detected", {"tool": "bash", "count": 5, "aid": -1}),
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

    tui.event_handler(SessionRuntimeEvent("error", {"reason": "boom", "aid": -1}))
    statuses = _status_plains(tui)
    assert any("Error: boom" in s for s in statuses)


# ---------------------------------------------------------------------------
# Scheduler orchestration events
# ---------------------------------------------------------------------------


def test_agent_spawned_emits_status_with_role():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SchedulerEvent(
            "agent_spawned",
            {"aid": 1, "parent_aid": 0, "role": "coder", "task": "implement X"},
        )
    )

    assert any("spawn" in k for k in tui._active_tools)
    statuses = _status_plains(tui)
    assert any(
        "coder" in s and "spawned" in s for s in statuses
    )


def test_agent_completed_clears_active_tool_and_emits_finished_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SchedulerEvent(
            "agent_spawned",
            {"aid": 1, "parent_aid": 0, "role": "coder", "task": "implement X"},
        )
    )
    tui.event_handler(
        SchedulerEvent(
            "agent_completed",
            {"aid": 1, "parent_aid": 0, "role": "coder", "latency": 1.5},
        )
    )

    assert not any("spawn" in k for k in tui._active_tools)
    statuses = _status_plains(tui)
    assert any("coder" in s and "completed" in s for s in statuses)


def test_review_started_tracks_review_loop_without_teammate_status():
    """Review loop is a scheduler-orchestration boundary; it should not produce
    a 'Teammate started' line — that fires only on the inner spawns."""
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SchedulerEvent(
            "review_started",
            {"tool": "review_loop", "iteration": 1, "max": 3},
        )
    )

    assert "review_loop" in tui._active_tools
    statuses = _status_plains(tui)
    assert not any("Teammate" in s for s in statuses)
    assert not any("spawned" in s for s in statuses)


def test_review_completed_clears_review_loop_from_active_tools():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SchedulerEvent("review_started", {"iteration": 1, "max": 3}),
    )
    tui.event_handler(
        SchedulerEvent("review_completed", {"iteration": 1, "verdict": "PASS"}),
    )

    assert "review_loop" not in tui._active_tools


def test_scheduler_events_do_not_use_legacy_tool_start_dispatch():
    """Sanity: a SchedulerEvent must not be routed through _handle_session_event.

    A SessionRuntimeEvent('tool_start', {tool: 'spawn', role: 'x'}) used to
    drive the Teammate-started status line. After refactoring that synthetic event
    no longer fires; instead, SchedulerEvent('agent_spawned') does. Driving the
    TUI with the legacy shape must therefore *not* produce the Teammate line
    via the scheduler-event handler — only via the session handler (which has had
    that branch removed)."""
    tui = _make_tui()
    tui._live_paused = True

    # Legacy shape, kept here intentionally to assert that the team-shaped
    # branches were removed from the session-runtime handler.
    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "spawn", "role": "coder", "task": "implement X", "aid": -1},
        )
    )

    # Active-tools is still tracked (it's a generic tool), but no "Teammate"
    # status line is added by the session-runtime handler anymore.
    statuses = _status_plains(tui)
    assert not any("Teammate coder started" in s for s in statuses)


# ---------------------------------------------------------------------------
# Team roster + inter-agent messaging events
# ---------------------------------------------------------------------------


def test_roster_tracks_spawn_and_completion_state():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    assert tui._roster[1] == {"role": "coder", "state": "running"}

    tui.event_handler(SchedulerEvent("agent_completed", {"aid": 1, "role": "coder", "latency": 1.0}))
    assert tui._roster[1]["state"] == "idle"


def test_agent_resumed_marks_roster_running_again():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "manager"}))
    tui.event_handler(SchedulerEvent("agent_completed", {"aid": 1, "role": "manager", "latency": 1.0}))
    assert tui._roster[1]["state"] == "idle"

    # The manager suspended on its own delegated work and was re-activated.
    tui.event_handler(SchedulerEvent("agent_resumed", {"aid": 1, "role": "manager"}))
    assert tui._roster[1]["state"] == "running"


def test_team_panel_renders_when_roster_present():
    tui = _make_tui()
    tui._live_paused = True
    assert tui._build_team_panel() is None

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 2, "parent_aid": 0, "role": "reviewer"}))
    panel = tui._build_team_panel()
    assert panel is not None
    assert "A2" in panel.plain and "reviewer" in panel.plain


def test_message_events_append_activity_lines():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SchedulerEvent("agent_message_sent", {"from_aid": 0, "to_aid": 2}))
    tui.event_handler(SchedulerEvent("agent_message_delivered", {"to_aid": 2, "result_len": 10}))

    timeline = "\n".join(
        block.plain for block in tui._timeline_blocks if hasattr(block, "plain")
    )
    assert "A0 → A2 message" in timeline
    assert "A2 replied" in timeline


def test_status_lines_use_explicit_non_white_styles():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("step_start", {"step": 4, "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("budget_warning", {}))
    tui.event_handler(SessionRuntimeEvent("error", {"reason": "boom", "aid": 0}))

    for status in tui._status_lines:
        _assert_visible_text_has_non_white_style(status)


def test_status_chrome_renderables_avoid_default_text_color():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "bash", "args": {"command": "pytest -q"}, "aid": 0},
        )
    )
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 2, "parent_aid": 0, "role": "reviewer"}))

    for block in tui._timeline_blocks:
        if isinstance(block, Text):
            _assert_visible_text_has_non_white_style(block)

    display = tui._build_display()
    for renderable in display.renderables:
        if isinstance(renderable, Text):
            _assert_visible_text_has_non_white_style(renderable)


def test_live_display_tails_when_content_exceeds_terminal_height():
    console = Console(file=StringIO(), width=40, height=4)
    tui = TUI(console)
    tui._live_paused = True
    for index in range(8):
        tui._status_lines.append(Text(f"status {index}", style="cyan"))

    display = tui._build_live_display()
    lines = console.render_lines(display, console.options, pad=False)
    plain = "\n".join("".join(segment.text for segment in line) for line in lines)

    assert len(lines) <= console.height
    assert "status 7" in plain
    assert "status 0" not in plain


def test_reset_clears_roster():
    tui = _make_tui()
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    tui.reset()
    assert tui._roster == {}


# ---------------------------------------------------------------------------
# Per-agent message filtering
# ---------------------------------------------------------------------------


def test_filter_off_renders_every_agent_stream():
    tui = TUI(filter_messages=False)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead ", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    assert tui._current_text == "lead child"


def test_filter_on_shows_only_selected_agent_defaulting_to_lead():
    tui = TUI(filter_messages=True)
    assert tui.selected_aid == 0
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead ", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    assert tui._current_text == "lead "


def test_select_agent_switches_the_visible_stream():
    tui = TUI(filter_messages=True)
    tui.select_agent(1)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead ", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    assert tui._current_text == "child"


def test_filter_suppresses_other_agents_tool_activity():
    tui = TUI(filter_messages=True)
    tui.event_handler(
        SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "ls"}, "aid": 1})
    )
    assert tui._active_tools == {}


def test_set_filter_toggles_at_runtime():
    tui = TUI(filter_messages=False)
    tui.set_filter(True)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    assert tui._current_text == ""


def test_filter_keeps_scheduler_roster_visible():
    tui = TUI(filter_messages=True)
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    assert 1 in tui._roster
