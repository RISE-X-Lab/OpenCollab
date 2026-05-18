"""TUI — Rich-based terminal interface with streaming and nested spinners.

Ref:
- kimi-cli: Wire pattern — bidirectional channel between agent and UI
- opencode: processor.ts events — text_delta, tool_start, tool_end, etc.
- openclaw: Rich terminal palette with dynamic updates
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.table import Table

from opencollab.core.session import SessionEvent


class TUI:
    """Terminal UI that renders SessionEvents in real-time.

    Consumes the event stream from Session (subscribed to its event bus)
    and renders streaming text, tool execution spinners, and status updates.
    """

    def __init__(self, console: Console | None = None):
        self.console = console or Console()
        self._current_text = ""
        self._active_tools: dict[str, dict] = {}
        self._status_lines: list[Text] = []
        self._timeline_blocks: list[Any] = []
        self._step = 0
        self._live: Live | None = None
        self._live_paused = False

    def event_handler(self, event: SessionEvent) -> None:
        """Synchronous event handler — subscribed to the Session event bus."""
        etype = event.type

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
            if not role and tool in ("delegate_task", "delegate_with_review") and isinstance(args, dict):
                role = str(args.get("role", ""))
            label = f"{role}:{tool}" if role else tool
            self._active_tools[label] = event.data
            preview = self._args_preview(args)
            self._append_activity(f"[cyan]{label} started[/cyan]{preview}")
            if tool == "delegate" and role:
                task = event.data.get("task", "")
                suffix = f" - {task}" if task else ""
                self._emit_status(f"[cyan]Teammate {role} started[/cyan]{suffix}")
            elif tool == "delegate_task" and role:
                self._emit_status(f"[cyan]Lead delegated to {role}[/cyan]")
            self._refresh()

        elif etype == "tool_end":
            tool = event.data.get("tool", "?")
            role = event.data.get("role", "")
            label = f"{role}:{tool}" if role else tool
            self._active_tools.pop(label, None)
            latency = event.data.get("latency", 0.0)
            self._append_activity(f"[green]{label} finished[/green] ({latency:.1f}s)")
            if tool == "delegate" and role:
                self._emit_status(f"[green]Teammate {role} finished[/green] ({latency:.1f}s)")
            self._refresh()

        elif etype == "step_start":
            self._step = event.data.get("step", 0)
            self._clear_thinking_status()
            self._emit_status(f"[dim]Lead thinking... step {self._step}[/dim]")

        elif etype == "compaction":
            self._emit_status("[dim]Context compacted[/dim]")

        elif etype == "loop_detected":
            tool = event.data.get("tool", "?")
            count = event.data.get("count", 0)
            self._emit_status(f"[yellow]Loop detected: {tool} called {count}x with same args[/yellow]")

        elif etype == "budget_warning":
            self._emit_status("[yellow]Token budget running low[/yellow]")

        elif etype == "error":
            reason = event.data.get("reason", "unknown")
            self._emit_status(f"[red]Error: {reason}[/red]")

    def _emit_status(self, message: str) -> None:
        """Route status lines to Live when active; print directly otherwise."""
        if self._live or self._live_paused:
            self._status_lines.append(Text.from_markup(message))
            self._refresh()
            return
        self.console.print(message)

    def _append_activity(self, message: str) -> None:
        """Insert one activity line at the current timeline position."""
        self._flush_current_text_to_timeline()
        line = Text.from_markup(f"[dim]•[/dim] {message}")
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
            return f" [dim]{args['command'][:80]}[/dim]"
        if "task" in args and isinstance(args["task"], str):
            return f" [dim]{args['task'][:80]}[/dim]"
        if "path" in args and isinstance(args["path"], str):
            return f" [dim]{args['path'][:80]}[/dim]"
        if "file_path" in args and isinstance(args["file_path"], str):
            return f" [dim]{args['file_path'][:80]}[/dim]"
        return ""

    def _clear_thinking_status(self) -> None:
        """Remove transient lead-thinking hint before adding fresher progress."""
        self._status_lines = [
            s for s in self._status_lines if not s.plain.startswith("Lead thinking...")
        ]

    def _refresh(self) -> None:
        """Re-render the current state."""
        if self._live and not self._live_paused:
            self._live.update(self._build_display())

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
            parts.append(Text("Running", style="bold cyan"))
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
                    ("  ", ""),
                    (f"[{label}]", "bold cyan"),
                    (args_preview, "dim"),
                )
                parts.append(spinner_text)
            parts.append(Text("", style=""))

        if self._status_lines:
            parts.extend(self._status_lines)
            parts.append(Text("", style=""))

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
        self._step = 0
