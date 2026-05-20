"""Delegation tools bound to a team delegation port."""

from __future__ import annotations

from typing import Any

from opencollab.application.ports import TeamDelegationPort
from opencollab.application.tool_runtime import ToolRuntime
from opencollab.tools.base import Tool


class DelegateTaskTool(Tool):
    """Tool that the Lead uses to delegate work to a teammate."""

    name = "delegate_task"
    description = (
        "Delegate a task to a specialist teammate. The teammate works in an isolated "
        "context and returns a summary. Available roles: analyst, coder, reviewer, "
        "or any custom name."
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
                "description": "Detailed task description for the teammate.",
            },
            "context": {
                "type": "string",
                "description": "Optional context to inject (e.g., relevant file contents, prior analysis).",
            },
        },
        "required": ["role", "task"],
    }

    def __init__(self, team: TeamDelegationPort):
        self._team = team

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        role = params["role"]
        task = params["task"]
        context = params.get("context", "")
        return await self._team.delegate(role, task, context)


class DelegateWithReviewTool(Tool):
    """Tool for tasks requiring code review."""

    name = "delegate_with_review"
    description = (
        "Delegate a coding task with mandatory code review. A Coder implements the task, "
        "then a Reviewer checks the work. If the review fails, the Coder retries with "
        "feedback. Max 3 iterations."
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

    def __init__(self, team: TeamDelegationPort):
        self._team = team

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        task = params["task"]
        context = params.get("context", "")
        max_iter = params.get("max_iterations", 3)
        return await self._team.delegate_with_review(task, context, max_iter)


__all__ = ["DelegateTaskTool", "DelegateWithReviewTool"]
