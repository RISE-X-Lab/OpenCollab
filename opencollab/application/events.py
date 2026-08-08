"""Single source of truth for run-loop and scheduler event construction.

Every session-runtime event — run loop, tool execution — is built here so the
event vocabulary lives in one place and each event carries the owning agent's
``aid`` for TUI routing. The use cases hold a frozen ``SessionEventFactory``
(injected in tests, defaulted otherwise) and call only the builders they need.
The ``Scheduler`` holds a ``SchedulerEventFactory`` for the same reason on the
orchestration side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from opencollab.domain.events import SchedulerEvent, SessionRuntimeEvent

# Scheduler-event ``task`` payloads are capped so one huge task string cannot
# blow up a TUI line or a persisted manifest.
SCHEDULER_TASK_PREVIEW_CHARS = 100


@dataclass(frozen=True)
class SessionEventFactory:
    # run loop
    step_start: Callable[[int], Any]
    step_end: Callable[[int, float], Any]
    text_delta: Callable[[str], Any]
    error: Callable[[str], Any]
    # tool execution
    loop_detected: Callable[[str, int], Any]
    tool_start: Callable[[str, dict[str, Any], str | None], Any]
    tool_end: Callable[[str, float, str | None], Any]


def default_session_event_factory(
    aid: int | Callable[[], int] = -1,
) -> SessionEventFactory:
    def current_aid() -> int:
        return aid() if callable(aid) else aid

    return SessionEventFactory(
        step_start=lambda step: SessionRuntimeEvent(
            type="step_start", data={"step": step, "aid": current_aid()}
        ),
        step_end=lambda step, latency: SessionRuntimeEvent(
            type="step_end",
            data={"step": step, "latency": latency, "aid": current_aid()},
        ),
        text_delta=lambda content: SessionRuntimeEvent(
            type="text_delta", data={"content": content, "aid": current_aid()}
        ),
        error=lambda reason: SessionRuntimeEvent(
            type="error", data={"reason": reason, "aid": current_aid()}
        ),
        loop_detected=lambda tool, count: SessionRuntimeEvent(
            type="loop_detected",
            data={"tool": tool, "count": count, "aid": current_aid()},
        ),
        tool_start=lambda tool, args, tool_call_id=None: SessionRuntimeEvent(
            type="tool_start",
            data={
                "tool": tool,
                "args": args,
                "tool_call_id": tool_call_id,
                "aid": current_aid(),
            },
        ),
        tool_end=lambda tool, latency, tool_call_id=None: SessionRuntimeEvent(
            type="tool_end",
            data={
                "tool": tool,
                "latency": latency,
                "tool_call_id": tool_call_id,
                "aid": current_aid(),
            },
        ),
    )


@dataclass(frozen=True)
class SchedulerEventFactory:
    # agent lifecycle
    agent_spawned: Callable[[int, int, str, str], SchedulerEvent]
    agent_completed: Callable[[int, int | None, str, float, int], SchedulerEvent]
    agent_resumed: Callable[[int, str], SchedulerEvent]
    agent_failed: Callable[[int, str, str], SchedulerEvent]
    agent_cancelled: Callable[[int, str], SchedulerEvent]
    # teammate messaging
    agent_message_sent: Callable[[int, int, str, str], SchedulerEvent]
    agent_message_delivered: Callable[[int, int, str, int], SchedulerEvent]
    agent_message_rejected_on_restore: Callable[[int, int, str], SchedulerEvent]
    # review loop
    review_started: Callable[[int, int], SchedulerEvent]
    review_completed: Callable[[int, bool], SchedulerEvent]


def default_scheduler_event_factory() -> SchedulerEventFactory:
    return SchedulerEventFactory(
        agent_spawned=lambda aid, parent_aid, role, task: SchedulerEvent(
            type="agent_spawned",
            data={
                "aid": aid,
                "parent_aid": parent_aid,
                "role": role,
                "task": task[:SCHEDULER_TASK_PREVIEW_CHARS],
            },
        ),
        agent_completed=lambda aid, parent_aid, role, latency, result_len: SchedulerEvent(
            type="agent_completed",
            data={
                "aid": aid,
                "parent_aid": parent_aid,
                "role": role,
                "latency": latency,
                "result_len": result_len,
            },
        ),
        agent_resumed=lambda aid, role: SchedulerEvent(
            type="agent_resumed", data={"aid": aid, "role": role}
        ),
        agent_failed=lambda aid, role, error: SchedulerEvent(
            type="agent_failed", data={"aid": aid, "role": role, "error": error}
        ),
        agent_cancelled=lambda aid, role: SchedulerEvent(
            type="agent_cancelled", data={"aid": aid, "role": role}
        ),
        agent_message_sent=lambda from_aid, to_aid, role, summary: SchedulerEvent(
            type="agent_message_sent",
            data={
                "from_aid": from_aid,
                "to_aid": to_aid,
                "role": role,
                "summary": summary,
            },
        ),
        agent_message_delivered=lambda from_aid, to_aid, summary, content_len: SchedulerEvent(
            type="agent_message_delivered",
            data={
                "from_aid": from_aid,
                "to_aid": to_aid,
                "summary": summary,
                "content_len": content_len,
            },
        ),
        agent_message_rejected_on_restore=lambda from_aid, to_aid, reason: SchedulerEvent(
            type="agent_message_rejected_on_restore",
            data={
                "from_aid": from_aid,
                "to_aid": to_aid,
                "reason": reason,
            },
        ),
        review_started=lambda iteration, maximum: SchedulerEvent(
            type="review_started",
            data={"tool": "review_loop", "iteration": iteration, "max": maximum},
        ),
        review_completed=lambda iteration, passed: SchedulerEvent(
            type="review_completed",
            data={
                "tool": "review_loop",
                "iteration": iteration,
                "verdict": "PASS" if passed else "FAIL",
            },
        ),
    )


__all__ = [
    "SchedulerEventFactory",
    "SessionEventFactory",
    "default_scheduler_event_factory",
    "default_session_event_factory",
]
