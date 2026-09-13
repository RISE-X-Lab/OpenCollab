"""``submit`` — the agent says it is finished, instead of the run inferring it.

A turn ends today when a model returns a message with no tool calls. That rule
cannot tell three different endings apart: the work is done, the model gave up,
or the model wrote a sentence about what it was going to do next and stopped
calling tools by accident. All three come out of the loop as
``DONE(reason="completed")``, and downstream every one of them submits whatever
happens to be in the workspace.

``submit`` gives the first ending a positive act. It does not move where a run
stops: a turn with no tool call still finishes exactly as it did, so runs made
with and without this tool remain comparable. What changes is that a run which
*did* end deliberately now says so, with the agent's own account of what it is
handing over, and that account becomes the turn's answer.

Deliberately not a gate. Nothing refuses to grade a run that never submitted,
because the workspace is read either way and a rule that discarded unsubmitted
work would turn a missing sentence into a missing result.
"""

from __future__ import annotations

from typing import Any

from opencollab.adapters.tools.base import Tool
from opencollab.application.tool_execution import ToolRuntime

MAX_SUMMARY_CHARS = 4000


class SubmitTool(Tool):
    """End this agent's turn and record what it is handing over."""

    name = "submit"
    description = (
        "Declare that your work on this task is finished and end your turn. "
        "Give a short summary of what you changed and why it answers the task. "
        "Call this instead of trailing off: it records that you stopped on "
        "purpose rather than ran out of things to say. Anything you have "
        "already written to the workspace is what gets read, whether or not "
        "you call this, so summarize rather than restating your work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "What you did and why it answers the task, in a "
                "few sentences.",
            },
        },
        "required": ["summary"],
    }

    def __init__(self) -> None:
        self.turn_submitted = False
        self.submitted_summary: str | None = None

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        # Reset first: one instance serves every step of a session, so a bad
        # call after a good one must not leave the turn marked as submitted.
        self.turn_submitted = False
        self.submitted_summary = None
        summary = params.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return "Error: submit needs a summary of what you are handing over."
        if len(summary) > MAX_SUMMARY_CHARS:
            return (
                f"Error: summary is {len(summary)} characters; keep it under "
                f"{MAX_SUMMARY_CHARS}. Summarize rather than restating your work."
            )
        self.submitted_summary = summary.strip()
        self.turn_submitted = True
        return "Submitted. Your turn ends here."


__all__ = ["MAX_SUMMARY_CHARS", "SubmitTool"]
