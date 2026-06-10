"""Provider classification shared across the LLM facade, config, and CLI.

A single source of truth for "is this the native-Anthropic path?" so the
``== "anthropic"`` check is not duplicated (and drifting) across layers, plus a
near-miss warning that catches provider typos (e.g. ``"Anthropic "``/``"claude"``)
before they silently fall through to the OpenAI-compatible path.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ANTHROPIC = "anthropic"

# Provider strings that look like a user meant Anthropic but don't normalize to
# ``anthropic`` — treating these as OpenAI-compatible would silently send the
# request to the wrong SDK with the wrong API key.
_ANTHROPIC_NEAR_MISSES = frozenset({"anthropics", "claude", "claude-ai", "anthropic-ai"})


def normalize_provider(provider: str | None) -> str:
    """Lower-case, whitespace-trimmed provider id (``"openai"`` when empty)."""
    return (provider or "openai").strip().lower()


def is_anthropic(provider: str | None) -> bool:
    """Whether ``provider`` selects the native Anthropic SDK path."""
    return normalize_provider(provider) == ANTHROPIC


def required_env_key(provider: str | None) -> str:
    """The provider-specific API-key env var checked for a missing key."""
    return "ANTHROPIC_API_KEY" if is_anthropic(provider) else "OPENAI_API_KEY"


def warn_provider_near_miss(provider: str | None) -> None:
    """Warn when ``provider`` resembles Anthropic but won't take that path."""
    normalized = normalize_provider(provider)
    if normalized != ANTHROPIC and normalized in _ANTHROPIC_NEAR_MISSES:
        logger.warning(
            "provider %r is not recognized as 'anthropic'; "
            "falling back to the OpenAI-compatible path. "
            "Set provider='anthropic' for the native Anthropic SDK.",
            provider,
        )
