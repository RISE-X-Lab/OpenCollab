"""Minimal LLM interface — thin wrapper over OpenAI-compatible API.

Supports any OpenAI-compatible provider (OpenAI, DeepSeek, local models)
and Anthropic natively. No custom message format — uses standard dicts.

This package keeps one provider per module behind the ``LLMClient`` facade:

- ``types``      — response containers, model metadata, token estimators
- ``retry``      — transient-error retry policy
- ``openai_provider`` / ``anthropic_provider`` — request build/parse per provider
- ``client``     — the ``LLMClient`` facade dispatching to providers
"""

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.adapters.llm.types import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MODEL_CONTEXT_WINDOWS,
    LLMResponse,
    ModelCapabilities,
    Usage,
    estimate_messages_tokens,
    estimate_tokens,
    model_capabilities,
    model_context_window,
)

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "MODEL_CONTEXT_WINDOWS",
    "LLMClient",
    "LLMResponse",
    "ModelCapabilities",
    "Usage",
    "estimate_messages_tokens",
    "estimate_tokens",
    "is_context_overflow_error",
    "model_capabilities",
    "model_context_window",
]
