"""Domain event contracts.

Two distinct event families flow through the runtime:

- ``SessionRuntimeEvent``: emitted by the session run loop and its
  collaborators (text deltas, step boundaries, tool start/end as called by
  the session, loop detection, budget warnings, errors).
- ``SchedulerEvent``: emitted by scheduler orchestration (agent lifecycle,
  spawn, review loop iterations).

Both share the ``(type, data)`` shape so the event bus, autosave subscriber,
and TUI adapter fan them out through one channel.

This module must not import from outer OpenCollab layers. The dependency
rule keeps domain events at the center of the import graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class DomainEvent(Protocol):
    """Structural shape every domain event carries."""

    type: str
    data: dict[str, Any]


SessionEventType = Literal[
    "text_delta",
    "tool_start",
    "tool_end",
    "step_start",
    "step_end",
    "loop_detected",
    "budget_warning",
    "budget_reserve_allocated",
    "error",
    "user_message_appended",
]


@dataclass
class SessionRuntimeEvent:
    """Event emitted by the session run loop and its collaborators."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


SchedulerEventType = Literal[
    "agent_spawned",
    "agent_completed",
    "agent_resumed",
    "agent_failed",
    "agent_cancelled",
    "agent_message_sent",
    "agent_message_delivered",
    "agent_message_rejected_on_restore",
    "review_started",
    "review_completed",
]


@dataclass
class SchedulerEvent:
    """Event emitted by scheduler orchestration (spawn, review loop)."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "DomainEvent",
    "SessionEventType",
    "SessionRuntimeEvent",
    "SchedulerEventType",
    "SchedulerEvent",
]
