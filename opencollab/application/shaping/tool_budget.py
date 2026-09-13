"""Per-tool-result budget shaper — the unconditional per-message cap."""

from __future__ import annotations

import copy
import json
from typing import Any

# Per-tool-result character budget. A single tool result larger than this is
# truncated (for the model's view only) to its head plus a re-read pointer.
DEFAULT_TOOL_RESULT_BUDGET = 16_000


class PerToolResultBudgetShaper:
    """Caps each tool-result message at ``max_chars`` for the model's view.

    A tool message whose ``content`` exceeds the budget is replaced (in a new
    dict — the input list and its messages are left untouched) with its head
    slice plus a reference notice telling the model how to recover the rest by
    re-reading a narrower range. The result is guaranteed to fit the budget.
    """

    #: Frozen trajectory label for this rung (see ``ShaperPipeline``).
    rung = "per_tool_budget"

    def __init__(self, max_chars: int = DEFAULT_TOOL_RESULT_BUDGET):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise ValueError("max_chars must be a positive integer")
        self.max_chars = max_chars

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shaped: list[dict[str, Any]] = []
        pending: dict[str, int] = {}
        for message in messages:
            shaped.append(self._shape_message(message))
            if message.get("role") == "assistant":
                calls = message.get("tool_calls")
                pending = {
                    call["id"]: len(shaped) - 1
                    for call in (calls if isinstance(calls, list) else ())
                    if isinstance(call, dict) and isinstance(call.get("id"), str)
                }
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                owner = pending.pop(call_id, None) if isinstance(call_id, str) else None
                content = message.get("content")
                if owner is not None and isinstance(content, str) and content.startswith((
                    "Error: invalid JSON arguments:",
                    "Error: tool arguments must be a JSON object:",
                    "Error: entire tool-call batch rejected before execution:",
                )):
                    shaped[owner] = self._shape_rejected_arguments(shaped[owner], call_id)
        return shaped

    def _shape_rejected_arguments(self, message: dict[str, Any], call_id: str) -> dict[str, Any]:
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict) or call.get("id") != call_id:
                continue
            function = call.get("function")
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(arguments, str) or len(arguments) <= self.max_chars:
                return message
            try:
                if isinstance(json.loads(arguments), dict):
                    return message
            except ValueError:
                pass
            except RecursionError:
                return message
            # Retain the source transcript and keep both wire representations
            # paired. This is a historical rejected call, never an executable one.
            result = copy.deepcopy(message)
            replacement = '{"_rejected_tool_arguments":"omitted"}'
            for previous in result["tool_calls"]:
                if isinstance(previous, dict) and previous.get("id") == call_id:
                    previous["function"]["arguments"] = replacement
            for item in result.get("response_items") or ():
                if (
                    isinstance(item, dict) and item.get("type") == "function_call"
                    and item.get("call_id") == call_id
                ):
                    item["arguments"] = replacement
            return result
        return message

    def _shape_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("role") != "tool":
            return message
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= self.max_chars:
            return message
        def notice(dropped: int) -> str:
            return (
                f"\n\n[truncated {dropped} chars — re-read a narrower range "
                f"(file_read with offset/limit, or grep) to see the rest]"
            )

        # The number of dropped characters affects the marker length. Recompute
        # until its digit width stabilizes, then enforce the cap defensively.
        head_len = 0
        for _ in range(3):
            marker = notice(len(content) - head_len)
            head_len = max(0, self.max_chars - len(marker))
        marker = notice(len(content) - head_len)
        shaped_content = content[:head_len] + marker
        return {**message, "content": shaped_content[: self.max_chars]}
