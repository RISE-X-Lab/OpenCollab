"""Per-tool-result budget shaper — the unconditional per-message cap."""

from __future__ import annotations

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

    def __init__(self, max_chars: int = DEFAULT_TOOL_RESULT_BUDGET):
        self.max_chars = max_chars

    def shape(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shaped: list[dict[str, Any]] = []
        for message in messages:
            shaped.append(self._shape_message(message))
        return shaped

    def _shape_message(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("role") != "tool":
            return message
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= self.max_chars:
            return message
        # Reserve headroom so head + notice still fits the budget. The notice is
        # short and bounded; a fixed 200-char reserve covers it comfortably.
        head_len = max(0, self.max_chars - 200)
        dropped = len(content) - head_len
        notice = (
            f"\n\n[truncated {dropped} chars — re-read a narrower range "
            f"(file_read with offset/limit, or grep) to see the rest]"
        )
        return {**message, "content": content[:head_len] + notice}
