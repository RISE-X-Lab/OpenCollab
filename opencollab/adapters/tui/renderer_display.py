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

from opencollab.adapters.tui.brand_motion import MARK_HEX, PulseDot
from opencollab.adapters.tui.theme import (
    BRAND_VIOLET,
    ERROR,
    MUTED,
    PRIMARY,
    SUBTLE,
    SUCCESS,
    WARNING,
)
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


class _PinnedFooterViewport:
    """A live body that reserves the terminal's physical bottom row for status."""

    def __init__(self, body: Any, footer: Any) -> None:
        self.body = body
        self.footer = footer

    def __rich_console__(self, console: Console, options: Any) -> Any:
        max_height = max(1, console.height)
        body_lines = console.render_lines(self.body, options, pad=False)
        footer_lines = console.render_lines(self.footer, options, pad=False)
        if len(footer_lines) >= max_height:
            lines = footer_lines[-max_height:]
        else:
            body_height = max_height - len(footer_lines)
            visible_body = body_lines[-body_height:] if body_height else []
            spacer = [[] for _ in range(body_height - len(visible_body))]
            lines = [*visible_body, *spacer, *footer_lines]
        yield from _LineViewport(lines).__rich_console__(console, options)


class _RendererDisplayMixin:
    """Builds Rich renderables from the TUI's current state."""

    # Neutral terminal chrome with one sparse OC-violet focus color. Warning and
    # error hues are reserved for exceptional outcomes, not ordinary activity.
    _STYLE_MUTED = MUTED
    _STYLE_SUBTLE = SUBTLE
    _STYLE_ACCENT = BRAND_VIOLET
    _STYLE_SUCCESS = SUCCESS
    _STYLE_WARNING = WARNING
    _STYLE_ERROR = ERROR
    _STYLE_HEADING = f"bold {PRIMARY}"
    _STYLE_KEY = SUBTLE

    # Bold marker glyphs for activity lines — a marker reads louder than its sentence.
    _MARK_START = ("▸ ", BRAND_VIOLET)
    _MARK_DONE = ("▪ ", SUCCESS)
    _MARK_FAIL = ("✗ ", f"bold {ERROR}")
    _MARK_WARN = ("⊘ ", WARNING)
    _MARK_MSG = ("⇄ ", BRAND_VIOLET)
    _MARK_DOT = ("· ", MUTED)

    # State → color for team chips + toolbar parity. "available" = configured
    # but un-spawned: it recedes to muted grey so empty slots don't draw a glance.
    _STATE_STYLES = {
        "running": _STYLE_SUBTLE,
        "idle": _STYLE_SUCCESS,
        "available": _STYLE_MUTED,
        "failed": _STYLE_ERROR,
        "stopped": _STYLE_MUTED,
        "cancelled": _STYLE_MUTED,
    }

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
        """One-line bottom status roster, or None when no team exists."""
        entries = self._team_entries()
        if not entries:
            return None
        entries = sorted(
            (entry for entry in entries if isinstance(entry[0], int)),
            key=lambda entry: entry[0],
        ) + [entry for entry in entries if entry[0] is None]

        def is_selected(entry: tuple[int | None, str, str]) -> bool:
            aid, _role, _state = entry
            return aid == self._selected_aid

        selected_position = next(
            (position for position, entry in enumerate(entries, 1) if is_selected(entry)),
            1,
        )

        def build_line(*, compact: bool) -> Text:
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append("AGENTS", style=self._STYLE_HEADING)
            line.append(
                f"  {selected_position}/{len(entries)}",
                style=self._STYLE_MUTED,
            )
            for aid, role, state in entries:
                style = self._STATE_STYLES.get(state, self._STYLE_MUTED)
                focused = is_selected((aid, role, state))
                line.append("  ", style=self._STYLE_MUTED)
                if focused:
                    line.append("◆ ", style=self._STYLE_ACCENT)
                if aid is None:
                    line.append(role, style=self._STYLE_MUTED)
                    if not compact:
                        line.append(f" {state}", style=style)
                elif aid == 0:
                    label_style = self._STYLE_HEADING if focused else self._STYLE_SUBTLE
                    line.append(self._agent_label(aid), style=label_style)
                    if not compact or focused:
                        line.append(f" {state}", style=style)
                else:
                    label_style = self._STYLE_HEADING if focused else self._STYLE_SUBTLE
                    line.append(f"A{aid}", style=label_style)
                    if not compact:
                        line.append(f" {role}", style=self._STYLE_MUTED)
                    if not compact or focused:
                        line.append(f" {state}", style=style)
            if len(entries) > 1 and not compact:
                line.append("  ⇧Tab/Tab", style=self._STYLE_MUTED)
            if self._holding_for_exit and not compact:
                line.append("  q close", style=self._STYLE_MUTED)
            return line

        line = build_line(compact=False)
        if line.cell_len > self.console.width:
            line = build_line(compact=True)
        line.truncate(max(1, self.console.width), overflow="ellipsis")
        return line

    def _new_thinking_bar(self, label: str) -> PulseDot:
        """A labeled waiting indicator: a gently pulsing brand dot + ``label`` +
        a live elapsed-seconds counter. Uses the muted chrome style so every
        non-dot glyph stays an explicit, non-white color."""
        return PulseDot(label, muted_style=self._STYLE_MUTED)

    def _assistant_block(self, body: Any) -> Any:
        """Wrap an assistant Markdown block with a brand ``◆`` gutter marker
        aligned to its first line (matching the welcome banner wordmark). A
        two-column ``Table.grid`` — a narrow marker column + the body column —
        keeps the Markdown wrapping intact under the marker."""
        from rich.table import Table

        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True)   # narrow ◆ gutter (top-aligned by default)
        grid.add_column(ratio=1)        # markdown body
        grid.add_row(Text("◆", style=MARK_HEX), body)
        return grid

    def _user_block(self, content: str) -> Any:
        """Render one user message inside the same redrawable transcript."""
        from rich.table import Table

        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(
            Text("❯", style=self._STYLE_ACCENT),
            Text(content, style=self._STYLE_SUBTLE),
        )
        return grid

    def _build_display(self, *, include_team_panel: bool = True) -> Any:
        """Build the Rich renderable for current state."""
        parts = []

        # Chronological blocks (assistant text snapshots + activity lines).
        if self._timeline_blocks:
            parts.extend(self._timeline_blocks)
            parts.append(Text("", style=""))

        # Current streaming chunk — fronted by the brand ◆ gutter marker.
        if self._current_text:
            parts.append(self._assistant_block(Markdown(self._current_text)))
            parts.append(Text("", style=""))

        # Active tool spinners — the shared pulsing brand dot, one calm
        # motion for every tool. Each tool is a one-row Table.grid so the spinner
        # cell and label cell stay on a single render line (the block is captured
        # by render_lines in _build_live_display).
        if self._active_tools:
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
                row.add_row(self._motion, label_text)
                parts.append(row)
            parts.append(Text("", style=""))

        if self._status_lines:
            parts.extend(self._status_lines)
            parts.append(Text("", style=""))

        # LLM-wait indicator: the animated pulsing dot (replaces the old static
        # "thinking" line). Shown while waiting on the model; cleared as soon as
        # text or tool progress arrives. Being time-driven, it keeps pulsing on
        # Live's refresh even when no new events land — so it never looks frozen.
        if self._thinking is not None:
            parts.append(self._thinking)
            parts.append(Text("", style=""))

        if include_team_panel:
            team_panel = self._build_team_panel()
            if team_panel is not None:
                parts.append(team_panel)

        if not parts:
            # Nothing yet — still animate the wait so first-token latency doesn't
            # look frozen. Route the placeholder through the same pulsing dot.
            return self._thinking or self._new_thinking_bar("Thinking…")

        from rich.console import Group
        return Group(*parts)

    def _build_live_display(self) -> Any:
        """Build a live frame with the team status on the terminal's bottom row."""
        max_height = max(1, self.console.height)
        team_panel = self._build_team_panel()
        if team_panel is None:
            display = self._build_display()
            lines = self.console.render_lines(display, self.console.options, pad=False)
            if len(lines) <= max_height:
                return display
            return _LineViewport(lines[-max_height:])

        body = self._build_display(include_team_panel=False)
        return _PinnedFooterViewport(body, team_panel)

    def _build_settled_display(self, *, aid: int = 0) -> Any | None:
        """Build one settled turn for an explicit scrollback handoff.

        Interactive mode keeps this material in the redrawable history viewport;
        one-shot mode uses this slice so it still leaves a normal terminal result.
        Live-only chrome (spinner, transient status, roster) is excluded.
        """
        state = self._state_for(aid)
        if state.current_text:
            self._flush_current_text_to_timeline(state)
        return self._build_history_display(state, start=state.turn_history_start)

    def _build_history_display(self, state: Any, *, start: int = 0) -> Any | None:
        """Build the requested slice of one agent's history without live chrome."""
        blocks = list(state.history_blocks[start:])
        if start == 0 and state.history_omitted_blocks:
            blocks.insert(
                0,
                Text(
                    f"... {state.history_omitted_blocks} older history blocks "
                    "omitted; full history remains in the run trace.",
                    style=self._STYLE_MUTED,
                ),
            )
        if state.current_text:
            blocks.append(self._assistant_block(Markdown(state.current_text)))
        if not blocks:
            return None
        from rich.console import Group

        return Group(*blocks)

    def render_selected_history(self) -> str:
        """Render the selected agent's complete history for Prompt Toolkit."""
        display = self._build_history_display(self._selected_state)
        if display is None:
            return ""
        with self.console.capture() as capture:
            self.console.print(display, end="")
        return capture.get().rstrip("\n")
