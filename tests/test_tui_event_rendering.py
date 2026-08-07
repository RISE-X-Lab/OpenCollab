"""Characterization tests for TUI event rendering.

Drives ``adapters.tui.renderer.TUI.event_handler`` with synthetic session-runtime
and scheduler-orchestration events. The tests lock semantic render state and
per-agent routing without golden-mastering a whole terminal frame.
"""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console
from rich.text import Text

from opencollab.adapters.tui import TUI
from opencollab.adapters.tui import renderer as renderer_mod
from opencollab.adapters.tui import renderer_events as renderer_events_mod
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent


def _make_tui() -> TUI:
    return TUI()


def _status_plains(tui: TUI, aid: int | None = None) -> list[str]:
    state = tui._selected_state if aid is None else tui._state_for(aid)
    return [line.plain for line in state.status_lines]




def test_one_shot_welcome_describes_a_running_issue_without_an_input_prompt():
    output = StringIO()
    tui = TUI(Console(file=output, width=120, color_system=None))

    tui.print_welcome(interactive=False)

    rendered = output.getvalue()
    assert "Running issue" in rendered
    assert "Type a message" not in rendered


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


def test_same_named_tool_calls_track_by_tool_call_id():
    tui = _make_tui()
    start = {"tool": "grep", "args": {"pattern": "x"}, "aid": 3}

    tui.event_handler(
        SessionRuntimeEvent("tool_start", {**start, "tool_call_id": "call-a"})
    )
    tui.event_handler(
        SessionRuntimeEvent("tool_start", {**start, "tool_call_id": "call-b"})
    )
    state = tui._state_for(3)
    assert len(state.active_tools) == 2

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_end",
            {"tool": "grep", "latency": 0.1, "aid": 3, "tool_call_id": "call-a"},
        )
    )
    assert len(state.active_tools) == 1
    assert next(iter(state.active_tools.values()))["tool_call_id"] == "call-b"

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_end",
            {"tool": "grep", "latency": 0.2, "aid": 3, "tool_call_id": "call-b"},
        )
    )
    assert state.active_tools == {}


def test_legacy_same_named_tool_events_receive_local_sequence_keys():
    tui = _make_tui()
    start = {"tool": "grep", "args": {"pattern": "x"}, "aid": 3}
    end = {"tool": "grep", "latency": 0.1, "aid": 3}

    tui.event_handler(SessionRuntimeEvent("tool_start", start))
    tui.event_handler(SessionRuntimeEvent("tool_start", start))
    state = tui._state_for(3)
    assert len(state.active_tools) == 2

    tui.event_handler(SessionRuntimeEvent("tool_end", end))
    assert len(state.active_tools) == 1
    tui.event_handler(SessionRuntimeEvent("tool_end", end))
    assert state.active_tools == {}


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


def test_args_preview_reads_nested_session_args_and_top_level_task():
    tui = _make_tui()
    # Session tool: args nested under "args".
    assert tui._args_preview({"args": {"command": "ls -la"}}) == " ls -la"
    assert tui._args_preview({"args": {"file_path": "src/x.py"}}) == " src/x.py"
    # Scheduler event (e.g. agent_spawned): task carried at the top level.
    assert tui._args_preview({"role": "coder", "task": "implement X"}) == " implement X"
    # Code formatting + length cap for the spinner.
    assert tui._args_preview({"args": {"command": "echo hi"}}, code=True) == " `echo hi`"


def test_spawn_spinner_preview_uses_scheduler_task_payload():
    # The Running block renders each active tool as an animated Spinner inside a
    # Table.grid, so the preview text is nested below the top-level renderables.
    # Render the display to text and assert the preview is visible to the user.
    console = Console(file=StringIO(), width=80, color_system="truecolor")
    tui = TUI(console)
    tui._live_paused = True
    tui.event_handler(
        SchedulerEvent(
            "agent_spawned",
            {"aid": 1, "parent_aid": 0, "role": "coder", "task": "implement X"},
        )
    )
    tui.select_agent(1)
    with console.capture() as capture:
        console.print(tui._build_display())
    assert "implement X" in capture.get()


def test_step_start_updates_step_counter_and_status():
    tui = _make_tui()
    tui._live_paused = True

    tui.event_handler(SessionRuntimeEvent("step_start", {"step": 4, "aid": -1}))
    assert tui._step == 4
    # The waiting indicator is now the animated pulsing dot; its label still
    # carries the agent + step counter (rendered inline, not as a status line).
    assert tui._thinking is not None
    line = tui._thinking.render().plain
    assert "thinking" in line and "step 4" in line


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

    tui.select_agent(1)
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

    tui.select_agent(1)
    assert not any("spawn" in k for k in tui._active_tools)
    statuses = _status_plains(tui)
    assert any("coder" in s and "completed" in s for s in statuses)


def test_follow_up_completion_is_not_labeled_as_another_spawn():
    tui = _make_tui()
    tui._live_paused = True
    tui.event_handler(
        SchedulerEvent(
            "agent_completed",
            {"aid": 1, "parent_aid": 0, "role": "coder", "latency": 0.5},
        )
    )

    lines = [
        block.plain
        for block in tui._state_for(1).timeline_blocks
        if isinstance(block, Text)
    ]
    assert any("A1 completed" in line for line in lines)
    assert all("A1:spawn" not in line for line in lines)


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
# Per-agent stream retention and selection
# ---------------------------------------------------------------------------


def test_filter_off_keeps_agent_streams_separate_and_switchable():
    tui = TUI(filter_messages=False)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead ", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))

    assert tui.selected_aid == 0
    assert tui._current_text == "lead "
    assert tui._state_for(1).current_text == "child"
    assert tui.select_next_agent() == 1
    assert tui._current_text == "child"


def test_compat_filter_setting_starts_on_lead_without_dropping_child():
    tui = TUI(filter_messages=True)
    assert tui._selected_aid == 0
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead ", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    assert tui._current_text == "lead "


def test_other_agent_tool_activity_is_retained_until_selected():
    tui = TUI(filter_messages=True)
    tui.event_handler(
        SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "ls"}, "aid": 1})
    )
    assert tui._active_tools == {}
    assert "A1:bash" in tui._state_for(1).active_tools
    tui.select_agent(1)
    assert "A1:bash" in tui._active_tools


def test_compat_filter_setting_keeps_scheduler_roster_visible():
    tui = TUI(filter_messages=True)
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    assert 1 in tui._roster


def test_agent_selection_wraps_through_live_agents_only():
    tui = TUI()
    tui._state_for(2)
    tui._state_for(5)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "idle", "busy": False},
        {"aid": None, "role": "analyst", "phase": "available", "busy": False},
        {"aid": 2, "role": "coder", "phase": "done", "busy": False},
        {"aid": 5, "role": "reviewer", "phase": "done", "busy": False},
    ])

    assert tui.selected_aid == 0
    assert tui.select_previous_agent() == 5
    assert tui.select_next_agent() == 0
    assert tui.select_next_agent() == 2
    assert tui.select_next_agent() == 5
    assert tui.select_next_agent() == 0
    assert tui.selected_role is None
    assert [target.aid for target in tui._focus_targets()] == [0, 2, 5]


def test_available_role_is_visible_but_joins_focus_only_after_spawn():
    roster = [
        {"aid": 0, "role": "lead", "phase": "idle", "busy": False},
        {"aid": None, "role": "coder", "phase": "available", "busy": False},
    ]
    tui = TUI()
    tui.set_team_provider(lambda: roster)

    assert tui.select_next_agent() == 0
    assert tui.selected_aid == 0
    assert "◆ Lead idle  coder available" in tui._build_team_panel().plain

    roster[1] = {"aid": 3, "role": "coder", "phase": "executing_tools", "busy": True}
    tui.event_handler(
        SchedulerEvent("agent_spawned", {"aid": 3, "parent_aid": 0, "role": "coder"})
    )

    assert tui.selected_aid == 0
    assert tui.select_next_agent() == 3
    assert "◆ A3 coder running" in tui._build_team_panel().plain
    assert tui._build_team_panel().plain.index("Lead") < tui._build_team_panel().plain.index("◆ A3")


def test_failed_agent_remains_selectable_with_retained_output_and_error():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui._live_paused = True
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "idle", "busy": False},
        {"aid": 2, "role": "reviewer", "phase": "failed", "busy": False},
    ])
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "partial output", "aid": 2}))
    tui.event_handler(SessionRuntimeEvent("error", {"reason": "model failed", "aid": 2}))
    tui.event_handler(
        SchedulerEvent("agent_failed", {"aid": 2, "role": "reviewer", "error": "boom"})
    )

    assert tui.select_agent(2) == 2
    assert any("model failed" in status for status in _status_plains(tui, 2))
    with console.capture() as capture:
        console.print(tui._build_display())
    rendered = capture.get()
    assert "partial output" in rendered
    assert "boom" in rendered
    assert "◆ A2 reviewer failed" in tui._build_team_panel().plain


def test_live_timeline_cap_does_not_truncate_complete_agent_history():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    state = tui._state_for(1)

    for index in range(100):
        tui._append_activity(
            (f"activity {index}", tui._STYLE_MUTED),
            state=state,
        )

    assert len(state.timeline_blocks) == 80
    assert len(state.history_blocks) == 100
    tui.select_agent(1)
    history = tui.render_selected_history()
    assert "activity 0" in history
    assert "activity 99" in history


def test_all_timeline_append_paths_share_one_global_bound():
    tui = TUI(Console(file=StringIO(), width=100, color_system=None))
    state = tui._state_for(1)

    for index in range(renderer_events_mod.MAX_TIMELINE_BLOCKS * 3):
        tui.event_handler(
            SessionRuntimeEvent(
                "text_delta",
                {"content": f"assistant {index}", "aid": 1},
            )
        )
        tui.event_handler(
            SessionRuntimeEvent("error", {"reason": f"failure {index}", "aid": 1})
        )
        tui.record_user_message(1, f"user {index}")

    assert len(state.timeline_blocks) == renderer_events_mod.MAX_TIMELINE_BLOCKS


def test_agent_history_has_a_global_per_agent_bound():
    tui = TUI(Console(file=StringIO(), width=100, color_system=None))
    state = tui._state_for(1)

    for index in range(renderer_mod.MAX_HISTORY_BLOCKS_PER_AGENT + 20):
        tui._append_activity(
            (f"bounded activity {index}", tui._STYLE_MUTED),
            state=state,
        )

    assert len(state.history_blocks) == renderer_mod.MAX_HISTORY_BLOCKS_PER_AGENT
    tui.select_agent(1)
    history = tui.render_selected_history()
    assert "bounded activity 0" not in history
    assert "20 older history blocks omitted" in history
    assert f"bounded activity {renderer_mod.MAX_HISTORY_BLOCKS_PER_AGENT + 19}" in history


def test_completed_agent_render_states_have_a_global_bound():
    tui = TUI()

    for aid in range(1, renderer_mod.MAX_TERMINAL_AGENT_STATES + 10):
        tui.event_handler(
            SchedulerEvent(
                "agent_completed",
                {"aid": aid, "role": "worker", "latency": 0.1},
            )
        )

    retained_children = {aid for aid in tui._agent_states if aid != 0}
    assert len(retained_children) <= renderer_mod.MAX_TERMINAL_AGENT_STATES
    assert 1 not in retained_children
    assert renderer_mod.MAX_TERMINAL_AGENT_STATES + 9 in retained_children
    summaries = {summary["aid"]: summary for summary in tui.terminal_agent_summaries}
    assert summaries[1]["role"] == "worker"
    assert summaries[1]["state"] == "idle"
    assert summaries[1]["retained_history_blocks"] > 0


def test_terminal_agent_summaries_have_a_global_bound():
    tui = TUI(Console(file=StringIO(), width=100, color_system=None))
    tui._live_paused = True
    total = (
        renderer_mod.MAX_TERMINAL_AGENT_STATES
        + renderer_mod.MAX_TERMINAL_AGENT_SUMMARIES
        + 10
    )

    for aid in range(1, total + 1):
        tui.event_handler(
            SchedulerEvent(
                "agent_completed",
                {"aid": aid, "role": "worker", "latency": 0.1},
            )
        )

    assert (
        len(tui.terminal_agent_summaries)
        == renderer_mod.MAX_TERMINAL_AGENT_SUMMARIES
    )
    assert tui.terminal_agent_summaries_omitted == 10


@pytest.mark.parametrize(
    ("event_type", "provider_phase"),
    [
        ("agent_completed", "done"),
        ("agent_failed", "error"),
        ("agent_cancelled", "stopped"),
    ],
)
def test_terminal_provider_entries_cannot_recreate_evicted_render_states(
    event_type,
    provider_phase,
):
    tui = TUI(Console(file=StringIO(), width=100, color_system=None))
    tui._live_paused = True
    total = renderer_mod.MAX_TERMINAL_AGENT_STATES + 300
    roster = [
        {"aid": 0, "role": "lead", "phase": "idle", "busy": False},
        *[
            {
                "aid": aid,
                "role": "worker",
                "phase": provider_phase,
                "busy": False,
            }
            for aid in range(1, total + 1)
        ],
    ]
    tui.set_team_provider(lambda: roster)
    for aid in range(1, total + 1):
        tui.event_handler(
            SchedulerEvent(
                event_type,
                {
                    "aid": aid,
                    "role": "worker",
                    "latency": 0.1,
                    "error": "boom",
                },
            )
        )

    retained = len(tui._agent_states)
    for aid in range(1, total + 1):
        tui.select_agent(aid)
        tui.render_selected_history()

    assert retained <= renderer_mod.MAX_TERMINAL_AGENT_STATES + 1
    assert len(tui._agent_states) == retained


def test_user_message_is_recorded_only_in_target_history_and_revises_cache_key():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    before = tui._state_for(2).history_revision

    tui.record_user_message(2, "please inspect the renderer")

    assert tui._state_for(2).history_revision == before + 1
    assert tui._state_for(0).history_blocks == []
    tui.select_agent(2)
    assert tui.selected_history_cache_key == (2, before + 1, 100)
    assert "please inspect the renderer" in tui.render_selected_history()


def test_stop_live_preserves_child_final_text_and_error_for_prompt_view():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui._live_paused = True
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "final child text", "aid": 1}))
    tui.event_handler(SessionRuntimeEvent("error", {"reason": "child error", "aid": 1}))

    tui.stop_live()
    tui.select_agent(1)
    history = tui.render_selected_history()

    assert tui._state_for(1).current_text == ""
    assert "final child text" in history
    assert "Error: child error" in history


def test_agent_history_accumulates_across_turn_resets():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "turn one", "aid": 1}))
    tui.stop_live()

    tui.reset()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "turn two", "aid": 1}))
    tui.stop_live()
    tui.select_agent(1)

    history = tui.render_selected_history()
    assert "turn one" in history
    assert "turn two" in history


def test_lead_settled_display_contains_only_current_turn_after_reset():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "old answer", "aid": 0}))
    tui._build_settled_display(aid=0)

    tui.reset()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "new answer", "aid": 0}))
    settled = tui._build_settled_display(aid=0)
    with console.capture() as capture:
        console.print(settled)

    output = capture.get()
    assert "new answer" in output
    assert "old answer" not in output


def test_switching_does_not_lose_partial_text_or_timeline_order():
    tui = TUI()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead-a", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child-a", "aid": 1}))
    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "bash", "args": {"command": "pwd"}, "aid": 1},
        )
    )
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead-b", "aid": 0}))

    assert tui._state_for(0).current_text == "lead-alead-b"
    assert tui._state_for(1).current_text == ""
    assert len(tui._state_for(1).timeline_blocks) == 2
    tui.select_agent(1)
    assert "A1:bash" in tui._active_tools


def test_legacy_event_without_aid_routes_to_lead_not_current_focus():
    tui = TUI()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    tui.select_agent(1)

    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead"}))

    assert tui._state_for(0).current_text == "lead"
    assert tui._state_for(1).current_text == "child"


def test_settled_display_always_commits_lead_when_child_is_selected():
    tui = TUI()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead answer", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child notes", "aid": 1}))
    tui.select_agent(1)

    settled = tui._build_settled_display(aid=0)

    console = Console(file=StringIO(), width=80)
    with console.capture() as capture:
        console.print(settled)
    output = capture.get()
    assert "lead answer" in output
    assert "child notes" not in output


def test_live_suspend_and_resume_transfer_keyboard_ownership():
    class FakeKeyboard:
        def __init__(self) -> None:
            self.active = False
            self.starts = 0
            self.stops = 0

        def start(self) -> bool:
            self.starts += 1
            self.active = True
            return True

        def stop(self) -> bool:
            self.stops += 1
            was_active = self.active
            self.active = False
            return was_active

    keyboard = FakeKeyboard()
    tui = TUI(Console(file=StringIO(), width=80))
    tui.set_keyboard_controller(keyboard)

    tui.start_live()
    assert keyboard.active is True
    assert tui.suspend_live() is True
    assert keyboard.active is False
    tui.resume_live(True)
    assert keyboard.active is True
    tui.stop_live()

    assert keyboard.starts == 2
    assert keyboard.stops == 2


@pytest.mark.asyncio
async def test_hold_live_keeps_the_display_until_quit_and_marks_the_footer():
    class FakeKeyboard:
        active = True

        def __init__(self) -> None:
            self.quit_callback = None

        def set_quit_callback(self, callback) -> None:
            self.quit_callback = callback

    class FakeLive:
        def __init__(self) -> None:
            self.updates = 0

        def update(self, display) -> None:
            self.updates += 1

    keyboard = FakeKeyboard()
    live = FakeLive()
    tui = TUI(Console(file=StringIO(), width=120))
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "analyst", "phase": "done", "busy": False},
        {"aid": 1, "role": "coder", "phase": "done", "busy": False},
        {"aid": 2, "role": "tester", "phase": "done", "busy": False},
    ])
    tui.set_keyboard_controller(keyboard)
    tui._live = live

    holding = asyncio.create_task(tui.hold_live())
    await asyncio.sleep(0)

    assert holding.done() is False
    assert callable(keyboard.quit_callback)
    assert "q close" in tui._build_team_panel().plain
    assert live.updates == 1

    keyboard.quit_callback()
    assert await holding is True
    assert keyboard.quit_callback is None
    assert tui._holding_for_exit is False


@pytest.mark.asyncio
async def test_hold_live_declines_without_an_active_tty_controller():
    class InactiveKeyboard:
        active = False

        def set_quit_callback(self, callback) -> None:
            raise AssertionError("inactive keyboard must not receive a quit callback")

    tui = TUI(Console(file=StringIO(), width=80))
    tui.set_keyboard_controller(InactiveKeyboard())
    tui._live = object()

    assert await tui.hold_live() is False
