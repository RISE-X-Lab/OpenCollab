"""Preserve native thinking across append-only requests and context edits."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from opencollab.domain.session import SessionState

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

def _strip_anthropic_thinking(messages: list[dict[str, Any]], start: int = 0) -> list[dict[str, Any]]:
    """Remove a suffix of native thinking while retaining text and tool blocks."""
    result = list(messages)
    for index in range(start, len(result)):
        message = result[index]
        state = message.get("provider_state")
        blocks = state.get("anthropic_content") if isinstance(state, dict) else None
        if not isinstance(blocks, list):
            continue
        kept = [block for block in blocks if not isinstance(block, dict) or block.get("type") not in _THINKING_TYPES]
        if len(kept) != len(blocks):
            result[index] = {**message, "provider_state": {**state, "anthropic_content": kept}}
    return result

def _edited_anthropic_history(before: list[dict], after: list[dict]) -> list[dict]:
    """Drop thinking bound to a changed prefix, keeping the valid older run."""
    if [message for message in before if message.get("role") == "system" and not message.get("compacted")] != [
        message for message in after if message.get("role") == "system" and not message.get("compacted")
    ]:
        return _strip_anthropic_thinking(after)
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new:
            return _strip_anthropic_thinking(after, index)
    return after

def _anthropic_source_edit_start(state: SessionState) -> int | None:
    history = state.anthropic_history
    source = history.get("source_messages") if history is not None else None
    if source is None or state.messages[:len(source)] == source:
        return None
    for index, (old, current) in enumerate(zip(source, state.messages)):
        if old != current:
            return index
    return len(state.messages)

def _anthropic_history_input(
    state: SessionState, *, on_source_edit: Callable[[int], None] | None = None,
) -> list[dict]:
    """Reuse the native view until its original transcript prefix changes."""
    history = state.anthropic_history
    if history is None:
        return state.messages
    edit_start = _anthropic_source_edit_start(state)
    if edit_start is not None:
        if on_source_edit is not None:
            on_source_edit(edit_start)
        return _edited_anthropic_history(history["source_messages"], state.messages)
    return copy.deepcopy([*history["messages"], *state.messages[history["source_count"]:]])

def _remember_anthropic_history(
    state: SessionState, messages: list[dict], tools: list[dict] | None,
    *, on_source_edit: Callable[[int], None] | None = None,
) -> list[dict]:
    """Persist edits so removed blocks and compacted content stay removed."""
    history = state.anthropic_history
    messages = _edited_anthropic_history(_anthropic_history_input(state, on_source_edit=on_source_edit), messages)
    if history is not None:
        messages = _edited_anthropic_history(history["messages"], messages)
        if history["tools"] != tools:
            messages = _strip_anthropic_thinking(messages)
    state.anthropic_history = copy.deepcopy({
        "messages": messages, "source_count": len(state.messages), "tools": tools,
        "source_messages": state.messages,
    })
    return messages

def _recover_anthropic_history(
    state: SessionState, messages: list[dict], tools: list[dict] | None, error: Exception,
    *, on_source_edit: Callable[[int], None] | None = None,
) -> tuple[list[dict], int] | None:
    """Retire replayed thinking after an explicit provider prefix rejection."""
    status = getattr(error, "status_code", None)
    if status != 400:
        return None
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else None
    if not isinstance(detail, dict) or detail.get("type") != "invalid_request_error":
        return None
    message = detail.get("message")
    if not isinstance(message, str) or (
        "Invalid `signature` in `thinking` block. The block is bound to a different conversation."
        not in message
    ):
        return None
    recovered = _strip_anthropic_thinking(messages)
    removed = sum(
        len(old["provider_state"]["anthropic_content"]) - len(new["provider_state"]["anthropic_content"])
        for old, new in zip(messages, recovered) if old is not new
    )
    if not removed:
        return None
    return _remember_anthropic_history(state, recovered, tools, on_source_edit=on_source_edit), removed
