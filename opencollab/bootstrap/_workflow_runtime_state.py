"""Shared workflow-runtime limits and owned background-task registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)
DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS = 2.0
MAX_WORKFLOW_DIRECTORY_ENTRIES = 4_096
MAX_WORKFLOW_FILES = 256
MAX_WORKFLOW_SOURCE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkflowRuntimeResult:
    """Internal output plus live metrics from one hardened workflow run."""

    output: Any
    name: str
    tokens: int
    sessions: int
    steps: int
    markup_recovered: int
    agent_failures: tuple[dict[str, Any], ...]
    stop_reason: Literal["budget_exceeded"] | None = None
    evidence_complete: bool = True
