"""Team roster, status styling, and terminal viewport rendering tests."""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.text import Text

from opencollab.adapters.tui import TUI
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent


def _make_tui() -> TUI:
    return TUI()


def _assert_visible_text_has_non_white_style(text: Text) -> None:
    console = Console(color_system="truecolor")
    for offset, char in enumerate(text.plain):
        if char.isspace():
            continue
        style = text.get_style_at_offset(console, offset)
        assert style.color is not None
        assert style.color.name != "white"


# ---------------------------------------------------------------------------
# Team roster + inter-agent messaging events
# ---------------------------------------------------------------------------


def test_roster_tracks_spawn_and_completion_state():
    tui = _make_tui()
    tui.set_redraw(lambda: None)

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    assert tui._roster[1] == {"role": "coder", "state": "running"}

    tui.event_handler(SchedulerEvent("agent_completed", {"aid": 1, "role": "coder", "latency": 1.0}))
    assert tui._roster[1]["state"] == "idle"


def test_agent_resumed_marks_roster_running_again():
    tui = _make_tui()
    tui.set_redraw(lambda: None)

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "manager"}))
    tui.event_handler(SchedulerEvent("agent_completed", {"aid": 1, "role": "manager", "latency": 1.0}))
    assert tui._roster[1]["state"] == "idle"

    # The manager suspended on its own delegated work and was re-activated.
    tui.event_handler(SchedulerEvent("agent_resumed", {"aid": 1, "role": "manager"}))
    assert tui._roster[1]["state"] == "running"


def test_team_panel_renders_when_roster_present():
    tui = _make_tui()
    tui.set_redraw(lambda: None)
    assert tui._build_team_panel() is None

    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 2, "parent_aid": 0, "role": "reviewer"}))
    panel = tui._build_team_panel()
    assert panel is not None
    assert "A2" in panel.plain and "reviewer" in panel.plain


def test_team_provider_renders_configured_team_before_any_spawn():
    tui = _make_tui()
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "idle", "busy": False},
        {"aid": None, "role": "analyst", "phase": "available", "busy": False},
        {"aid": None, "role": "coder", "phase": "available", "busy": False},
    ])
    panel = tui._build_team_panel()
    assert panel is not None
    # Lead shown by name (idle), unspawned roles shown as available.
    assert panel.plain == (
        "AGENTS  1/3  ◆ Lead idle  analyst available  coder available  ⇧Tab/Tab"
    )


def test_team_panel_names_agent_zero_after_the_configured_entry_role():
    tui = _make_tui()
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "analyst", "phase": "idle", "busy": False},
        {"aid": None, "role": "coder", "phase": "available", "busy": False},
        {"aid": None, "role": "tester", "phase": "available", "busy": False},
    ])

    panel = tui._build_team_panel()

    assert "◆ Analyst idle" in panel.plain
    assert "Lead" not in panel.plain


def test_team_provider_marks_busy_agent_running():
    tui = _make_tui()
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "awaiting_events", "busy": True},
        {"aid": 1, "role": "coder", "phase": "executing_tools", "busy": True},
    ])
    panel = tui._build_team_panel()
    assert "Lead running" in panel.plain
    assert "A1 coder running" in panel.plain
    assert panel.plain.endswith("⇧Tab/Tab")


def test_team_provider_settled_phases_render_idle():
    tui = _make_tui()
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "done", "busy": False},
        {"aid": 1, "role": "coder", "phase": "idle", "busy": False},
    ])
    panel = tui._build_team_panel()
    # A settled 'done' agent and a fresh 'idle' agent both present as idle.
    assert "Lead idle" in panel.plain
    assert "A1 coder idle" in panel.plain


def test_team_provider_failure_falls_back_to_event_roster():
    tui = _make_tui()

    def boom():
        raise RuntimeError("dict changed size during iteration")

    tui.set_team_provider(boom)
    # No spawned agents and a failing provider -> no panel (no crash).
    assert tui._build_team_panel() is None
    # Once an agent spawns, the event-driven roster still renders.
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    panel = tui._build_team_panel()
    assert panel is not None and "A1" in panel.plain


def test_message_events_append_activity_lines():
    tui = _make_tui()
    tui.set_redraw(lambda: None)

    tui.event_handler(SchedulerEvent("agent_message_sent", {"from_aid": 0, "to_aid": 2}))
    tui.event_handler(SchedulerEvent("agent_message_delivered", {"to_aid": 2, "result_len": 10}))

    sent_history = "\n".join(
        block.plain
        for block in tui._state_for(0).history_blocks
        if hasattr(block, "plain")
    )
    delivered_history = "\n".join(
        block.plain
        for block in tui._state_for(2).history_blocks
        if hasattr(block, "plain")
    )
    assert "A0 → A2 message" in sent_history
    assert "A2 received message" in delivered_history


def test_status_lines_use_explicit_non_white_styles():
    tui = _make_tui()
    tui.set_redraw(lambda: None)

    tui.event_handler(SessionRuntimeEvent("step_start", {"step": 4, "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("budget_warning", {}))
    tui.event_handler(SessionRuntimeEvent("error", {"reason": "boom", "aid": 0}))

    for status in tui._status_lines:
        _assert_visible_text_has_non_white_style(status)


def test_status_chrome_renderables_avoid_default_text_color():
    tui = _make_tui()
    tui.set_redraw(lambda: None)

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "bash", "args": {"command": "pytest -q"}, "aid": 0},
        )
    )
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 2, "parent_aid": 0, "role": "reviewer"}))

    for block in tui._selected_state.history_blocks:
        if isinstance(block, Text):
            _assert_visible_text_has_non_white_style(block)

    _assert_visible_text_has_non_white_style(tui._build_status_row())


def test_status_row_stays_one_row_when_the_text_it_quotes_has_newlines():
    """The chrome quotes text the renderer does not control.

    A shell command, a tool argument and an error reason all reach the status
    row verbatim, and a newline in any of them turns the single shared row into
    three — undoing the whole point of collapsing the chrome onto one row.
    """
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)

    tui.event_handler(
        SessionRuntimeEvent(
            "tool_start",
            {"tool": "bash", "args": {"command": "echo one\necho two\r\necho three"}, "aid": 0},
        )
    )
    row = tui._build_status_row()
    assert "\n" not in row.plain and "\r" not in row.plain
    assert len(console.render_lines(row, console.options.update(width=100))) == 1

    # Same for the error path, whose reason comes straight from the runtime.
    tui.set_redraw(lambda: None)
    tui.event_handler(
        SessionRuntimeEvent("error", {"reason": "boom\n  File x.py, line 1\n  KeyError", "aid": 0})
    )
    tui._active_tools.clear()
    row = tui._build_status_row()
    assert "\n" not in row.plain
    assert len(console.render_lines(row, console.options.update(width=100))) == 1
    # Scrollback still gets the whole message — only the one-row chrome folds.
    assert "File x.py" in console.file.getvalue()


def test_the_hud_tails_when_content_exceeds_terminal_height():
    console = Console(file=StringIO(), width=40, height=4)
    tui = TUI(console)
    tui.set_redraw(lambda: None)
    for index in range(8):
        tui._status_lines.append(Text(f"status {index}", style="#6B7280"))

    display = tui._build_hud()
    lines = console.render_lines(display, console.options, pad=False)
    plain = "\n".join("".join(segment.text for segment in line) for line in lines)

    assert len(lines) <= console.height
    assert "status 7" in plain
    assert "status 0" not in plain


def test_narrow_agent_status_keeps_selected_agent_visible():
    console = Console(file=StringIO(), width=40, color_system=None)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "done", "busy": False},
        {"aid": 1, "role": "analyst", "phase": "done", "busy": False},
        {"aid": 5, "role": "reviewer", "phase": "executing_tools", "busy": True},
    ])
    tui.select_agent(5)

    rendered = console.render_lines(tui._build_team_panel(), console.options, pad=False)
    plain = "".join(segment.text for segment in rendered[0])

    assert len(rendered) == 1
    assert "3/3" in plain
    assert "◆ A5 running" in plain
    assert plain.index("Lead") < plain.index("A1") < plain.index("◆ A5")


def _hud_rows(tui: TUI) -> list[str]:
    console = tui.console
    rendered = console.render_lines(tui._build_hud(), console.options, pad=False)
    return ["".join(segment.text for segment in line) for line in rendered]


def _stream(tui: TUI, paragraphs: int) -> None:
    """Give the frame a body taller than any terminal under test."""
    tui._state_for(0).current_text = "\n\n".join(
        f"streamed paragraph {index}" for index in range(paragraphs)
    )


def test_agent_status_remains_last_row_when_the_hud_is_cropped():
    console = Console(file=StringIO(), width=50, height=3, color_system=None)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "done", "busy": False},
        {"aid": 1, "role": "coder", "phase": "executing_tools", "busy": True},
    ])
    _stream(tui, 8)

    rows = _hud_rows(tui)

    assert len(rows) == console.height
    # The body is cropped to its tail; the status row is not part of that budget.
    assert "AGENTS" in rows[-1]
    assert "streamed paragraph 7" in "\n".join(rows[:-1])
    assert "streamed paragraph 0" not in "\n".join(rows)


def test_agent_status_is_pinned_to_terminal_bottom_when_content_is_short():
    console = Console(file=StringIO(), width=50, height=6, color_system=None)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "analyst", "phase": "done", "busy": False},
        {"aid": 1, "role": "coder", "phase": "done", "busy": False},
    ])
    tui._state_for(0).current_text = "short body"

    rows = _hud_rows(tui)

    # A short frame must claim only the rows it needs. Padding it out to the
    # terminal height is what buried the settled transcript printed above it.
    assert len(rows) < console.height
    assert "short body" in rows[0]
    assert "AGENTS" in rows[-1]
    assert all("AGENTS" not in row for row in rows[:-1])


def test_agent_status_owns_only_row_in_one_line_terminal():
    console = Console(file=StringIO(), width=50, height=1, color_system=None)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "done", "busy": False},
    ])
    tui._state_for(0).current_text = "body is hidden"

    rows = _hud_rows(tui)

    assert len(rows) == 1
    assert "AGENTS" in rows[0]
    assert "body is hidden" not in rows[0]


def test_pinned_agent_status_reflows_to_resized_terminal_height():
    console = Console(file=StringIO(), width=50, height=6, color_system=None)
    tui = TUI(console)
    tui.set_team_provider(lambda: [
        {"aid": 0, "role": "lead", "phase": "done", "busy": False},
    ])
    _stream(tui, 8)
    assert len(_hud_rows(tui)) == 6

    console.height = 3

    rows = _hud_rows(tui)
    assert len(rows) == 3
    assert "AGENTS" in rows[-1]


def test_reset_clears_live_state_but_preserves_agent_history_and_roster():
    tui = _make_tui()
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    tui.select_agent(1)
    history = list(tui._state_for(1).history_blocks)
    tui.reset()
    assert tui._roster[1] == {"role": "coder", "state": "running"}
    assert tui.selected_aid == 1
    assert list(tui._agent_states) == [0, 1]
    assert tui._state_for(1).history_blocks == history
    assert tui._state_for(1).active_tools == {}
    assert tui._state_for(1).status_lines == []
    assert tui._state_for(1).thinking is None
