"""Event handling for the TUI: session-runtime + scheduler event dispatch.

Mixed into ``renderer.TUI`` — methods run on the TUI instance and feed the
history/status/roster state that ``renderer_display`` renders. A block that
settles here is handed straight to scrollback for the focused agent.
"""

from __future__ import annotations

from typing import Any

from rich.markdown import Markdown
from rich.text import Text

from opencollab.adapters.tui.renderer_display import collapse_text_rows
from opencollab.domain.events import SchedulerEvent

MAX_STATUS_LINES = 40


class _RendererEventsMixin:
    """Consumes runtime/scheduler events and updates the TUI's render state."""

    @staticmethod
    def _tool_start_key(state: Any, label: str, data: dict[str, Any]) -> str:
        tool_call_id = data.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return f"{label}#{tool_call_id}"
        prefix = f"{label}#legacy-"
        has_legacy = any(key.startswith(prefix) for key in state.active_tools)
        if label not in state.active_tools and not has_legacy:
            return label

        if label in state.active_tools:
            existing = state.active_tools.pop(label)
            state.active_tools[f"{label}#legacy-1"] = existing
        sequence = 1
        while f"{prefix}{sequence}" in state.active_tools:
            sequence += 1
        return f"{prefix}{sequence}"

    @staticmethod
    def _tool_end_key(state: Any, label: str, data: dict[str, Any]) -> str | None:
        tool_call_id = data.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return f"{label}#{tool_call_id}"
        if label in state.active_tools:
            return label
        prefix = f"{label}#legacy-"
        return next(
            (key for key in state.active_tools if key.startswith(prefix)),
            None,
        )

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
        aid = self._event_aid(event.data.get("aid", -1))
        state = self._state_for(aid)
        agent_label = self._agent_label(aid)

        if etype == "text_delta":
            self._clear_thinking_status(state)
            state.current_text += event.data.get("content", "")
            self._refresh()

        elif etype == "tool_start":
            self._clear_thinking_status(state)
            tool = event.data.get("tool", "?")
            role = event.data.get("role", "")
            args = event.data.get("args", {})
            if not role and tool == "spawn_agent" and isinstance(args, dict):
                role = str(args.get("role", ""))
            label = f"{agent_label}:{tool}"
            key = self._tool_start_key(state, label, event.data)
            state.active_tools[key] = {**event.data, "_display_label": label}
            preview = self._args_preview(event.data)
            self._append_activity(
                (f"{label} started", self._STYLE_ACCENT),
                (preview, self._STYLE_MUTED),
                marker=self._MARK_START,
                state=state,
            )
            if tool == "spawn_agent" and role:
                self._emit_status(
                    Text(f"{agent_label} spawned {role}", style=self._STYLE_ACCENT),
                    state=state,
                )
            self._refresh()

        elif etype == "tool_end":
            tool = event.data.get("tool", "?")
            label = f"{agent_label}:{tool}"
            key = self._tool_end_key(state, label, event.data)
            if key is not None:
                state.active_tools.pop(key, None)
            latency = event.data.get("latency", 0.0)
            self._append_activity(
                (f"{label} finished", self._STYLE_SUCCESS),
                (f" ({latency:.1f}s)", self._STYLE_MUTED),
                marker=self._MARK_DONE,
                state=state,
            )
            self._refresh()

        elif etype == "step_start":
            state.step = event.data.get("step", 0)
            self._clear_thinking_status(state)
            # Drive the live view with the animated pulsing dot. A fresh bar
            # resets the elapsed counter for this step; it keeps flowing on Live's
            # refresh even while the model is silent, so it never looks frozen.
            state.thinking = self._new_thinking_bar(
                f"{agent_label} thinking… · step {state.step}"
            )
            self._refresh()

        elif etype == "loop_detected":
            tool = event.data.get("tool", "?")
            count = event.data.get("count", 0)
            # Preserved: with the live chrome down to one shared row, a warning
            # that only lived there would be overwritten by the next tool.
            self._emit_status(
                Text(f"Loop detected: {tool} called {count}x with same args", style=self._STYLE_WARNING),
                state=state,
                preserve=True,
            )

        elif etype == "budget_warning":
            self._emit_status(
                Text("Token budget running low", style=self._STYLE_WARNING),
                state=state,
                preserve=True,
            )

        elif etype == "error":
            reason = event.data.get("reason", "unknown")
            self._emit_status(
                Text(f"Error: {reason}", style=self._STYLE_ERROR),
                state=state,
                preserve=True,
            )

    def _handle_scheduler_event(self, event: SchedulerEvent) -> None:
        etype = event.type
        aid = self._event_aid(event.data.get("aid", -1))
        role = event.data.get("role", "")
        agent_label = self._agent_label(aid)

        if etype == "agent_spawned":
            state = self._state_for(aid)
            self._clear_thinking_status(state)
            self._mark_roster(aid, role, "running")
            # aid 0 is the lead/turn itself, not a spawned teammate — no chrome.
            if aid != 0:
                label = f"{agent_label}:spawn"
                state.active_tools[label] = dict(event.data)
                self._append_activity(
                    (f"{label} started", self._STYLE_ACCENT),
                    marker=self._MARK_START,
                    state=state,
                )
                if role:
                    self._emit_status(
                        Text(f"Agent {agent_label} ({role}) spawned", style=self._STYLE_ACCENT),
                        state=state,
                    )
            self._refresh()

        elif etype == "agent_resumed":
            state = self._state_for(aid)
            # A parent that suspended on delegated work has been re-activated.
            self._mark_roster(aid, role, "running")
            self._append_activity(
                (f"{agent_label} resumed", self._STYLE_ACCENT),
                marker=self._MARK_START,
                state=state,
            )
            self._refresh()

        elif etype == "agent_completed":
            state = self._state_for(aid)
            label = f"{agent_label}:spawn"
            completed_spawn = label in state.active_tools
            state.active_tools.pop(label, None)
            self._mark_roster(aid, role, "idle")
            latency = event.data.get("latency", 0.0)
            # The lead (aid 0) finishing IS the turn boundary — the stats footer
            # already marks it. Skip the teammate-style "finished"/"completed"
            # chrome so a solo turn isn't buried under its own lifecycle noise.
            if aid != 0:
                activity = f"{label} finished" if completed_spawn else f"{agent_label} completed"
                self._append_activity(
                    (activity, self._STYLE_SUCCESS),
                    (f" ({latency:.1f}s)", self._STYLE_MUTED),
                    marker=self._MARK_DONE,
                    state=state,
                )
                if role:
                    self._emit_status(
                        Text(
                            f"Agent {agent_label} ({role}) completed ({latency:.1f}s)",
                            style=self._STYLE_SUCCESS,
                        ),
                        state=state,
                    )
            self._refresh()

        elif etype == "agent_failed":
            state = self._state_for(aid)
            label = f"{agent_label}:spawn"
            failed_spawn = label in state.active_tools
            state.active_tools.pop(label, None)
            self._mark_roster(aid, role, "failed")
            error = event.data.get("error", "unknown")
            activity = f"{label} failed" if failed_spawn else f"{agent_label} failed"
            self._append_activity(
                (activity, self._STYLE_ERROR),
                (f": {error}", self._STYLE_MUTED),
                marker=self._MARK_FAIL,
                state=state,
            )
            self._refresh()

        elif etype == "agent_cancelled":
            state = self._state_for(aid)
            label = f"{agent_label}:spawn"
            cancelled_spawn = label in state.active_tools
            state.active_tools.pop(label, None)
            self._mark_roster(aid, role, "cancelled")
            activity = f"{label} cancelled" if cancelled_spawn else f"{agent_label} cancelled"
            self._append_activity(
                (activity, self._STYLE_WARNING),
                marker=self._MARK_WARN,
                state=state,
            )
            self._refresh()

        elif etype == "agent_message_sent":
            from_aid = event.data.get("from_aid", -1)
            to_aid = event.data.get("to_aid", -1)
            state = self._state_for(self._event_aid(from_aid))
            self._append_activity(
                (f"A{from_aid} → A{to_aid} message", self._STYLE_ACCENT),
                marker=self._MARK_MSG,
                state=state,
            )
            self._refresh()

        elif etype == "agent_message_delivered":
            to_aid = event.data.get("to_aid", -1)
            state = self._state_for(self._event_aid(to_aid))
            self._append_activity(
                (f"A{to_aid} received message", self._STYLE_SUCCESS),
                marker=self._MARK_DONE,
                state=state,
            )
            self._refresh()

        elif etype == "review_started":
            state = self._state_for(0)
            self._clear_thinking_status(state)
            state.active_tools["review_loop"] = dict(event.data)
            self._append_activity(
                ("review_loop started", self._STYLE_ACCENT),
                marker=self._MARK_START,
                state=state,
            )
            self._refresh()

        elif etype == "review_completed":
            self._state_for(0).active_tools.pop("review_loop", None)
            self._refresh()

    def _mark_roster(self, aid: int, role: str, state: str) -> None:
        entry = self._roster.setdefault(aid, {"role": role or "agent", "state": state})
        entry["state"] = state
        if role:
            entry["role"] = role
        self._track_agent_render_lifecycle(aid, state)

    def _emit_status(
        self,
        message: Text | str,
        *,
        state: Any | None = None,
        preserve: bool = False,
    ) -> None:
        """Route status lines to Live when active; print directly otherwise."""
        status = message if isinstance(message, Text) else Text.from_markup(message)
        target = state or self._selected_state
        target_aid = self._aid_of(target)
        if preserve:
            self._flush_current_text_to_timeline(target)
            self._append_history_block(target, status)
            self._drain_pending(target_aid)
        if self._live or self._live_paused:
            # The status row is one row. The preserved copy above keeps the
            # message whole in scrollback, so nothing is lost by folding it.
            target.status_lines.append(collapse_text_rows(status))
            overflow = len(target.status_lines) - MAX_STATUS_LINES
            if overflow > 0:
                del target.status_lines[:overflow]
            self._refresh()
            return
        # No live frame to hold a transient line. Printing it is the only way to
        # show it at all — but it still obeys focus, and a preserved line has
        # already been printed as part of the transcript.
        if not preserve and target_aid == self._selected_aid:
            self.console.print(status)

    def _append_activity(
        self,
        *segments: tuple[str, str],
        marker: tuple[str, str] | None = None,
        state: Any | None = None,
    ) -> None:
        """Insert one activity line; ``marker`` is the leading geometric glyph
        (defaults to the neutral dot)."""
        target = state or self._selected_state
        self._flush_current_text_to_timeline(target)
        styled_segments: list[tuple[str, str]] = [marker or self._MARK_DOT]
        styled_segments.extend((text, style) for text, style in segments if text)
        self._append_history_block(target, Text.assemble(*styled_segments))
        self._drain_pending(self._aid_of(target))

    def _flush_current_text_to_timeline(self, state: Any | None = None) -> None:
        """Commit accumulated assistant text so later events appear in-order."""
        target = state or self._selected_state
        if not target.current_text:
            return
        block = self._assistant_block(Markdown(target.current_text))
        target.current_text = ""
        self._append_history_block(target, block)
        self._drain_pending(self._aid_of(target))

    def _aid_of(self, state: Any) -> int:
        """Reverse-lookup an agent id from its render state.

        Event handlers thread the state object, not the aid, and only the
        focused agent's blocks may be printed — so the printer needs the aid
        back. An unregistered state yields ``-1``, which can never be focused.
        """
        return next(
            (aid for aid, candidate in self._agent_states.items() if candidate is state),
            -1,
        )

    def _clear_thinking_status(self, state: Any | None = None) -> None:
        """Drop the transient LLM-wait indicator before fresher progress shows."""
        target = state or self._selected_state
        target.thinking = None
        target.status_lines = [
            s for s in target.status_lines if "thinking" not in s.plain.lower()
        ]
