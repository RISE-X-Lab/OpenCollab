"""Display building for the TUI: team panel, timeline, and live viewport.

Mixed into ``renderer.TUI`` — methods render the state that
``renderer_events`` maintains. Also owns the shared style palette.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment
from rich.text import Text

from opencollab.application.scheduler_types import roster_display_state


class _LineViewport:
    """A pre-rendered, bottom-aligned live viewport."""

    def __init__(self, lines: list[list[Segment]]) -> None:
        self.lines = lines

    def __rich_console__(self, console: Console, options: Any) -> Any:
        for index, line in enumerate(self.lines):
            yield from line
            if index < len(self.lines) - 1:
                yield Segment.line()


class _RendererDisplayMixin:
    """Builds Rich renderables from the TUI's current state."""

    _STYLE_MUTED = "bright_black"
    _STYLE_ACCENT = "cyan"
    _STYLE_SUCCESS = "green"
    _STYLE_WARNING = "yellow"
    _STYLE_ERROR = "red"
    _STYLE_HEADING = "bold cyan"

    _STATE_STYLES = {
        "running": _STYLE_WARNING,
        "idle": _STYLE_SUCCESS,
        "failed": _STYLE_ERROR,
        "cancelled": _STYLE_WARNING,
    }

    def _agent_label(self, aid: int) -> str:
        return "Lead" if aid == 0 else f"A{aid}"

    def _is_visible(self, aid: int) -> bool:
        """Whether an agent's session stream should render under the filter."""
        if not self._filter_messages:
            return True
        return aid == self._selected_aid

    # Argument keys to preview, in priority order; ``command`` reads as code.
    _PREVIEW_KEYS = ("command", "task", "path", "file_path")

    def _args_preview(self, payload: Any, *, limit: int = 80, code: bool = False) -> str:
        """Short preview of a tool's arguments for activity lines and spinners.

        ``payload`` may be a bare args dict, or a full event-data dict whose args
        live under ``"args"`` (session tools) or directly on it (scheduler events
        such as ``agent_spawned``, which carry ``task`` at the top level).
        """
        if not isinstance(payload, dict):
            return ""
        sources = []
        nested = payload.get("args")
        if isinstance(nested, dict):
            sources.append(nested)
        sources.append(payload)
        for source in sources:
            for key in self._PREVIEW_KEYS:
                value = source.get(key)
                if isinstance(value, str):
                    text = value[:limit]
                    return f" `{text}`" if code and key == "command" else f" {text}"
        return ""

    def _team_entries(self) -> list[tuple[int | None, str, str]]:
        """(aid, role, state) tuples for the panel: the configured roster from
        the provider when available, else the event-driven spawned roster."""
        if self._team_provider is not None:
            try:
                roster = self._team_provider() or []
            except Exception:
                roster = []
            if roster:
                return [
                    (e.get("aid"), e.get("role", "?"), roster_display_state(e))
                    for e in roster
                ]
        return [
            (aid, self._roster[aid]["role"], self._roster[aid]["state"])
            for aid in sorted(self._roster)
        ]

    def _build_team_panel(self) -> Any | None:
        """Compact team roster, or None when no team exists."""
        entries = self._team_entries()
        if not entries:
            return None
        chips: list[tuple[str, str]] = []
        for aid, role, state in entries:
            style = self._STATE_STYLES.get(state, self._STYLE_MUTED)
            focused = self._filter_messages and aid == self._selected_aid
            if chips:
                chips.append(("  ", self._STYLE_MUTED))
            if aid is None:
                chips.append((f"{role} ", "bold cyan"))
            elif aid == 0:
                chips.append((f"{'▶ ' if focused else ''}Lead ", "bold cyan"))
            else:
                chips.append((f"{'▶ ' if focused else ''}A{aid} ", "bold cyan"))
                chips.append((f"{role} ", self._STYLE_MUTED))
            chips.append((f"[{state}]", style))
        header = (
            f"Team (showing {self._agent_label(self._selected_aid)}): "
            if self._filter_messages
            else "Team: "
        )
        return Text.assemble((header, self._STYLE_HEADING), *chips)

    def _build_display(self) -> Any:
        """Build the Rich renderable for current state."""
        parts = []

        # Chronological blocks (assistant text snapshots + activity lines).
        if self._timeline_blocks:
            parts.extend(self._timeline_blocks)
            parts.append(Text("", style=""))

        # Current streaming chunk.
        if self._current_text:
            parts.append(Markdown(self._current_text))
            parts.append(Text("", style=""))

        # Active tool spinners
        if self._active_tools:
            parts.append(Text("Running", style=self._STYLE_HEADING))
            for label, data in self._active_tools.items():
                args_preview = self._args_preview(data, limit=60, code=True)

                spinner_text = Text.assemble(
                    ("  ", self._STYLE_MUTED),
                    (f"[{label}]", self._STYLE_HEADING),
                    (args_preview, self._STYLE_MUTED),
                )
                parts.append(spinner_text)
            parts.append(Text("", style=""))

        if self._status_lines:
            parts.extend(self._status_lines)
            parts.append(Text("", style=""))

        team_panel = self._build_team_panel()
        if team_panel is not None:
            parts.append(team_panel)

        if not parts:
            return Text("Thinking...", style="dim")

        from rich.console import Group
        return Group(*parts)

    def _build_live_display(self) -> Any:
        """Build a terminal-height live frame focused on newest output."""
        display = self._build_display()
        max_height = max(1, self.console.height)
        lines = self.console.render_lines(display, self.console.options, pad=False)
        if len(lines) <= max_height:
            return display
        return _LineViewport(lines[-max_height:])
