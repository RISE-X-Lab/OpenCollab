"""Ask-User Tool — human-in-the-loop as a regular Tool.

First principle: human is not a special framework interrupt mechanism,
but merely another Tool the agent can call.  The run_loop state machine
needs zero changes.

Ref:
- kimi-cli: tools/ask_user — async wire-based question, yolo auto-dismiss
- claude-code: AskUserQuestion tool
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.application.tool_runtime import ToolRuntime
from opencollab.adapters.tools.base import Tool


class AskUserTool(Tool):
    """Ask the human user a question. Use only when stuck or ambiguous."""

    name = "ask_user"
    description = (
        "Ask the user a question. ONLY use when: (1) the requirement is fundamentally "
        "ambiguous and you cannot proceed without clarification, or (2) you need "
        "explicit permission for a destructive action. NEVER use for problems you "
        "can solve by reading code or trying different approaches."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user. Be specific and concise.",
            },
        },
        "required": ["question"],
    }

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        question = params["question"]
        confirm_fn = runtime.confirm_fn()

        # Non-interactive mode: auto-dismiss (ref: kimi-cli yolo fallback)
        if confirm_fn is None:
            return (
                "Running in non-interactive mode. "
                "Make your own best judgment and proceed."
            )

        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(None, _prompt_user, question)
        except (EOFError, KeyboardInterrupt):
            return "User declined to answer."

        return answer


def _prompt_user(question: str) -> str:
    """Synchronous prompt — runs in executor thread."""
    try:
        from rich.prompt import Prompt
        return Prompt.ask(f"\n[bold yellow][Agent asks][/bold yellow] {question}")
    except ImportError:
        return input(f"\n[Agent asks] {question}\n> ")
