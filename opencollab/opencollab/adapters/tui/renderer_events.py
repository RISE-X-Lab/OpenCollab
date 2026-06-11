"""Event handling for the TUI: session-runtime + scheduler event dispatch.

Mixed into ``renderer.TUI`` — methods run on the TUI instance and feed the
timeline/status/roster state that ``renderer_display`` renders.
"""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.text import Text

from opencollab.domain.events import SchedulerEvent

# Cap on retained timeline blocks so long sessions don't grow render cost.
MAX_TIMELINE_BLOCKS = 80


class _RendererEventsMixin:
    """Consumes runtime/scheduler events and updates the TUI's render state."""

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
            if not self._is_visible(aid):
                return
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
            preview = self._args_preview(event.data)
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

        elif etype == "agent_resumed":
            # A parent that suspended on delegated work has been re-activated.
            self._mark_roster(aid, role, "running")
            self._append_activity((f"{agent_label} resumed", self._STYLE_ACCENT))
            self._refresh()

        elif etype == "agent_completed":
            label = f"{agent_label}:spawn"
            self._active_tools.pop(label, None)
            self._mark_roster(aid, role, "idle")
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
            self._append_activity((f"A{to_aid} received message", self._STYLE_SUCCESS))
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
        overflow = len(self._timeline_blocks) - MAX_TIMELINE_BLOCKS
        if overflow > 0:
            self._timeline_blocks = self._timeline_blocks[overflow:]

    def _flush_current_text_to_timeline(self) -> None:
        """Commit accumulated assistant text so later events appear in-order."""
        if not self._current_text:
            return
        self._timeline_blocks.append(Markdown(self._current_text))
        self._current_text = ""

    def _clear_thinking_status(self) -> None:
        """Remove transient thinking hints before adding fresher progress."""
        self._status_lines = [
            s for s in self._status_lines if "thinking..." not in s.plain.lower()
        ]
