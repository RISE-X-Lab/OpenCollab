"""Response containers, model metadata, and token-estimation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Response containers
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Single LLM completion result."""

    content: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=lambda: Usage())
    finish_reason: str | None = None


# ---------------------------------------------------------------------------
# Model context windows
# ---------------------------------------------------------------------------

# Best-effort context-window sizes (tokens), keyed by a substring of the model
# id. Used to derive history-compaction triggers (see
# ``shaping.history_trigger_target``); an unrecognised model returns ``None`` and
# the caller falls back to fixed defaults. Substring match keeps this resilient
# to version/date suffixes (e.g. ``claude-opus-4-8-2026...`` matches ``claude``).
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o3": 200_000,
    "deepseek": 64_000,
    "qwen": 131_072,
    "gemini": 1_000_000,
}

# Conservative default output reservation when a model is recognised.
DEFAULT_MAX_OUTPUT_TOKENS = 8_192


def model_context_window(model: str | None) -> int | None:
    """The known context window for ``model``, or ``None`` if unrecognised."""
    if not model:
        return None
    lowered = model.lower()
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in lowered:
            return window
    return None


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English, ~2 for CJK."""
    return max(1, len(text) // 3)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across a message list."""
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and "text" in part:
                    total += estimate_tokens(part["text"])
    return total
