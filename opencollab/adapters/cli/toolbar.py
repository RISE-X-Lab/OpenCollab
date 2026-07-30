"""Prompt bottom-toolbar rendering: the one-line live team roster."""

from __future__ import annotations

from html import escape
from typing import Any

from prompt_toolkit.formatted_text import HTML

from opencollab.adapters.tui.theme import (
    BRAND_VIOLET,
    ERROR,
    MUTED,
    SUBTLE,
)
from opencollab.application.scheduler_types import RosterEntry, roster_display_state

_TOOLBAR_MUTED = MUTED
_TOOLBAR_SUBTLE = SUBTLE
_TOOLBAR_ACCENT = BRAND_VIOLET
_TOOLBAR_SUCCESS = SUBTLE
_TOOLBAR_ERROR = ERROR

_TOOLBAR_STATE_STYLES = {
    "running": _TOOLBAR_SUBTLE,
    "idle": _TOOLBAR_SUCCESS,
    "available": _TOOLBAR_MUTED,  # configured-only slots recede
    "failed": _TOOLBAR_ERROR,
    "stopped": _TOOLBAR_MUTED,
    "cancelled": _TOOLBAR_MUTED,
}


def _toolbar_style(text: Any, color: str) -> str:
    return f'<style fg="{color}">{escape(str(text))}</style>'


def format_team_toolbar(
    snapshot: list[RosterEntry],
    *,
    selected_aid: int = 0,
) -> HTML | str:
    """One-line team roster for the prompt bottom toolbar."""
    if not snapshot:
        return ""
    snapshot = sorted(
        (entry for entry in snapshot if isinstance(entry.get("aid"), int)),
        key=lambda entry: entry.get("aid", 0),
    ) + [entry for entry in snapshot if entry.get("aid") is None]

    def is_selected(entry: RosterEntry) -> bool:
        aid = entry.get("aid")
        return aid == selected_aid

    selected_position = next(
        (position for position, entry in enumerate(snapshot, 1) if is_selected(entry)),
        1,
    )
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
        focused = is_selected(entry)
        marker = _toolbar_style("◆ ", _TOOLBAR_ACCENT) if focused else ""
        label_style = _TOOLBAR_SUBTLE if aid is not None or focused else _TOOLBAR_MUTED
        parts.append(
            marker
            + _toolbar_style(f"{label} ", label_style)
            + _toolbar_style(state, state_style)
        )
    separator = _toolbar_style("  ", _TOOLBAR_MUTED)
    count = _toolbar_style(
        f"  {selected_position}/{len(snapshot)}",
        _TOOLBAR_MUTED,
    )
    return HTML(
        _toolbar_style("AGENTS", _TOOLBAR_SUBTLE)
        + count
        + _toolbar_style("  ", _TOOLBAR_MUTED)
        + separator.join(parts)
    )
