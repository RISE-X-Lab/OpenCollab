"""Precheck vocabulary — what a stop condition reads and what it answers.

``PrecheckContext`` is the snapshot a session run takes of itself before each
model call: the counters and caps its stop conditions compare. ``StopDecision``
is one condition's verdict to halt. Both are plain values, so a condition can
be exercised against a literal context with no session behind it.

This module is data only — no I/O, no outer-layer imports — so it sits at the
center of the dependency graph alongside the other ``domain`` modules.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrecheckContext:
    """The quantities the precheck stop conditions read, taken once per pass.

    Field names follow the session attributes they mirror. ``cancel_requested``
    is the cancellation event's set flag; ``team_budget_exhausted`` is the
    aggregate-cap predicate already evaluated (``False`` when no team cap is
    wired). The caps travel here rather than inside a condition because both
    can be raised or lowered while the session runs.
    """

    cancel_requested: bool
    loop_blocked_since_progress: int
    used_tokens: int
    max_budget_tokens: int
    team_budget_exhausted: bool
    step_count: int
    max_steps: int


@dataclass(frozen=True)
class StopDecision:
    """A stop condition's verdict to halt the session.

    ``reason`` is the single disposition detail: it becomes the terminal reason
    and the emitted error. ``message`` overrides the visible system message the
    runner otherwise derives from ``reason``.
    """

    reason: str
    message: str | None = None


__all__ = [
    "PrecheckContext",
    "StopDecision",
]
