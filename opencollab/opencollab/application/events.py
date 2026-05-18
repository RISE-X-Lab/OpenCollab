"""Application-layer event contracts.

Two distinct event families flow through the runtime:

- ``SessionRuntimeEvent``: emitted by the session run loop and its
  collaborators (text deltas, step boundaries, tool start/end as called by
  the session, compaction, loop detection, budget warnings, errors).
- ``TeamEvent``: emitted by team orchestration (delegation lifecycle,
  review loop iterations).

Both are deliberately kept duck-compatible with the legacy
``(type, data)`` shape so the event bus, autosave subscriber, and TUI
adapter can continue to fan them out through one channel during the
boundary migration.

This module must not import from ``opencollab.core``, ``opencollab.tools``,
``opencollab.bootstrap``, ``opencollab.cli``, ``opencollab.tui``, or
``opencollab.team`` — the dependency rule keeps application events at the
top of the import graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


SessionEventType = Literal[
    "text_delta",
    "tool_start",
    "tool_end",
    "step_start",
    "step_end",
    "compaction",
    "compaction_applied",
    "loop_detected",
    "budget_warning",
    "error",
    "user_message_appended",
]


@dataclass
class SessionRuntimeEvent:
    """Event emitted by the session run loop and its collaborators."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


TeamEventType = Literal[
    "delegation_started",
    "delegation_completed",
    "teammate_run_started",
    "teammate_run_completed",
    "review_started",
    "review_completed",
]


@dataclass
class TeamEvent:
    """Event emitted by team orchestration (delegation, review loop)."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "SessionEventType",
    "SessionRuntimeEvent",
    "TeamEventType",
    "TeamEvent",
]
