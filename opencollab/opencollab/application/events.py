"""Single source of truth for run-loop event construction.

Every session-runtime event — run loop, tool execution, compaction — is built
here so the event vocabulary lives in one place and each event carries the
owning agent's ``aid`` for TUI routing. The use cases hold a frozen
``SessionEventFactory`` (injected in tests, defaulted otherwise) and call only
the builders they need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from opencollab.domain.events import SessionRuntimeEvent


@dataclass(frozen=True)
class SessionEventFactory:
    # run loop
    step_start: Callable[[int], Any]
    step_end: Callable[[int, float], Any]
    text_delta: Callable[[str], Any]
    error: Callable[[str], Any]
    # compaction
    compaction: Callable[[], Any]
    compaction_applied: Callable[[int], Any]
    # tool execution
    loop_detected: Callable[[str, int], Any]
    tool_start: Callable[[str, dict[str, Any]], Any]
    tool_end: Callable[[str, float], Any]


def default_session_event_factory(aid: int = -1) -> SessionEventFactory:
    return SessionEventFactory(
        step_start=lambda step: SessionRuntimeEvent(
            type="step_start", data={"step": step, "aid": aid}
        ),
        step_end=lambda step, latency: SessionRuntimeEvent(
            type="step_end", data={"step": step, "latency": latency, "aid": aid}
        ),
        text_delta=lambda content: SessionRuntimeEvent(
            type="text_delta", data={"content": content, "aid": aid}
        ),
        error=lambda reason: SessionRuntimeEvent(
            type="error", data={"reason": reason, "aid": aid}
        ),
        compaction=lambda: SessionRuntimeEvent(
            type="compaction", data={"reason": "context_overflow", "aid": aid}
        ),
        compaction_applied=lambda tokens_after: SessionRuntimeEvent(
            type="compaction_applied", data={"tokens_after": tokens_after, "aid": aid}
        ),
        loop_detected=lambda tool, count: SessionRuntimeEvent(
            type="loop_detected", data={"tool": tool, "count": count, "aid": aid}
        ),
        tool_start=lambda tool, args: SessionRuntimeEvent(
            type="tool_start", data={"tool": tool, "args": args, "aid": aid}
        ),
        tool_end=lambda tool, latency: SessionRuntimeEvent(
            type="tool_end", data={"tool": tool, "latency": latency, "aid": aid}
        ),
    )


__all__ = ["SessionEventFactory", "default_session_event_factory"]
