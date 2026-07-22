"""Minimal workflow-authoring surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from opencollab.application.workflow_registry import workflow


class WorkflowContext(Protocol):
    """Stable subset of context operations intended for workflow authors."""

    workspace_root: str | None

    async def agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        tools: Sequence[Any] | None = None,
        budget: int | None = None,
    ) -> str | dict[str, Any] | None: ...

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

    def seconds_left(self) -> float: ...

    def time_low(self) -> bool: ...


__all__ = ["WorkflowContext", "workflow"]
