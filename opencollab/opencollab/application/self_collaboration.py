"""Self-collaboration: the sequential coder -> reviewer loop.

Extracted from ``Scheduler`` so the long, self-contained review-iteration logic
does not bloat the scheduler's core. It drives the loop through the slice of the
scheduler's public surface typed by ``ReviewLoopScheduler`` (``spawn``,
``wait_for``, ``table``, ``emit_scheduler_event``), so it takes the scheduler
as a parameter rather than being a method.
"""

from __future__ import annotations

from typing import Any, Protocol

from opencollab.domain.events import SchedulerEventType
from opencollab.domain.scheduler import ReviewVerdict, SessionTable


class ReviewLoopScheduler(Protocol):
    """The slice of the Scheduler surface the review loop drives."""

    table: SessionTable

    async def spawn(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str = "",
    ) -> int:
        ...

    async def wait_for(self, aid: int) -> None:
        ...

    async def emit_scheduler_event(
        self,
        event_type: SchedulerEventType,
        data: dict[str, Any],
    ) -> None:
        ...


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
    current_task = task
    last_result = ""

    for iteration in range(1, max_iterations + 1):
        await scheduler.emit_scheduler_event(
            "review_started",
            {
                "tool": "review_loop",
                "iteration": iteration,
                "max": max_iterations,
            },
        )

        # Spawn coder and wait
        coder_aid = await scheduler.spawn(parent_aid, "coder", current_task, context)
        await scheduler.wait_for(coder_aid)
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
        await scheduler.wait_for(reviewer_aid)
        reviewer_scb = scheduler.table.get(reviewer_aid)
        review_result = reviewer_scb.result if reviewer_scb else ""

        verdict = ReviewVerdict.parse(review_result)
        await scheduler.emit_scheduler_event(
            "review_completed",
            {
                "tool": "review_loop",
                "iteration": iteration,
                "verdict": "PASS" if verdict.passed else "FAIL",
            },
        )

        if verdict.passed:
            return (
                f"[Self-Collaboration: PASSED after {iteration} iteration(s)]\n\n"
                f"{code_result}"
            )

        current_task = (
            f"Your previous implementation failed review (iteration {iteration}/{max_iterations}).\n"
            f"Original task: {task}\n\n"
            f"Reviewer feedback:\n{review_result}\n\n"
            f"Fix the issues identified by the reviewer."
        )
        last_result = code_result

    return (
        f"[Self-Collaboration: FAILED after {max_iterations} iterations]\n\n"
        f"Last implementation:\n{last_result}"
    )


__all__ = ["ReviewLoopScheduler", "run_spawn_with_review"]
