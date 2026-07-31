"""Minimal workflow-authoring surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from opencollab.application.workflow_registry import workflow
from opencollab.tools import Tool


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
        tool_choice: str | None = None,
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

    def tokens_spent(self) -> int: ...

    def tokens_remaining(self) -> float: ...

    def seconds_left(self) -> float: ...

    def time_low(self) -> bool: ...


__all__ = ["WorkflowContext", "workflow"]
