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

    # ── Calm-HUD palette: one accent (cyan) + a two-tier grey hierarchy. ──
    # State lives in color; every value is an explicit, non-"white" style so
    # the per-glyph chrome walk in the tests passes.
    _STYLE_MUTED = "grey46"        # chrome: markers, dividers, separators, args
    _STYLE_SUBTLE = "grey58"       # secondary text: stats numbers, session path
    _STYLE_ACCENT = "cyan"         # the one identity hue: brand, in-flight, messages
    _STYLE_SUCCESS = "green3"      # outcome only: finished / received / idle
    _STYLE_WARNING = "gold3"       # outcome only: loop / budget / cancelled / running
    _STYLE_ERROR = "red3"          # outcome only: failed / error
    _STYLE_HEADING = "bold cyan"   # section headings ("Running"), banner wordmark
    _STYLE_KEY = "grey58"          # banner key column ("model", "cwd", …)

    # Bold marker glyphs for activity lines — a marker reads louder than its sentence.
    _MARK_START = ("▸ ", "cyan")          # work begun (in-flight, cyan)
    _MARK_DONE = ("▪ ", "bold green3")    # finished / received
    _MARK_FAIL = ("✗ ", "bold red3")      # failed / error
    _MARK_WARN = ("⊘ ", "bold gold3")     # cancelled
    _MARK_MSG = ("⇄ ", "bold cyan")       # inter-agent message
    _MARK_DOT = ("· ", "grey46")          # neutral default / resumed

    # State → color for team chips + toolbar parity. "available" = configured
    # but un-spawned: it recedes to muted grey so empty slots don't draw a glance.
    _STATE_STYLES = {
        "running": _STYLE_WARNING,
        "idle": _STYLE_SUCCESS,
        "available": _STYLE_MUTED,
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
            if focused and aid is not None:
                chips.append(("▶ ", self._STYLE_ACCENT))  # explicit accent glyph
            if aid is None:
                chips.append((f"{role} ", self._STYLE_HEADING))
            elif aid == 0:
                chips.append(("Lead ", self._STYLE_HEADING))
            else:
                chips.append((f"A{aid} ", self._STYLE_HEADING))
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

        # Active tool spinners — animated, accent-hued. Each tool is a one-row
        # Table.grid so the spinner cell and label cell stay on a single render
        # line (the block is captured by render_lines in _build_live_display).
        if self._active_tools:
            from rich.spinner import Spinner
            from rich.table import Table

            parts.append(Text("Running", style=self._STYLE_HEADING))
            for label, data in self._active_tools.items():
                args_preview = self._args_preview(data, limit=60, code=True)

                label_text = Text.assemble(
                    (label, self._STYLE_ACCENT),        # tool name carries the accent
                    (args_preview, self._STYLE_MUTED),  # preview stays dim (" `ls -la`")
                )
                row = Table.grid(padding=(0, 1))
                row.add_column(no_wrap=True)            # spinner cell
                row.add_column(ratio=1)                 # label + args cell
                row.add_row(Spinner("dots", style=self._STYLE_ACCENT), label_text)
                parts.append(row)
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

    def _build_settled_display(self) -> Any | None:
        """The persistent transcript committed to scrollback when a turn ends.

        Only the conversation — assistant text + tool/activity lines — is kept.
        The live-only HUD (running spinner, transient status, team roster) is
        dropped so the settled view stays focused on the reply; the team remains
        visible in the prompt's bottom toolbar. Returns ``None`` when the turn
        produced nothing worth persisting.
        """
        if self._current_text:
            self._flush_current_text_to_timeline()
        if not self._timeline_blocks:
            return None
        from rich.console import Group

        return Group(*self._timeline_blocks)
