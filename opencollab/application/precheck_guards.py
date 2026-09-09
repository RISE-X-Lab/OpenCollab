"""Precheck guards — the stop conditions run before each model call.

Each guard is one ``PrecheckGuardPort`` implementation replacing one branch of
the former inline ``SessionRunUseCase.precheck``: a pure read of a
``PrecheckContext`` that answers with a ``StopDecision`` or ``None``. The runner
walks them in order and the first decision wins, so no guard needs to know
about the ones before it.

``default_precheck_guards`` gives the built-in order as two tuples, split
around the enforcement gate the runner still applies inline: the first tuple
runs before the gate, the second after it (the gate may choose the next phase
itself, in which case the second tuple is skipped).
"""

from __future__ import annotations

from opencollab.application._session_run_shared import DEFAULT_LOOP_BLOCKED_LIMIT
from opencollab.application.ports import PrecheckGuardPort
from opencollab.domain.precheck import PrecheckContext, StopDecision


class CancelGuard:
    """Stop when the caller's cancellation event is set.

    Replaces the ``cancel_event.is_set()`` branch; keeps its dedicated visible
    message instead of the derived ``[Reason. Session stopped.]`` form.
    """

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        if ctx.cancel_requested:
            return StopDecision("interrupted by user", message="[Session interrupted by user]")
        return None


class LoopBlockGuard:
    """Stop once repeated short-circuited tool calls reach ``limit``.

    Replaces the ``loop_blocked_since_progress`` branch. ``limit`` defaults to
    ``DEFAULT_LOOP_BLOCKED_LIMIT``, which stays in ``_session_run_shared``.
    """

    def __init__(self, limit: int = DEFAULT_LOOP_BLOCKED_LIMIT):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        self.limit = limit

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        if ctx.loop_blocked_since_progress >= self.limit:
            return StopDecision(f"loop block limit reached: {ctx.loop_blocked_since_progress} repeated tool calls")
        return None


class SessionBudgetGuard:
    """Stop once this session's own token spend reaches its cap.

    Replaces the ``used_tokens >= max_budget_tokens`` branch.
    """

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        if ctx.used_tokens >= ctx.max_budget_tokens:
            return StopDecision(f"budget exceeded: {ctx.used_tokens} tokens used")
        return None


class TeamBudgetGuard:
    """Stop when the team's aggregate spend has reached the global cap.

    Replaces the ``_team_budget_exhausted()`` branch. Defense in depth: even a
    session under its own cap stops here, since a single overshooting turn or
    fan-out could otherwise spend past the pool that reserve-at-allocation is
    meant to protect.
    """

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        if ctx.team_budget_exhausted:
            return StopDecision("team budget exceeded: aggregate spend reached the global cap")
        return None


class StepLimitGuard:
    """Stop once the step count reaches the session's step cap.

    Replaces the ``step_count >= max_steps`` branch. It runs after the
    enforcement gate, which is why it sits in the second tuple below.
    """

    def check(self, ctx: PrecheckContext) -> StopDecision | None:
        if ctx.step_count >= ctx.max_steps:
            return StopDecision(f"step limit reached: {ctx.step_count} steps")
        return None


def default_precheck_guards() -> tuple[tuple[PrecheckGuardPort, ...], tuple[PrecheckGuardPort, ...]]:
    """The built-in guards in their original order, split around the enforcement gate.

    Returns ``(before_enforcement, after_enforcement)``: cancel, loop block,
    session budget, and team budget run before the gate; the step limit runs
    after it.
    """
    return (
        (CancelGuard(), LoopBlockGuard(), SessionBudgetGuard(), TeamBudgetGuard()),
        (StepLimitGuard(),),
    )


__all__ = [
    "CancelGuard",
    "LoopBlockGuard",
    "SessionBudgetGuard",
    "TeamBudgetGuard",
    "StepLimitGuard",
    "default_precheck_guards",
]
