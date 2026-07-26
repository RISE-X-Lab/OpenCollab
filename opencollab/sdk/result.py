"""The single public result and infrastructure error for Python runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")


class RunError(RuntimeError):
    """Infrastructure failed, or a caller explicitly rejected a non-OK result."""

    def __init__(self, message: str, *, result: RunResult[Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult(Generic[T]):
    """Common outcome returned by agent, team, and workflow runs."""

    output: T | None
    status: Literal["completed", "stopped", "failed"]
    reason: str | None = None
    tokens: int | None = None
    artifacts: Path | None = None
    error: BaseException | None = field(default=None, repr=False, compare=False)
    metrics: dict[str, Any] = field(default_factory=dict)
    agent_failures: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the run completed normally."""
        return self.status == "completed"

    def raise_for_status(self) -> RunResult[T]:
        """Return self when completed; otherwise raise one evidence-bearing error."""
        if not self.ok:
            detail = self.reason or self.status
            raise RunError(f"run {self.status}: {detail}", result=self)
        return self


__all__ = ["RunError", "RunResult"]
