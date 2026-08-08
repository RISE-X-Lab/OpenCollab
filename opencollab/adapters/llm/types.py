"""Response containers, model metadata, and token-estimation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP

# ---------------------------------------------------------------------------
# Response containers
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Token accounting for one completion.

    ``input_tokens`` is the *true* total input the model processed for the
    call. Cached-token fields are retained for observability and pricing
    reports; they are already included in ``input_tokens`` and must not be
    added again to compute the budget total.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated: bool = False
    raw_usage: dict[str, Any] = field(default_factory=dict)
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
    # Provider-owned continuation data that must survive a tool round trip.
    # The application stores it without interpreting it, and each provider
    # removes data that does not belong on its request path.
    provider_state: dict[str, Any] | None = None


def rescue_empty_turn(
    content: str | None,
    tool_calls: list[dict[str, Any]],
    reasoning: str | None,
) -> str | None:
    """Empty-stop rescue rung, shared by both providers.

    Providers keep chain-of-thought (``reasoning``) separate from the answer
    (``content``). When a completion returns neither answer text nor a tool
    call, an empty ``content`` would silently end the session; fall back to the
    reasoning so the turn still carries something forward. Returns ``content``
    unchanged whenever the turn already produced an answer or a tool call — so
    this is the single source both ``openai_provider`` and ``anthropic_provider``
    delegate to instead of hand-mirroring the guard.
    """
    if not content and not tool_calls:
        return reasoning
    return content


# ---------------------------------------------------------------------------
# Model context windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCapabilities:
    """Provider-facing behavior known for one exact model identifier."""

    context_window: int | None = None
    supports_forced_tool_choice: bool = True
    honors_workflow_thinking_override: bool = True


# Best-effort context-window sizes (tokens), keyed by a model family. Used to
# derive history-compaction triggers (see ``shaping.history_trigger_target``);
# an unrecognised model returns ``None`` and the caller falls back to fixed
# defaults. Family matching accepts provider prefixes and hyphenated
# version/date suffixes without treating unrelated substrings as a known model.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude": 200_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o3": 200_000,
    "deepseek": 64_000,
    "qwen": 131_072,
    "glm-5.2": 400_000,
    "gemini": 1_000_000,
}

_EXACT_MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "k3": ModelCapabilities(
        context_window=1_048_576,
        supports_forced_tool_choice=False,
        honors_workflow_thinking_override=False,
    ),
    "kimi-for-coding": ModelCapabilities(
        context_window=262_144,
        supports_forced_tool_choice=False,
        honors_workflow_thinking_override=False,
    ),
}

# Conservative default output reservation when a model is recognised.
DEFAULT_MAX_OUTPUT_TOKENS = DEFAULT_MAX_TOKENS_PER_STEP


def model_matches_family(model: str | None, family: str) -> bool:
    """Whether ``model`` is an exact or hyphen-suffixed family identifier."""
    if not model:
        return False
    normalized = model.strip().lower().rsplit("/", 1)[-1]
    normalized_family = family.strip().lower()
    return normalized == normalized_family or normalized.startswith(
        f"{normalized_family}-"
    )


def _canonical_model_id(model: str) -> str:
    leaf = model.strip().lower().rsplit("/", 1)[-1]
    if leaf in _EXACT_MODEL_CAPABILITIES:
        return leaf
    for known in _EXACT_MODEL_CAPABILITIES:
        if re.fullmatch(rf"{re.escape(known)}-\d{{4}}(?:-\d{{2}}){{0,2}}", leaf):
            return known
    return leaf


def model_capabilities(model: str | None) -> ModelCapabilities:
    """Return exact capability metadata plus a best-effort context window."""
    if not model:
        return ModelCapabilities()
    canonical = _canonical_model_id(model)
    if canonical in _EXACT_MODEL_CAPABILITIES:
        return _EXACT_MODEL_CAPABILITIES[canonical]
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if model_matches_family(model, key):
            return ModelCapabilities(context_window=window)
    return ModelCapabilities()


def model_context_window(model: str | None) -> int | None:
    """The known context window for ``model``, or ``None`` if unrecognised."""
    return model_capabilities(model).context_window


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
