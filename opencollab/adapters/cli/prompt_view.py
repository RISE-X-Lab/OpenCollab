"""Prompt Toolkit key bindings for selecting the agent a line is addressed to."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.key_binding import KeyBindings


def build_agent_navigation_bindings(tui: Any) -> KeyBindings:
    """Bind prompt-owned Tab navigation without touching the input buffer.

    Selecting an agent reprints it, and that print has to go through the
    scrollback gate the CLI installs for the duration of the prompt — otherwise
    it lands in the middle of prompt_toolkit's own redraw.
    """
    bindings = KeyBindings()

    @bindings.add("tab")
    def select_next(event: Any) -> None:
        tui.select_next_agent()
        event.app.invalidate()

    @bindings.add("s-tab")
    def select_previous(event: Any) -> None:
        tui.select_previous_agent()
        event.app.invalidate()

    return bindings


__all__ = ["build_agent_navigation_bindings"]
