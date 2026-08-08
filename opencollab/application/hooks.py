"""HookEventSubscriber — bridges the runtime event bus to the hook runner.

Subscribed to the team-level ``EventBus``, it sees every agent's session-runtime
events (forwarded up from per-session buses) and the scheduler's lifecycle
events. It translates the internal event vocabulary into the CC-style hook
event names the user configures, builds a payload, and calls the ``HookPort``.

Observe-only: the runner's ``HookOutcome`` is ignored here (bus delivery is
fire-and-forget). Blocking hooks live on the synchronous tool-execution path,
not the bus — see the phase-2 note in the plan.
"""

from __future__ import annotations

from typing import Any

from opencollab.application.ports import EventPublisherPort, HookPort

# Internal event ``type`` -> CC hook event name. ``agent_completed`` is split by
# parent_aid at dispatch time (lead vs. child), so it is handled separately.
_DIRECT_EVENT_MAP: dict[str, str] = {
    "tool_start": "PreToolUse",
    "tool_end": "PostToolUse",
    "agent_spawned": "SessionStart",
    "error": "Notification",
}
_TERMINAL_DISPOSITIONS = {
    "agent_completed": "completed",
    "agent_failed": "failed",
    "agent_cancelled": "cancelled",
}


class HookEventSubscriber(EventPublisherPort):
    def __init__(self, runner: HookPort):
        self._runner = runner

    async def emit(self, event: Any) -> None:
        hook_event = self._hook_event_for(event)
        if hook_event is None:
            return
        payload = {"hook_event_name": hook_event, **dict(event.data)}
        disposition = _TERMINAL_DISPOSITIONS.get(event.type)
        if disposition is not None:
            payload["disposition"] = disposition
        await self._runner.fire(hook_event, payload)

    def _hook_event_for(self, event: Any) -> str | None:
        if event.type == "agent_completed":
            # parent_aid is None only for agent 0 (the lead): that completion is
            # the whole team stopping; a child completing is a SubagentStop.
            return "Stop" if event.data.get("parent_aid") is None else "SubagentStop"
        if event.type in {"agent_failed", "agent_cancelled"}:
            return "Stop" if event.data.get("aid") == 0 else "SubagentStop"
        return _DIRECT_EVENT_MAP.get(event.type)


__all__ = ["HookEventSubscriber"]
