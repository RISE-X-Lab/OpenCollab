"""Inter-agent coordination tools bound to a scheduler port.

- ``message_agent``: queue an async message for an existing agent.
- ``team_status``: read the live team roster so an agent can pick a target aid.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.base import Tool
from opencollab.application.ports import SchedulerPort
from opencollab.application.tool_execution import ToolRuntime

#: Appended to every acknowledgement. The scheduler's own ack says the message
#: was queued and nothing else, which reads like the end of an exchange -- and
#: an agent that expects an answer on this call and gets a queue receipt has no
#: way to tell "still working" from "never arrived". Both facts here are things
#: a sender has to know to decide whether to wait or to carry on.
#:
#: The third fact is the one a sender cannot find out by trying: finishing is
#: how you wait. A reply reopens a finished turn (a terminal session resumes to
#: IDLE when a message is delivered, ``domain/session.py`` PHASE_TRANSITIONS)
#: and the run is not over until nobody is running and no inbox is left
#: holding a message (``_scheduler_run._quiescent``). Without that sentence a
#: sender with nothing else to do has to guess between two wrong moves: spend
#: turns on ``team_status`` until the answer shows up, which bills a full
#: conversation to the provider each time, or finish believing the exchange
#: died with it.
_DELIVERY_NOTE = (
    "The teammate runs on its next turn, not now, and nothing about its work "
    "comes back through this call. If it answers, the answer arrives as a "
    "message in your conversation, and that reopens your turn even if you had "
    "already finished -- so if you have nothing else to do, finishing is how "
    "you wait, and it costs nothing. Carry on with whatever you can do "
    "meanwhile, or use team_status to see whether it is running."
)


class MessageAgentTool(Tool):
    """Queue a message for an existing agent and return immediately."""

    name = "message_agent"
    description = (
        "Send an async message to an existing agent, addressed either by role "
        "(to_role, e.g. \"coder\") or by agent id (to_aid). Give exactly one. "
        "The target receives it as a user message on its next turn and may "
        "reply later by messaging you back; this call returns as soon as the "
        "message is queued and carries no result. Use team_status to see who "
        "exists. You may only message agents your role is allowed to reach."
    )
    parameters = {
        "type": "object",
        "properties": {
            "to_role": {
                "type": "string",
                "description": "The role name of the teammate to message, as "
                "team_status reports it. Use this or to_aid, not both.",
            },
            "to_aid": {
                "type": "integer",
                "description": "The agent id (aid) of the teammate to message. "
                "Use this or to_role, not both.",
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
        "required": ["summary", "content"],
    }

    def __init__(self, scheduler: SchedulerPort):
        self._scheduler = scheduler

    def _resolve_target(self, params: dict[str, Any]) -> int | str:
        """The aid to send to, or an error string naming what went wrong.

        Addressing by role exists because addressing by aid alone cost a turn:
        an aid is not knowable from the role card, so every first message had to
        be preceded by a team_status call. On a team whose roles are declared
        before the run, the role name is the stable name and the aid is an
        implementation detail of this particular run.
        """
        to_aid = params.get("to_aid")
        to_role = params.get("to_role")
        if (to_aid is None) == (to_role is None):
            return "Error: give exactly one of to_role or to_aid."
        if to_aid is not None:
            if isinstance(to_aid, bool) or not isinstance(to_aid, int):
                return "Error: to_aid must be an integer agent id."
            return to_aid
        if not isinstance(to_role, str) or not to_role.strip():
            return "Error: to_role must be a role name."
        wanted = to_role.strip().lower()
        matches = [
            entry
            for entry in self._scheduler.team_snapshot()
            if str(entry.get("role", "")).lower() == wanted
        ]
        if not matches:
            known = sorted(
                {str(entry.get("role", "?")) for entry in self._scheduler.team_snapshot()}
            )
            return (
                f"Error: no agent has the role {to_role!r}. "
                f"Roles on this team: {', '.join(known) or '(none)'}."
            )
        if len(matches) > 1:
            aids = ", ".join(str(entry["aid"]) for entry in matches)
            return (
                f"Error: {len(matches)} agents have the role {to_role!r} "
                f"(aids {aids}). Address one of them with to_aid."
            )
        return int(matches[0]["aid"])

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        target = self._resolve_target(params)
        if isinstance(target, str):
            return target
        summary = params["summary"]
        content = params["content"]
        ack = await self._scheduler.send_message(
            runtime.aid, target, summary, content
        )
        if ack.startswith("Error"):
            return ack
        return f"{ack} {_DELIVERY_NOTE}"


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
