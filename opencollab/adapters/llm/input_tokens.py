"""Additive estimates over the history representation replayed by a provider."""

from __future__ import annotations

from typing import Any

from opencollab.adapters.llm.anthropic_provider import convert_to_anthropic_messages
from opencollab.adapters.llm.openai_provider import _normalize_request_messages
from opencollab.adapters.llm.providers import RESPONSES, is_anthropic
from opencollab.adapters.llm.responses_messages import messages_to_input
from opencollab.domain.token_estimation import estimate_request_message_tokens


def estimate_input_message_tokens(
    messages: list[dict[str, Any]],
    *,
    provider: str,
    wire_protocol: str,
    stream_chat: bool,
) -> int:
    """Size replayed input, excluding display copies and unsent reasoning.

    Project each source message separately to retain additivity when shaping
    subtracts a group or replaces one tool result. Per-message framing remains
    conservative when a provider merges adjacent messages. Server-side thinking
    filtering and encrypted-state tokenization are not locally observable, so
    native state is counted conservatively rather than silently discarded.
    """
    projected: list[dict[str, Any]] = []
    if is_anthropic(provider):
        for message in messages:
            system, native_messages = convert_to_anthropic_messages([message])
            projected.extend({"role": "system", "content": part} for part in system)
            projected.extend(native_messages)
    elif wire_protocol == RESPONSES:
        for message in messages:
            instructions, items = messages_to_input([message], allow_unmatched_tool_outputs=True)
            if instructions:
                projected.append({"role": "system", "content": instructions})
            if items:
                # Count native items or converted legacy content, never both.
                projected.append({"role": message.get("role"), "content": items})
    else:
        projected = _normalize_request_messages(messages, keep_reasoning_content=not stream_chat)
    return estimate_request_message_tokens(projected)
