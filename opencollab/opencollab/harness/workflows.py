"""Example workflows for the headless eval harness.

These are plain async workflow functions (see ``application.workflow``) that the
evaluator can run instead of a single agent session. They live in the harness
(outer) layer, so they may read the shared task ``Environment`` that the eval
runner attaches to the context as ``ctx.env``.

``generate_review_fix`` is the A/B candidate against the single-session
baseline: implement -> review the diff -> conditionally apply the feedback.
"""

from __future__ import annotations

from typing import Any

# Structured verdict the review agent must emit. ``needs_changes`` gates the
# (optional) third apply stage; ``feedback`` carries the actionable notes.
REVIEW_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["needs_changes", "feedback"],
    "properties": {
        "needs_changes": {"type": "boolean"},
        "feedback": {"type": "string"},
    },
}

_IMPLEMENT_PROMPT = (
    "Implement a fix for the following task. Read the relevant files first, then "
    "make minimal, targeted edits. Do NOT commit.\n\nTask:\n{description}"
)

_REVIEW_PROMPT = (
    "You are reviewing a proposed code change for the task below. Here is the "
    "current diff of the working tree:\n\n{diff}\n\nTask:\n{description}\n\n"
    "Decide whether the change needs further work. If it is correct and "
    "complete, set needs_changes=false. Otherwise set needs_changes=true and "
    "give specific, actionable feedback."
)

_APPLY_PROMPT = (
    "A reviewer found issues with your change for the task below. Apply their "
    "feedback with minimal, targeted edits. Do NOT commit.\n\nTask:\n"
    "{description}\n\nReviewer feedback:\n{feedback}"
)


async def _current_diff(ctx: Any) -> str:
    """Return the working-tree diff via the env attached to ``ctx`` (best-effort)."""
    env = getattr(ctx, "env", None)
    if env is None:
        return ""
    try:
        result = await env.exec_cmd("git diff")
        if result.stdout_truncated or result.stderr_truncated:
            return (
                "[diff unavailable: command output truncated; "
                f"stdout dropped {result.stdout_dropped_bytes} bytes, "
                f"stderr dropped {result.stderr_dropped_bytes} bytes]"
            )
        diff = result.stdout
        if not diff.strip():
            result = await env.exec_cmd("git diff HEAD")
            if result.stdout_truncated or result.stderr_truncated:
                return (
                    "[diff unavailable: command output truncated; "
                    f"stdout dropped {result.stdout_dropped_bytes} bytes, "
                    f"stderr dropped {result.stderr_dropped_bytes} bytes]"
                )
            diff = result.stdout
        return diff
    except Exception:  # noqa: BLE001 — a missing diff must not abort the workflow
        return ""


async def generate_review_fix(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Implement, review, then conditionally apply review feedback.

    Stage 1 implements the fix. Stage 2 reviews the resulting diff with a
    structured verdict. Stage 3 runs only when the verdict requests changes.
    Returns the verdict dict (plus an ``applied`` flag) for trajectory analysis.
    """
    description = str(args.get("description", ""))

    await ctx.phase("implement")
    await ctx.agent(
        _IMPLEMENT_PROMPT.format(description=description),
        label="implement",
    )

    await ctx.phase("review")
    diff = await _current_diff(ctx)
    verdict = await ctx.agent(
        _REVIEW_PROMPT.format(diff=diff, description=description),
        schema=REVIEW_VERDICT_SCHEMA,
        label="review",
    )

    # A failed/None review verdict is treated as "no changes requested" so a
    # dead review agent never forces a speculative apply stage.
    if not isinstance(verdict, dict):
        return {"needs_changes": False, "feedback": "", "applied": False}

    if not verdict.get("needs_changes"):
        return {**verdict, "applied": False}

    await ctx.phase("apply")
    await ctx.agent(
        _APPLY_PROMPT.format(
            description=description,
            feedback=str(verdict.get("feedback", "")),
        ),
        label="apply",
    )
    return {**verdict, "applied": True}


__all__ = ["REVIEW_VERDICT_SCHEMA", "generate_review_fix"]
