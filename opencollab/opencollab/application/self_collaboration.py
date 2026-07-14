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

    for iteration in range(1, max_iterations + 1):
        await _emit_review_event(
            scheduler,
            scheduler.events.review_started(iteration, max_iterations)
        )

        # Spawn coder and wait
        coder_aid = await scheduler.spawn(parent_aid, "coder", current_task, context)
        await scheduler.wait_until_terminal(coder_aid)
        coder_scb = scheduler.table.get(coder_aid)
        code_result = coder_scb.result if coder_scb else ""

        # Spawn reviewer and wait
        review_prompt = (
            f"Review the following implementation for task: '{task}'\n\n"
            f"Implementation result:\n{code_result}\n\n"
            f"Your response MUST end with a verdict line in exactly this format:\n"
            f"VERDICT: PASS\n"
            f"or\n"
            f"VERDICT: FAIL\n\n"
            f"If FAIL, provide detailed fix instructions before the verdict line."
        )
        reviewer_aid = await scheduler.spawn(parent_aid, "reviewer", review_prompt)
        await scheduler.wait_until_terminal(reviewer_aid)
        reviewer_scb = scheduler.table.get(reviewer_aid)
        review_result = reviewer_scb.result if reviewer_scb else ""

        verdict = ReviewVerdict.parse(review_result)
        await _emit_review_event(
            scheduler,
            scheduler.events.review_completed(iteration, verdict.passed)
        )

        if verdict.passed:
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
        last_result = code_result

    return (
        f"[Self-Collaboration: FAILED after {max_iterations} iterations]\n\n"
        f"Last implementation:\n{last_result}"
    )


__all__ = [
    "MAX_REVIEW_ITERATIONS",
    "ReviewLoopScheduler",
    "run_spawn_with_review",
    "validate_review_iterations",
]
