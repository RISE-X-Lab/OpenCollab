"""Response containers, model metadata, and token-estimation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Response containers
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token accounting for one completion.

    ``input_tokens`` is the *true* total input the model processed for the
    call. For providers with prompt caching (Anthropic), the provider folds
    cached read/creation tokens into ``input_tokens`` so the budget meter and
    ``total_tokens`` reflect everything the API actually billed/processed.
    ``cache_read_tokens`` / ``cache_creation_tokens`` are retained purely for
    observability (tracing) and are already included in ``input_tokens``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated: bool = False
    # Observability counter: 1 when this completion leaked kimi tool-call markup
    # as literal text (in ``content`` or ``reasoning_content``) that the provider
    # recovered into structured ``tool_calls`` (P6), else 0. Summed up the chain
    # so a run's metrics surface how often the recovery fired — a regression alarm
    # if it spikes or (after a provider fix) silently drops to zero.
    markup_recovered: int = 0

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
    # Provider chain-of-thought (OpenAI ``reasoning_content`` / Anthropic
    # ``thinking`` blocks), kept for trajectory observability. ``None`` when the
    # provider/turn produced no thinking.
    reasoning: str | None = None


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
