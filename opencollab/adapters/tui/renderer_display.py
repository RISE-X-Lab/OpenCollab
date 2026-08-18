"""Display building for the TUI: team panel, live viewport, and block chrome.

Mixed into ``renderer.TUI`` — methods render the state that
``renderer_events`` maintains. Also owns the shared style palette.

Settled blocks are printed to scrollback by ``renderer``; what is built here is
only the in-flight remainder — the HUD the prompt paints above its input line —
so the frame stays proportional to its content instead of claiming rows it has
nothing to say in.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
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

# Ceiling on the HUD body, in terminal rows. The frame carries only in-flight
# chrome, so it is normally far shorter; this bounds the pathological case (a
# long streamed answer) so the HUD can never push the input line off the screen
# it shares with the transcript.
MAX_LIVE_BODY_LINES = 12


def collapse_rows(value: str) -> str:
    """Fold CR/LF into spaces so one line of chrome stays one terminal row.

    Chrome quotes text the renderer does not control — a shell command, a tool
    argument, an error reason. A newline in any of them turns the single shared
    status row into three, which is the regression the one-row chrome exists to
    prevent. Width is already handled downstream by truncation.
    """
    if "\n" not in value and "\r" not in value:
        return value
    return " ".join(value.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def collapse_text_rows(text: Text) -> Text:
    """``collapse_rows`` for a styled ``Text``.

    Status lines carry a single base style rather than per-word spans, so
    rebuilding the string keeps the line looking the same.
    """
    plain = text.plain
    if "\n" not in plain and "\r" not in plain:
        return text
    return Text(collapse_rows(plain), style=text.style)


class _LineViewport:
    """A pre-rendered, bottom-aligned view of a taller body.

    ``close`` terminates the final line. Every ordinary Rich renderable does,
    and a group relies on it: left open, the renderable that follows is
    appended to a row that is already the full terminal width, and the crop
    to that width silently swallows it.
    """

    def __init__(self, lines: list[list[Segment]], *, close: bool = False) -> None:
        self.lines = lines
        self.close = close

    def __rich_console__(self, console: Console, options: Any) -> Any:
        for index, line in enumerate(self.lines):
            yield from line
            if self.close or index < len(self.lines) - 1:
                yield Segment.line()


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
                    text = collapse_rows(value)[:limit]
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

    def _build_team_panel(self, *, width: int | None = None) -> Any | None:
        """The roster half of the status row, or None when no team exists.

        ``width`` is the space left after the activity half; it defaults to the
        whole terminal for callers that render the roster on its own.
        """
        available = max(1, self.console.width if width is None else width)
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
            return line

        line = build_line(compact=False)
        if line.cell_len > available:
            line = build_line(compact=True)
        line.truncate(available, overflow="ellipsis")
        return line

    def _activity_text(self) -> Text | None:
        """The activity half of the status row: what this agent is doing now.

        One line, so the three in-flight signals are ranked rather than stacked:
        a running tool is the most concrete, the model wait is next, and a
        transient status note only shows when nothing else is happening.
        """
        if self._active_tools:
            label, data = next(iter(self._active_tools.items()))
            text = self._motion.render()
            text.append("  ", style=self._STYLE_MUTED)
            text.append(str(data.get("_display_label", label)), style=self._STYLE_ACCENT)
            text.append(self._args_preview(data, limit=40, code=True), style=self._STYLE_MUTED)
            if len(self._active_tools) > 1:
                text.append(f"  +{len(self._active_tools) - 1}", style=self._STYLE_MUTED)
            return text
        if self._thinking is not None:
            return self._thinking.render()
        if self._status_lines:
            text = self._motion.render()
            text.append("  ", style=self._STYLE_MUTED)
            text.append_text(self._status_lines[-1])
            return text
        return None

    def _build_status_row(self) -> Text | None:
        """The whole live frame's chrome on one row: activity, then roster.

        Everything the frame says about *state* shares this single row — it is
        the terminal's bottom status bar. Stacking it (a heading per tool, a row
        per status note, a roster line) cost up to a dozen rows and pushed the
        settled transcript off the screen it belongs on.

        Activity goes left because it is the volatile half and the half that
        must never be the one truncated; the roster already knows how to shrink
        into whatever width is left.
        """
        width = max(1, self.console.width)
        line = Text(no_wrap=True, overflow="ellipsis")
        activity = self._activity_text()
        if activity is not None:
            line.append_text(activity)
        if self._queued_turns:
            if line.cell_len:
                line.append("  ", style=self._STYLE_MUTED)
            line.append(f"+{self._queued_turns} queued", style=self._STYLE_MUTED)
        roster = self._build_team_panel(width=width - line.cell_len - 2)
        if roster is not None:
            if line.cell_len:
                line.append("  ", style=self._STYLE_MUTED)
            line.append_text(roster)
        if not line.cell_len:
            return None
        line.truncate(width, overflow="ellipsis")
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

    def _focus_band(self, aid: int) -> Any:
        """The labelled rule that opens a newly focused agent's redraw."""
        from rich.rule import Rule

        role = next(
            (
                str(entry_role)
                for entry_aid, entry_role, _state in self._team_entries()
                if entry_aid == aid
            ),
            "",
        )
        label = self._agent_label(aid)
        title = f"{label} {role}" if role and aid != 0 else label
        return Rule(Text(title, style=self._STYLE_HEADING), style=self._STYLE_MUTED)

    def _build_body(self) -> Any | None:
        """The in-flight content above the status row: the streaming answer.

        Settled blocks are not included: they already went to scrollback. Tool
        progress, status notes and the roster are not here either — they are
        chrome, and chrome lives on the one status row.
        """
        if not self._current_text:
            return None
        # Current streaming chunk — fronted by the brand ◆ gutter marker.
        return self._assistant_block(Markdown(self._current_text))

    def _build_display(self) -> Any:
        """The whole in-flight frame, uncropped: body then status row."""
        body = self._build_body()
        status = self._build_status_row()
        if body is None:
            # Nothing yet — still animate the wait so first-token latency doesn't
            # look frozen. Route the placeholder through the same pulsing dot.
            return status or self._thinking or self._new_thinking_bar("Thinking…")
        return Group(body, status) if status is not None else body

    def _build_hud(self) -> Any | None:
        """Build the frame the prompt paints, sized to its content.

        The body is capped at ``MAX_LIVE_BODY_LINES`` (and at the terminal
        height) and shows its tail when it overflows. It is never padded out to
        the terminal height: every row the HUD claims is a row the transcript
        above it loses, and the input line has to stay visible under it.

        The status row is cropped out of the body budget rather than out of the
        frame. ``None`` means the frame has nothing to say — between turns with
        no team, there is no reason to hold a row at all.
        """
        status = self._build_status_row()
        max_height = max(1, min(MAX_LIVE_BODY_LINES, self.console.height))
        if status is not None:
            max_height -= 1
        body = self._build_body()
        if body is None or max_height < 1:
            return status or self._thinking
        lines = self.console.render_lines(body, self.console.options, pad=False)
        if len(lines) > max_height:
            body = _LineViewport(lines[-max_height:], close=status is not None)
        return Group(body, status) if status is not None else body
