"""Shared types and configuration for session execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opencollab.application.ports import CompletionResponse

DEFAULT_DEFERRABLE_TOOLS = frozenset({"spawn_agent"})

class GenerationTimeoutError(asyncio.TimeoutError):
    """A single provider generation exceeded its per-call ceiling."""

class _ContextOverflowStop(Exception):
    """Internal control-flow signal: the prompt still overflows the model window
    after a forced maximal compaction pass and one retry. Caught inside
    ``run_llm_call`` to perform a controlled graceful stop (CONTEXT_OVERFLOW)
    rather than letting it propagate as an unhandled ERROR. Never escapes the
    use case.
    """

class _TeamBudgetStop(Exception):
    """Internal signal that shared team allowance ran out before a request."""

@dataclass(slots=True)
class _RequestBudgetRecovery:
    available: Callable[[], int]
    claim: Callable[[int, int], int | None]
    observed: bool = False

class _TokenBudgetStop(Exception):
    """Internal signal that a request has insufficient legal output headroom."""

    def __init__(self, *, reserved_input_tokens: int, remaining_budget: int, minimum_output_tokens: int = 1) -> None:
        self.reserved_input_tokens = reserved_input_tokens
        self.remaining_budget = remaining_budget
        self.minimum_output_tokens = minimum_output_tokens
        super().__init__(
            "conservative input reservation requires "
            f"{reserved_input_tokens} of {remaining_budget} remaining tokens"
        )

@dataclass
class PendingStep:
    """The LLM response carried across HANDLING_RESPONSE → EXECUTING_TOOLS →
    AUTOSAVING. Lives here, not in domain ``SessionState``: the response is an
    adapter-shaped object and would breach the inward dependency rule.
    """

    response: CompletionResponse
    latency: float
    started_at: float | None = None

# Retry an empty stop before treating a turn with no answer or action as complete.
_EMPTY_STOP_NUDGE = (
    "Your previous response was empty — no message and no tool call. "
    "If the task is finished, say so briefly. Otherwise continue now by "
    "calling the appropriate tool or giving your answer."
)

# Separate consecutive user messages for Anthropic.
_EMPTY_STOP_PLACEHOLDER = "[no output produced this turn]"

_OUTPUT_LIMIT_REASONS = frozenset({"length", "max_tokens"})
_INCOMPLETE_RESPONSE_REASONS = _OUTPUT_LIMIT_REASONS | {"model_context_window_exceeded", "content_filter"}
_FILTERED_OUTPUT_REASON = "provider filtered the output"
_BUDGET_EXCEEDED_PREFIX = "budget exceeded:"
_REQUEST_BUDGET_REFUSAL_PREFIX = "budget exhausted before model call:"

def _last_assistant_answer(messages: list[dict[str, Any]], turn_start: int | None) -> str:
    if turn_start is None:
        return ""
    for message in reversed(messages[turn_start:]):
        content = message.get("content")
        if (
            message["role"] == "assistant"
            and content
            and message.get("_opencollab_placeholder") is not True
        ):
            return content
    return ""

# Wind-down and steering share this tool vocabulary.
_READ_TOOLS = frozenset({"file_read", "grep"})
_WRITE_TOOLS = frozenset({"file_write", "apply_patch"})
_STRUCTURED_OUTPUT_TOOL = "structured_output"

def _request_tool_names(tools: list[dict] | None) -> list[str]:
    """Sorted names of the tool schemas one request carries.

    Names only. The schema bodies are already recorded once per seat by
    ``assigned.topology_nodes``; repeating them on every call would multiply the
    trajectory for nothing, while the names alone answer the question the record
    is for -- which tools this particular request offered.
    """
    return sorted(
        str(name)
        for spec in (tools or [])
        if isinstance(spec, dict)
        and (name := spec.get("function", {}).get("name")) is not None
    )

# Enable the protected submit turn for models that need budget enforcement.
ENFORCEMENT_OFF = "off"
ENFORCEMENT_ON = "needs-enforcement"

# Keep the submit tool name as data to avoid importing the tool into this layer.
SUBMIT_TOOL_NAME = "submit_findings"

# Carved from the token cap so the protected submit turn stays within budget.
DEFAULT_COMMIT_RESERVE = 25_000

# No-progress steps and low-yield results also trigger _enter_wind_down.
# Use information gain rather than has_write, which represents repository mutation
# and is shared with verification and diff accounting.
DEFAULT_WATCHDOG_K = 4
DEFAULT_LOW_YIELD_M = 3
DEFAULT_LOOP_BLOCKED_LIMIT = 3

# Announce tool removal immediately so the model submits instead of spending
# its protected turn retrying an exploration tool that is now unavailable.
_WIND_DOWN_NUDGE = (
    "Exploration budget spent — all exploration tools (grep/file_read/bash) have now "
    "been removed; ONLY submit_findings is available. Your NEXT action MUST be "
    "submit_findings with the evidence you already have; do not call any other tool. "
    "Cite file:line / exact matched strings from your real tool results; mark each "
    "finding verified|unverified; if you lack evidence for a dimension, set "
    "insufficient_evidence — do NOT fabricate."
)

# One explicit retry for models that still call a removed exploration tool.
_WIND_DOWN_RETRY = (
    "Only submit_findings is available. Call submit_findings now with the evidence you "
    "have; do not call any other tool. Cite file:line / exact matched strings; mark each "
    "finding verified|unverified; set insufficient_evidence if you lack evidence — do NOT "
    "fabricate."
)

def _submit_tool_choice(name: str) -> dict[str, Any]:
    """Build an OpenAI-style choice forcing the named submit function.

    A provider rejection can fall back once to ``auto``. The submit-only
    wind-down toolset and retry instruction still apply.
    """
    return {"type": "function", "function": {"name": name}}
