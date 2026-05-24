"""TUI — Rich-based terminal interface with streaming and nested spinners.

Ref:
- kimi-cli: Wire pattern — bidirectional channel between agent and UI
- opencode: processor.ts events — text_delta, tool_start, tool_end, etc.
- openclaw: Rich terminal palette with dynamic updates
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from opencollab.domain.events import SchedulerEvent


class TUI:
    """Terminal UI that renders runtime + scheduler events in real-time.

    Consumes the event stream from Session (subscribed to its event bus)
    and renders streaming text, tool execution spinners, and status updates.
    The single ``event_handler`` accepts both ``SessionRuntimeEvent`` and
    ``SchedulerEvent`` and dispatches to the appropriate handler so that the
    spawn/review lifecycle no longer overloads session tool events.
    """

    _STYLE_MUTED = "bright_black"
    _STYLE_ACCENT = "cyan"
    _STYLE_SUCCESS = "green"
    _STYLE_WARNING = "yellow"
    _STYLE_ERROR = "red"
    _STYLE_HEADING = "bold cyan"

    def __init__(self, console: Console | None = None):
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

    def event_handler(self, event: Any) -> None:
        """Synchronous event handler — subscribed to the Session event bus.

        Dispatches by the event class so session-runtime and scheduler-orchestration
        events stay in separate, narrow handlers without losing the single
        bus fan-out the runtime currently relies on.
        """
        if isinstance(event, SchedulerEvent):
            self._handle_scheduler_event(event)
            return
        self._handle_session_event(event)

    def _handle_session_event(self, event: Any) -> None:
        etype = event.type
        aid = event.data.get("aid", -1)
        agent_label = "Lead" if aid == 0 else f"A{aid}"

        if etype == "text_delta":
            self._clear_thinking_status()
            content = event.data.get("content", "")
            self._current_text += content
            self._refresh()

        elif etype == "tool_start":
            self._clear_thinking_status()
            tool = event.data.get("tool", "?")
            role = event.data.get("role", "")
            args = event.data.get("args", {})
            if not role and tool in ("spawn_agent", "spawn_with_review") and isinstance(args, dict):
                role = str(args.get("role", ""))
            label = f"{agent_label}:{tool}"
            self._active_tools[label] = event.data
            preview = self._args_preview(args)
            self._append_activity((f"{label} started", self._STYLE_ACCENT), (preview, self._STYLE_MUTED))
            if tool == "spawn_agent" and role:
                self._emit_status(Text(f"{agent_label} spawned {role}", style=self._STYLE_ACCENT))
            self._refresh()

        elif etype == "tool_end":
            tool = event.data.get("tool", "?")
            label = f"{agent_label}:{tool}"
            self._active_tools.pop(label, None)
            latency = event.data.get("latency", 0.0)
            self._append_activity(
                (f"{label} finished", self._STYLE_SUCCESS),
                (f" ({latency:.1f}s)", self._STYLE_MUTED),
            )
            self._refresh()

        elif etype == "step_start":
            self._step = event.data.get("step", 0)
            self._clear_thinking_status()
            self._emit_status(Text(f"{agent_label} thinking... step {self._step}", style=self._STYLE_MUTED))

        elif etype == "compaction":
            self._emit_status(Text("Context compacted", style=self._STYLE_MUTED))

        elif etype == "loop_detected":
            tool = event.data.get("tool", "?")
            count = event.data.get("count", 0)
            self._emit_status(
                Text(f"Loop detected: {tool} called {count}x with same args", style=self._STYLE_WARNING)
            )

        elif etype == "budget_warning":
            self._emit_status(Text("Token budget running low", style=self._STYLE_WARNING))

        elif etype == "error":
            reason = event.data.get("reason", "unknown")
            self._emit_status(Text(f"Error: {reason}", style=self._STYLE_ERROR))

    def _handle_scheduler_event(self, event: SchedulerEvent) -> None:
        etype = event.type
        aid = event.data.get("aid", -1)
        role = event.data.get("role", "")
        agent_label = f"A{aid}" if aid != 0 else "Lead"

        if etype == "agent_spawned":
            self._clear_thinking_status()
            label = f"{agent_label}:spawn"
            self._active_tools[label] = dict(event.data)
            self._roster[aid] = {"role": role or "agent", "state": "running"}
            self._append_activity((f"{label} started", self._STYLE_ACCENT))
            if role:
                self._emit_status(Text(f"Agent {agent_label} ({role}) spawned", style=self._STYLE_ACCENT))
            self._refresh()

        elif etype == "agent_completed":
            label = f"{agent_label}:spawn"
            self._active_tools.pop(label, None)
            self._mark_roster(aid, role, "done")
            latency = event.data.get("latency", 0.0)
            self._append_activity(
                (f"{label} finished", self._STYLE_SUCCESS),
                (f" ({latency:.1f}s)", self._STYLE_MUTED),
            )
            if role:
                self._emit_status(
                    Text(f"Agent {agent_label} ({role}) completed ({latency:.1f}s)", style=self._STYLE_SUCCESS)
                )
            self._refresh()

        elif etype == "agent_failed":
            label = f"{agent_label}:spawn"
            self._active_tools.pop(label, None)
            self._mark_roster(aid, role, "failed")
            error = event.data.get("error", "unknown")
            self._append_activity((f"{label} failed", self._STYLE_ERROR), (f": {error}", self._STYLE_MUTED))
            self._refresh()

        elif etype == "agent_cancelled":
            label = f"{agent_label}:spawn"
            self._active_tools.pop(label, None)
            self._mark_roster(aid, role, "cancelled")
            self._append_activity((f"{label} cancelled", self._STYLE_WARNING))
            self._refresh()

        elif etype == "agent_message_sent":
            from_aid = event.data.get("from_aid", -1)
            to_aid = event.data.get("to_aid", -1)
            self._append_activity((f"A{from_aid} → A{to_aid} message", self._STYLE_ACCENT))
            self._refresh()

        elif etype == "agent_message_delivered":
            to_aid = event.data.get("to_aid", -1)
            self._append_activity((f"A{to_aid} replied", self._STYLE_SUCCESS))
            self._refresh()

        elif etype == "review_started":
            self._clear_thinking_status()
            self._active_tools["review_loop"] = dict(event.data)
            self._append_activity(("review_loop started", self._STYLE_ACCENT))
            self._refresh()

        elif etype == "review_completed":
            self._active_tools.pop("review_loop", None)
            self._refresh()

    def _mark_roster(self, aid: int, role: str, state: str) -> None:
        entry = self._roster.setdefault(aid, {"role": role or "agent", "state": state})
        entry["state"] = state
        if role:
            entry["role"] = role

    def _emit_status(self, message: Text | str) -> None:
        """Route status lines to Live when active; print directly otherwise."""
        status = message if isinstance(message, Text) else Text.from_markup(message)
        if self._live or self._live_paused:
            self._status_lines.append(status)
            self._refresh()
            return
        self.console.print(status)

    def _append_activity(self, *segments: tuple[str, str]) -> None:
        """Insert one activity line at the current timeline position."""
        self._flush_current_text_to_timeline()
        styled_segments: list[tuple[str, str]] = [("• ", self._STYLE_MUTED)]
        styled_segments.extend((text, style) for text, style in segments if text)
        line = Text.assemble(*styled_segments)
        self._timeline_blocks.append(line)
        # Keep recent timeline blocks bounded.
        overflow = len(self._timeline_blocks) - 80
        if overflow > 0:
            self._timeline_blocks = self._timeline_blocks[overflow:]

    def _flush_current_text_to_timeline(self) -> None:
        """Commit accumulated assistant text so later events appear in-order."""
        if not self._current_text:
            return
        self._timeline_blocks.append(Markdown(self._current_text))
        self._current_text = ""

    def _args_preview(self, args: Any) -> str:
        """Render a short argument preview for tool activity lines."""
        if not isinstance(args, dict):
            return ""
        if "command" in args and isinstance(args["command"], str):
            return f" {args['command'][:80]}"
        if "task" in args and isinstance(args["task"], str):
            return f" {args['task'][:80]}"
        if "path" in args and isinstance(args["path"], str):
            return f" {args['path'][:80]}"
        if "file_path" in args and isinstance(args["file_path"], str):
            return f" {args['file_path'][:80]}"
        return ""

    def _clear_thinking_status(self) -> None:
        """Remove transient thinking hints before adding fresher progress."""
        self._status_lines = [
            s for s in self._status_lines if "thinking..." not in s.plain.lower()
        ]

    def _refresh(self) -> None:
        """Re-render the current state."""
        if self._live and not self._live_paused:
            self._live.update(self._build_display())

    _STATE_STYLES = {
        "running": _STYLE_WARNING,
        "done": _STYLE_SUCCESS,
        "failed": _STYLE_ERROR,
        "cancelled": _STYLE_WARNING,
    }

    def _build_team_panel(self) -> Any | None:
        """Compact roster of spawned agents, or None when no team exists."""
        if not self._roster:
            return None
        chips: list[tuple[str, str]] = []
        for aid in sorted(self._roster):
            entry = self._roster[aid]
            style = self._STATE_STYLES.get(entry["state"], self._STYLE_MUTED)
            if chips:
                chips.append(("  ", self._STYLE_MUTED))
            chips.append((f"A{aid} ", "bold cyan"))
            chips.append((f"{entry['role']} ", self._STYLE_MUTED))
            chips.append((f"[{entry['state']}]", style))
        return Text.assemble(("Team: ", self._STYLE_HEADING), *chips)

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
                args_preview = ""
                if "args" in data:
                    args = data["args"]
                    if isinstance(args, dict) and "command" in args:
                        args_preview = f" `{args['command'][:60]}`"
                    elif isinstance(args, dict) and "task" in args:
                        args_preview = f" {args['task'][:60]}"
                elif "task" in data and isinstance(data["task"], str):
                    args_preview = f" {data['task'][:60]}"

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

    def start_live(self) -> None:
        """Start the Live display context."""
        self._live_paused = False
        self._live = Live(
            Text("Thinking...", style="dim"),
            console=self.console,
            refresh_per_second=10,
            transient=False,
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
            self._build_display(),
            console=self.console,
            refresh_per_second=10,
            transient=True,
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
