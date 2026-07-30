"""Prompt Toolkit bindings and redrawable selected-agent history."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings


def build_agent_prompt(tui: Any, base_prompt: Any) -> Any:
    """Return a dynamic prompt containing the selected agent's latest history.

    Prompt Toolkit owns this viewport and erases it when input is submitted, so
    switching focus replaces the prior agent instead of adding terminal scrollback.
    The revision-aware cache avoids re-rendering Rich history on every keystroke.
    """
    history_cache: dict[tuple[int, int, int], str] = {}

    def prompt() -> list[tuple[str, str]]:
        key = tui.selected_history_cache_key
        if key not in history_cache:
            try:
                history_cache[key] = tui.render_selected_history()
            except Exception:
                history_cache[key] = ""
        history = history_cache[key]

        fragments = []
        if history:
            fragments.extend(to_formatted_text(ANSI(history)))
            fragments.append(("", "\n"))
        fragments.extend(to_formatted_text(base_prompt))
        return fragments

    return prompt


def build_agent_navigation_bindings(tui: Any) -> KeyBindings:
    """Bind prompt-owned Tab navigation without touching the input buffer."""
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


__all__ = ["build_agent_navigation_bindings", "build_agent_prompt"]
