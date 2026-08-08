"""Self-collaboration: the sequential coder -> reviewer loop.

Extracted from ``Scheduler`` so the long, self-contained review-iteration logic
does not bloat the scheduler's core. It drives the loop through the slice of the
scheduler's public surface typed by ``ReviewLoopScheduler`` (``spawn``,
``wait_until_terminal``, ``table``, ``events``, ``emit_scheduler_event``), so it
takes the scheduler as a parameter rather than being a method.
"""

from __future__ import annotations

import logging
from typing import Protocol

from opencollab.application.events import SchedulerEventFactory
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.scheduler import ReviewVerdict, SessionTable
from opencollab.domain.session import SessionPhase

logger = logging.getLogger(__name__)
MAX_REVIEW_ITERATIONS = 3


def validate_review_iterations(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_REVIEW_ITERATIONS
    ):
        raise ValueError(
            f"max_iterations must be an integer in 1..{MAX_REVIEW_ITERATIONS}"
        )
    return value


class ReviewLoopScheduler(Protocol):
    """The slice of the Scheduler surface the review loop drives."""

    table: SessionTable
    events: SchedulerEventFactory

    async def spawn(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str = "",
    ) -> int:
        ...

    async def wait_until_terminal(self, aid: int) -> None:
        ...

    async def emit_scheduler_event(self, event: SchedulerEvent) -> None:
        ...


async def _emit_review_event(
    scheduler: ReviewLoopScheduler, event: SchedulerEvent
) -> None:
    """Publish review telemetry without making it part of loop correctness."""
    safe_emit = getattr(scheduler, "_safe_emit_scheduler_event", None)
    if callable(safe_emit):
        await safe_emit(event)
        return
    try:
        await scheduler.emit_scheduler_event(event)
    except Exception as exc:
        logger.error("review event %s failed: %s", event.type, exc)


def _format_review_failure(
    *,
    iteration: int,
    stage: str,
    artifact: str,
    feedback: str = "",
    reason: str = "",
    coder_phase: str | None = None,
    coder_reason: str | None = None,
) -> str:
    sections = [
        f"[Self-Collaboration: FAILED after {iteration} iteration(s)]",
        f"Failure stage: {stage}",
    ]
    if reason:
        sections.append(f"Failure reason: {reason}")
    if coder_phase is not None:
        sections.extend(
            (
                f"Coder terminal phase: {coder_phase}",
                f"Coder terminal reason: {coder_reason or 'none'}",
            )
        )
    sections.append(f"Last implementation:\n{artifact}")
    if feedback:
        sections.append(f"Last reviewer feedback:\n{feedback}")
    return "\n\n".join(sections)


async def run_spawn_with_review(
    scheduler: ReviewLoopScheduler,
    parent_aid: int,
    task: str,
    context: str = "",
    max_iterations: int = 3,
) -> str:
    """Run a sequential coder -> reviewer loop and return the final result.

    Each iteration spawns a fresh ``coder`` agent with the current task and
    waits for it, then spawns a ``reviewer`` agent over the coder's result and
    parses a ``VERDICT: PASS``/``FAIL`` line from its reply. PASS returns the
    coder's result; FAIL folds the reviewer's feedback into the next
    iteration's task. After ``max_iterations`` failed reviews the last
    implementation is returned, marked as failed. ``review_started`` /
    ``review_completed`` scheduler events bracket each iteration.
    """
    max_iterations = validate_review_iterations(max_iterations)
    current_task = task
    last_result = ""
    last_feedback = ""

    for iteration in range(1, max_iterations + 1):
        await _emit_review_event(
            scheduler,
            scheduler.events.review_started(iteration, max_iterations)
        )

        # Spawn coder and wait
        try:
            coder_aid = await scheduler.spawn(
                parent_aid,
                "coder",
                current_task,
                context,
            )
        except Exception as exc:
            await _emit_review_event(
                scheduler,
                scheduler.events.review_completed(iteration, False),
            )
            return _format_review_failure(
                iteration=iteration,
                stage="coder_spawn",
                artifact=last_result,
                feedback=last_feedback,
                reason=f"{type(exc).__name__}: {exc}",
            )
        try:
            await scheduler.wait_until_terminal(coder_aid)
        except Exception as exc:
            coder_scb = scheduler.table.get(coder_aid)
            code_result = (coder_scb.result if coder_scb else "") or last_result
            await _emit_review_event(
                scheduler,
                scheduler.events.review_completed(iteration, False),
            )
            return _format_review_failure(
                iteration=iteration,
                stage="coder_wait",
                artifact=code_result,
                feedback=last_feedback,
                reason=f"{type(exc).__name__}: {exc}",
            )
        coder_scb = scheduler.table.get(coder_aid)
        code_result = (coder_scb.result if coder_scb else "") or last_result
        if coder_scb is None or coder_scb.state.phase is not SessionPhase.DONE:
            phase = "missing" if coder_scb is None else coder_scb.state.phase.value
            reason = None if coder_scb is None else coder_scb.state.terminal_reason
            await _emit_review_event(
                scheduler,
                scheduler.events.review_completed(iteration, False),
            )
            return _format_review_failure(
                iteration=iteration,
                stage="coder_terminal",
                artifact=code_result,
                feedback=last_feedback,
                coder_phase=phase,
                coder_reason=reason,
            )
        last_result = code_result

        # Spawn reviewer and wait
        review_prompt = (
            "Review the artifact against the original task and required context.\n\n"
            f"Original task:\n{task}\n\n"
            f"Required context:\n{context or '(none provided)'}\n\n"
            f"Artifact to review:\n{code_result}\n\n"
            f"Your response MUST end with a verdict line in exactly this format:\n"
            f"VERDICT: PASS\n"
            f"or\n"
            f"VERDICT: FAIL\n\n"
            f"If FAIL, provide detailed fix instructions before the verdict line."
        )
        try:
            reviewer_aid = await scheduler.spawn(
                parent_aid,
                "reviewer",
                review_prompt,
                context,
            )
        except Exception as exc:
            await _emit_review_event(
                scheduler,
                scheduler.events.review_completed(iteration, False),
            )
            return _format_review_failure(
                iteration=iteration,
                stage="reviewer_spawn",
                artifact=last_result,
                feedback=last_feedback,
                reason=f"{type(exc).__name__}: {exc}",
            )
        try:
            await scheduler.wait_until_terminal(reviewer_aid)
        except Exception as exc:
            reviewer_scb = scheduler.table.get(reviewer_aid)
            review_result = reviewer_scb.result if reviewer_scb else ""
            await _emit_review_event(
                scheduler,
                scheduler.events.review_completed(iteration, False),
            )
            return _format_review_failure(
                iteration=iteration,
                stage="reviewer_wait",
                artifact=last_result,
                feedback=review_result or last_feedback,
                reason=f"{type(exc).__name__}: {exc}",
            )
        reviewer_scb = scheduler.table.get(reviewer_aid)
        review_result = reviewer_scb.result if reviewer_scb else ""
        last_feedback = review_result

        verdict = ReviewVerdict.parse(review_result)
        reviewer_completed = (
            reviewer_scb is not None
            and reviewer_scb.state.phase is SessionPhase.DONE
        )
        passed = reviewer_completed and verdict.passed
        await _emit_review_event(
            scheduler, scheduler.events.review_completed(iteration, passed)
        )

        if passed:
            return (
                f"[Self-Collaboration: PASSED after {iteration} iteration(s)]\n\n"
                f"{code_result}"
            )

        current_task = (
            f"Your previous implementation failed review (iteration {iteration}/{max_iterations}).\n"
            f"Original task: {task}\n\n"
            f"Previous implementation artifact (including any worktree diff):\n"
            f"{code_result}\n\n"
            f"Reviewer feedback:\n{review_result}\n\n"
            f"Reapply the previous implementation in your fresh worktree, preserve its "
            f"correct parts, and fix the issues identified by the reviewer."
        )
    return _format_review_failure(
        iteration=max_iterations,
        stage="review_verdict",
        artifact=last_result,
        feedback=last_feedback,
        reason="reviewer did not return a passing verdict",
    )


__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "ReviewLoopScheduler",
    "run_spawn_with_review",
    "validate_review_iterations",
]
