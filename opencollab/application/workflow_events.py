"""Observability events emitted by deterministic workflows."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkflowEvent:
    """Lightweight phase or log event published by a workflow context."""

    kind: str
    message: str
