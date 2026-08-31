"""Minimal workflow-authoring surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from opencollab.application.workflow_candidates import CandidateRun
from opencollab.application.workflow_registry import workflow
from opencollab.tools import Tool, VerificationTool


class WorkflowContext(Protocol):
    """Stable subset of context operations intended for workflow authors."""

    workspace_root: str | None

    async def agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        tools: Sequence[Tool] | None = None,
        budget: int | None = None,
        timeout: float | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
        over_budget_ok: bool = False,
    ) -> str | dict[str, Any] | None: ...

    async def draft_findings(
        self,
        prompt: str,
        *,
        label: str | None = None,
        budget: int | None = None,
    ) -> dict[str, Any] | None:
        """Capture a structured evidence draft before exploratory workflow steps."""
        ...

    async def parallel(
        self,
        thunks: Sequence[Callable[[], Awaitable[Any]]],
    ) -> list[Any]: ...

    async def phase(self, title: str) -> None: ...

    async def log(self, message: str) -> None: ...

    async def source_changed(
        self,
        exclude_paths: Sequence[str] = (),
    ) -> bool | None: ...

    async def diff(self) -> str | None: ...

    async def execute_verification(
        self,
        tool: VerificationTool,
        params: Mapping[str, object],
    ) -> str: ...

    async def candidate_agent(
        self,
        prompt: str,
        *,
        label: str,
        tools: Sequence[Tool] | None = None,
        budget: int | None = None,
        timeout: float | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
    ) -> CandidateRun: ...

    async def candidate_workflow(
        self,
        workflow_fn: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        args: dict[str, Any],
        *,
        label: str,
        budget: int | None = None,
    ) -> CandidateRun: ...

    async def adopt_candidate(
        self,
        candidate: CandidateRun,
        *,
        preserve_paths: Sequence[str] = (),
    ) -> None: ...

    def tokens_spent(self) -> int: ...

    def tokens_remaining(self) -> float: ...

    def seconds_left(self) -> float: ...

    def time_low(self) -> bool: ...


__all__ = ["CandidateRun", "WorkflowContext", "workflow"]
