"""Inter-agent coordination tools bound to a scheduler port.

- ``message_agent``: queue an async message for an existing agent.
- ``team_status``: read the live team roster so an agent can pick a target aid.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.base import Tool
from opencollab.application.ports import SchedulerPort
from opencollab.application.tool_execution import ToolRuntime


class MessageAgentTool(Tool):
    """Queue a message for an existing agent and return immediately."""

    name = "message_agent"
    description = (
        "Send an async message to an existing agent (by its aid). The target "
        "will receive it as a user message and may reply later by messaging you "
        "back. Use team_status first to find the aid you want. You may only "
        "message agents your role is allowed to reach."
    )
    parameters = {
        "type": "object",
        "properties": {
            "to_aid": {
                "type": "integer",
                "description": "The agent id (aid) of the teammate to message.",
            },
            "summary": {
                "type": "string",
                "description": "Brief summary of the message.",
            },
            "content": {
                "type": "string",
                "description": "Full message content to send to that agent.",
            },
        },
        "required": ["to_aid", "summary", "content"],
    }

    def __init__(self, scheduler: SchedulerPort):
        self._scheduler = scheduler

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        to_aid = params["to_aid"]
        summary = params["summary"]
        content = params["content"]
        return await self._scheduler.send_message(runtime.aid, to_aid, summary, content)


def _display_team_state(entry: dict[str, Any]) -> str:
    if entry.get("busy"):
        return "busy"
    phase = entry.get("phase", "?")
    if phase == "done":
        return "idle"
    if phase == "awaiting_events":
        return "awaiting"
    return phase


class TeamStatusTool(Tool):
    """Report the current team roster (aids, roles, display states)."""

    name = "team_status"
    description = (
        "List the current team: each agent's aid, role, parent, state, and "
        "whether it is busy. Use this to discover which agents exist before "
        "messaging one."
    )
    parameters = {"type": "object", "properties": {}}

    def __init__(self, scheduler: SchedulerPort):
        self._scheduler = scheduler

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        roster = self._scheduler.team_snapshot()
        if not roster:
            return "No agents in the team yet."
        lines = ["Team roster:"]
        for entry in roster:
            state = _display_team_state(entry)
            parent = entry.get("parent_aid")
            parent_str = "root" if parent is None else f"child of {parent}"
            lines.append(
                f"- aid {entry['aid']}: {entry.get('role', '?')} ({parent_str}) — {state}"
            )
        return "\n".join(lines)


__all__ = ["MessageAgentTool", "TeamStatusTool"]
