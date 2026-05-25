"""Spawn agent tools bound to a scheduler port."""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.base import Tool
from opencollab.application.ports import SchedulerPort
from opencollab.application.tool_execution import ToolRuntime


class SpawnAgentTool(Tool):
    """Tool that an agent uses to spawn a child agent asynchronously.

    Returns immediately with the child agent's aid. The child runs in parallel
    and its result is injected into the parent's message history when complete.
    """

    name = "spawn_agent"
    description = (
        "Spawn a specialist agent to work on a task. You will pause until the "
        "agent finishes, then its result is delivered straight back to you as "
        "this tool call's result — so you can act on it in the same turn. Spawn "
        "several at once to run them in parallel; you resume when all are done. "
        "Available roles: analyst, coder, reviewer, or any custom name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": "The specialist role (e.g., 'coder', 'analyst', 'reviewer').",
            },
            "task": {
                "type": "string",
                "description": "Detailed task description for the agent.",
            },
            "context": {
                "type": "string",
                "description": "Optional context to inject (e.g., relevant file contents, prior analysis).",
            },
        },
        "required": ["role", "task"],
    }

    def __init__(self, scheduler: SchedulerPort):
        self._scheduler = scheduler

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> int:
        role = params["role"]
        task = params["task"]
        context = params.get("context", "")
        parent_aid = runtime.aid
        # Return the child aid so the deferral path can register a pending row
        # keyed by this tool call; the child's result fills it on completion.
        return await self._scheduler.spawn(
            parent_aid, role, task, context, tool_call_id=runtime.tool_call_id
        )


class SpawnWithReviewTool(Tool):
    """Tool for coding tasks requiring mandatory code review."""

    name = "spawn_with_review"
    description = (
        "Spawn a coding task with mandatory code review. A Coder implements the task, "
        "then a Reviewer checks the work. If the review fails, the Coder retries with "
        "feedback. Max 3 iterations. Blocks until complete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task to implement and review.",
            },
            "context": {
                "type": "string",
                "description": "Optional context (file contents, requirements, etc.).",
            },
            "max_iterations": {
                "type": "integer",
                "description": "Max review iterations (default 3).",
            },
        },
        "required": ["task"],
    }

    def __init__(self, scheduler: SchedulerPort):
        self._scheduler = scheduler

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        task = params["task"]
        context = params.get("context", "")
        max_iter = params.get("max_iterations", 3)
        parent_aid = runtime.aid
        return await self._scheduler.spawn_with_review(
            parent_aid, task, context, max_iter
        )


__all__ = ["SpawnAgentTool", "SpawnWithReviewTool"]
