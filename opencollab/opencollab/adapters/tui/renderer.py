"""TUI — Rich-based terminal interface with streaming and nested spinners.

Ref:
- kimi-cli: Wire pattern — bidirectional channel between agent and UI
- opencode: processor.ts events — text_delta, tool_start, tool_end, etc.
- openclaw: Rich terminal palette with dynamic updates

Split by concern (``self`` is unchanged — mixins run on the one TUI instance):

- ``renderer_events``  — event dispatch + timeline/status/roster updates
- ``renderer_display`` — Rich renderable building + style palette
- this module          — ``TUI`` state, Live lifecycle, and turn-level API
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from opencollab.adapters.tui.renderer_display import _RendererDisplayMixin
from opencollab.adapters.tui.renderer_events import _RendererEventsMixin


class TUI(_RendererEventsMixin, _RendererDisplayMixin):
    """Terminal UI that renders runtime + scheduler events in real-time.

    Consumes the event stream from Session (subscribed to its event bus)
    and renders streaming text, tool execution spinners, and status updates.
    The single ``event_handler`` accepts both ``SessionRuntimeEvent`` and
    ``SchedulerEvent`` and dispatches to the appropriate handler so that the
    spawn/review lifecycle no longer overloads session tool events.
    """

    def __init__(self, console: Console | None = None, *, filter_messages: bool = False):
        self.console = console or Console()
        self._current_text = ""
        self._active_tools: dict[str, dict] = {}
        self._status_lines: list[Text] = []
        self._timeline_blocks: list[Any] = []
        # aid -> {"role": str, "state": str} for the live team roster panel.
        self._roster: dict[int, dict] = {}
        self._step = 0
        self._live: Live | None = None
        self._live_paused = False
        # When filtering is on, only the selected agent's text stream is shown.
        # Tool/status events stay visible for every agent so background work
        # does not look frozen while a teammate is running.
        # Defaults to the Lead (aid 0).
        self._filter_messages = filter_messages
        self._selected_aid = 0
        # Optional callable returning the full team roster (live agents +
        # configured "available" roles). When set, the team panel renders from
        # it so the roster stays visible during a turn, not only after a spawn.
        self._team_provider: Any | None = None

    def set_team_provider(self, provider: Any) -> None:
        """Supply a callable returning the full team roster so the live display
        shows the team continuously (matching the prompt's bottom toolbar)."""
        self._team_provider = provider

    def _refresh(self) -> None:
        """Re-render the current state."""
        if self._live and not self._live_paused:
            self._live.update(self._build_live_display())

    def start_live(self) -> None:
        """Start the Live display context."""
        self._live_paused = False
        self._live = Live(
            self._build_live_display(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
            vertical_overflow="crop",
        )
        self._live.start()

    def suspend_live(self) -> bool:
        """Temporarily stop Live updates while preserving render state."""
        if not self._live or self._live_paused:
            return False
        self._live.stop()
        self._live = None
        self._live_paused = True
        return True

    def resume_live(self, was_suspended: bool) -> None:
        """Resume Live after suspend_live was used."""
        if not was_suspended or not self._live_paused:
            return
        self._live = Live(
            self._build_live_display(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
            vertical_overflow="crop",
        )
        self._live_paused = False
        self._live.start()

    def stop_live(self) -> None:
        """Stop the Live display and print final output."""
        if self._live:
            # Keep the final live frame on screen and avoid duplicate manual prints.
            self._refresh()
            self._live.stop()
            self._live = None
        self._live_paused = False

        self._status_lines.clear()
        self._current_text = ""

    def print_welcome(self) -> None:
        self.console.print(Panel.fit(
            "[bold]OpenCollab[/bold] — Mini Multi-Agent Collaboration Framework\n"
            "[dim]Type your message. Ctrl+C to interrupt. 'exit' to quit.[/dim]",
            border_style="blue",
        ))

    def print_stats(self, tokens: int, steps: int) -> None:
        self.console.print(f"\n[dim]({tokens:,} tokens, {steps} steps)[/dim]")

    def reset(self) -> None:
        """Reset state for next turn."""
        self._current_text = ""
        self._timeline_blocks.clear()
        self._active_tools.clear()
        self._status_lines.clear()
        self._roster.clear()
        self._step = 0
