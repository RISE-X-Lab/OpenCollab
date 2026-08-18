"""Scrollback behaviour of the TUI: what reaches the terminal, and when.

Split out of ``test_tui_event_rendering`` — those tests cover event dispatch,
these cover the printing contract: only the focused agent's settled blocks are
written, each exactly once, and focusing an agent redraws it in full.
"""

from __future__ import annotations

from io import StringIO

from rich.console import Console
from rich.text import Text

from opencollab.adapters.tui import TUI
from opencollab.adapters.tui import renderer as renderer_mod
from opencollab.adapters.tui import renderer_display as renderer_display_mod
from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent


def _make_tui() -> TUI:
    return TUI(Console(file=StringIO(), width=100, color_system=None))


def _scrollback(tui: TUI) -> str:
    """Everything the TUI has committed to the terminal so far."""
    return tui.console.file.getvalue()


def _history_plains(tui: TUI, aid: int) -> list[str]:
    return [
        block.plain
        for block in tui._state_for(aid).history_blocks
        if isinstance(block, Text)
    ]


def test_live_frame_stays_bounded_while_full_history_reaches_scrollback():
    console = Console(file=StringIO(), width=100, height=24, color_system=None)
    tui = TUI(console)
    tui.select_agent(1)

    for index in range(100):
        tui._append_activity((f"activity {index}", tui._STYLE_MUTED))

    frame = console.render_lines(tui._build_live_display(), console.options, pad=False)
    assert len(frame) <= renderer_display_mod.MAX_LIVE_BODY_LINES
    scrollback = _scrollback(tui)
    assert "activity 0" in scrollback
    assert "activity 99" in scrollback


def test_every_settled_block_path_reaches_scrollback_for_the_focused_agent():
    tui = TUI(Console(file=StringIO(), width=100, color_system=None))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "assistant say", "aid": 1}))
    assert tui.select_agent(1) == 1

    tui.event_handler(SessionRuntimeEvent("error", {"reason": "failure here", "aid": 1}))
    tui.record_user_message(1, "user asks")
    tui.event_handler(
        SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "pwd"}, "aid": 1})
    )

    scrollback = _scrollback(tui)
    for expected in ("assistant say", "failure here", "user asks", "A1:bash started"):
        assert expected in scrollback


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
    scrollback = _scrollback(tui)
    assert "bounded activity 0" not in scrollback
    assert "20 older history blocks omitted" in scrollback
    assert f"bounded activity {renderer_mod.MAX_HISTORY_BLOCKS_PER_AGENT + 19}" in scrollback


def test_user_message_is_recorded_only_in_target_and_printed_only_when_focused():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)

    tui.record_user_message(2, "please inspect the renderer")

    assert tui._state_for(0).history_blocks == []
    assert len(tui._state_for(2).history_blocks) == 1
    # Focus is still agent 0, so agent 2's line has not reached the terminal.
    assert "please inspect the renderer" not in _scrollback(tui)

    tui.select_agent(2)
    assert "please inspect the renderer" in _scrollback(tui)


def test_stop_live_settles_child_final_text_and_error_into_its_history():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui._live_paused = True
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "final child text", "aid": 1}))
    tui.event_handler(SessionRuntimeEvent("error", {"reason": "child error", "aid": 1}))

    tui.stop_live()
    tui.select_agent(1)

    assert tui._state_for(1).current_text == ""
    scrollback = _scrollback(tui)
    assert "final child text" in scrollback
    assert "Error: child error" in scrollback


def test_agent_history_accumulates_across_turn_resets():
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "turn one", "aid": 1}))
    tui.stop_live()

    tui.reset()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "turn two", "aid": 1}))
    tui.stop_live()
    tui.select_agent(1)

    scrollback = _scrollback(tui)
    assert "turn one" in scrollback
    assert "turn two" in scrollback


def test_scrollback_is_append_only_across_turns_for_the_focused_agent():
    """A new turn must not reprint the previous one — the terminal already has it."""
    console = Console(file=StringIO(), width=100, color_system=None)
    tui = TUI(console)
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "old answer", "aid": 0}))
    tui.stop_live()

    tui.reset()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "new answer", "aid": 0}))
    tui.stop_live()

    scrollback = _scrollback(tui)
    assert scrollback.count("old answer") == 1
    assert scrollback.count("new answer") == 1
    assert scrollback.index("old answer") < scrollback.index("new answer")


def test_switching_does_not_lose_partial_text_or_history_order():
    tui = _make_tui()
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
    assert _history_plains(tui, 1) == ["▸ A1:bash started pwd"]
    assert len(tui._state_for(1).history_blocks) == 2
    tui.select_agent(1)
    assert "A1:bash" in tui._active_tools


def test_legacy_event_without_aid_routes_to_lead_not_current_focus():
    tui = TUI()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child", "aid": 1}))
    tui.select_agent(1)

    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead"}))

    assert tui._state_for(0).current_text == "lead"
    assert tui._state_for(1).current_text == "child"


def test_stop_live_settles_every_agent_but_prints_only_the_focused_one():
    tui = _make_tui()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead answer", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child notes", "aid": 1}))

    tui.stop_live()

    # Both agents' streamed text is committed, so neither is lost on a later switch.
    assert tui._state_for(0).current_text == ""
    assert tui._state_for(1).current_text == ""
    scrollback = _scrollback(tui)
    assert "lead answer" in scrollback
    assert "child notes" not in scrollback

    tui.select_agent(1)
    assert "child notes" in _scrollback(tui)


def test_finishing_on_an_unfocused_agent_prints_its_tail_exactly_once():
    """The one-shot flush drains a tail; it must not replay the transcript.

    A focus switch reprints an agent in full, which is right when the user asks
    to look at one. Ending a run is not that ask: everything the target settled
    while it held focus is already on screen.
    """
    tui = _make_tui()
    tui.event_handler(SchedulerEvent("agent_spawned", {"aid": 1, "parent_aid": 0, "role": "coder"}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "early lead line", "aid": 0}))
    tui.event_handler(
        SessionRuntimeEvent("tool_start", {"tool": "bash", "args": {"command": "pwd"}, "aid": 0})
    )
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "the final answer", "aid": 0}))

    tui.select_agent(1)  # the user wanders off to watch a teammate
    tui.stop_live(final_aid=0)

    scrollback = _scrollback(tui)
    assert scrollback.count("early lead line") == 1
    assert scrollback.count("the final answer") == 1
    # Under a band naming the lead, because the rows above belong to the teammate.
    band = tui._agent_label(0)
    assert scrollback.rindex(band) < scrollback.index("the final answer")


def test_stop_live_reports_whose_trailing_text_it_committed():
    """The CLI salvages a failed turn's partial answer only if this says no."""
    tui = _make_tui()
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "lead partial", "aid": 0}))
    tui.event_handler(SessionRuntimeEvent("text_delta", {"content": "child partial", "aid": 1}))

    tui.stop_live()

    # The lead held focus, so its partial reached scrollback; the child's did not.
    assert tui.drained_partial_answer(0) is True
    assert tui.drained_partial_answer(1) is False
    # An agent that streamed nothing has no partial to have been committed.
    assert tui.drained_partial_answer(2) is False


def test_overlapping_prompts_keep_the_gate_until_the_last_one_releases():
    """Two agents can hold a prompt at once; the first to finish must not
    uninstall the gate the other is still relying on."""
    tui = _make_tui()
    withheld: list[object] = []

    def outer_gate(emit) -> None:
        withheld.append(emit)

    def inner_gate(emit) -> None:
        withheld.append(emit)

    tui.set_scrollback_gate(outer_gate)   # agent 1's permission y/N
    tui.set_scrollback_gate(inner_gate)   # agent 2's ask_user question
    tui.set_scrollback_gate(None)         # agent 2 answers first

    assert tui._scrollback_gate is outer_gate

    tui.record_user_message(0, "settled while the first prompt is still up")

    # Withheld, not printed behind the surviving prompt's back.
    assert len(withheld) == 1
    assert _scrollback(tui) == ""

    tui.set_scrollback_gate(None)
    assert tui._scrollback_gate is None


def test_focus_switch_scrolls_the_screen_away_instead_of_erasing_it():
    """The redraw opens on row 1, and the rows it displaced stay in scrollback.

    ``ESC[2J`` would also blank the screen, but erased rows never reach
    scrollback — the previous agent's last screenful would be lost.
    """
    console = Console(
        file=StringIO(), width=100, height=24, force_terminal=True, color_system=None
    )
    tui = TUI(console)
    tui.record_user_message(0, "lead line")
    tui.record_user_message(1, "child line")

    tui.select_agent(1)

    scrollback = _scrollback(tui)
    scroll_to_top = "\n" * console.height + "\x1b[H"
    assert scrollback.count(scroll_to_top) == 1
    before, after = scrollback.split(scroll_to_top)
    # Displaced, not erased: the lead's line is still above the switch.
    assert "lead line" in before
    assert "child line" in after
    assert "\x1b[2J" not in scrollback and "\x1b[3J" not in scrollback


def test_focus_switch_leaves_a_redirected_terminal_alone():
    """Cursor control is meaningless in a file or a CI capture — emit none of it."""
    tui = _make_tui()
    tui.record_user_message(1, "child line")

    tui.select_agent(1)

    scrollback = _scrollback(tui)
    assert "child line" in scrollback
    assert "\x1b[" not in scrollback


def test_focus_switch_scroll_is_gated_with_the_redraw_it_belongs_to():
    """The scroll must reach the terminal through the same gate as the blocks.

    Splitting them would let a prompt_toolkit redraw land between the scroll and
    the agent it was making room for.
    """
    console = Console(
        file=StringIO(), width=100, height=24, force_terminal=True, color_system=None
    )
    tui = TUI(console)
    tui.record_user_message(1, "child line")
    withheld: list[object] = []
    tui.set_scrollback_gate(withheld.append)

    tui.select_agent(1)

    assert len(withheld) == 1
    assert _scrollback(tui) == ""
