"""Prompt bottom-toolbar rendering: the one-line live team roster."""

from __future__ import annotations

from html import escape
from typing import Any

from prompt_toolkit.formatted_text import HTML

from opencollab.application.scheduler_types import RosterEntry, roster_display_state

_TOOLBAR_MUTED = "ansibrightblack"
_TOOLBAR_ACCENT = "ansicyan"
_TOOLBAR_SUCCESS = "ansigreen"
_TOOLBAR_WARNING = "ansiyellow"
_TOOLBAR_ERROR = "ansired"

_TOOLBAR_STATE_STYLES = {
    "running": _TOOLBAR_WARNING,
    "idle": _TOOLBAR_SUCCESS,
    "available": _TOOLBAR_MUTED,  # configured-only slots recede
    "failed": _TOOLBAR_ERROR,
    "stopped": _TOOLBAR_WARNING,
}


def _toolbar_style(text: Any, color: str) -> str:
    return f'<style fg="{color}">{escape(str(text))}</style>'


def format_team_toolbar(snapshot: list[RosterEntry]) -> HTML | str:
    """One-line team roster for the prompt bottom toolbar."""
    if not snapshot:
        return ""
    parts = []
    for entry in snapshot:
        aid = entry.get("aid")
        role = entry.get("role", "?")
        if aid is None:
            label = role  # configured role with no live agent yet
        elif aid == 0:
            label = "Lead"
        else:
            label = f"A{aid} {role}"
        state = roster_display_state(entry)
        state_style = _TOOLBAR_STATE_STYLES.get(str(state).lower(), _TOOLBAR_MUTED)
        parts.append(
            _toolbar_style(label, _TOOLBAR_MUTED)
            + _toolbar_style("(", _TOOLBAR_MUTED)
            + _toolbar_style(state, state_style)
            + _toolbar_style(")", _TOOLBAR_MUTED)
        )
    separator = _toolbar_style("  ", _TOOLBAR_MUTED)
    return HTML(
        _toolbar_style("Team:", _TOOLBAR_ACCENT)
        + _toolbar_style(" ", _TOOLBAR_MUTED)
        + separator.join(parts)
    )
