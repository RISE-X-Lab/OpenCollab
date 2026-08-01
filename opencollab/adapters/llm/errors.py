"""Shared provider errors and request-size overflow classification.

A context overflow is a request rejection the run loop must treat specially:
it is *not* transient (retrying the same prompt is futile — see ``retry.py``,
which already excludes client errors from the retryable set), and it is *not* a
generic client error either. The session layer catches it to force a maximal
compaction pass and retry once, then degrade gracefully if even that overflows.

This classifier is deliberately conservative. A 400 alone is not enough because
malformed requests, invalid tool schemas, and bad model ids all yield 400. A 413
is accepted only with the relay's exact ``upstream_request_too_large`` code.
"""

from __future__ import annotations


class TransientProviderError(RuntimeError):
    """A provider failure for which repeating the same request can succeed."""


class TransientEmptyOutputError(TransientProviderError):
    """A provider completed a request without usable model output."""


_CONTEXT_OVERFLOW_STATUS = 400
_RELAY_WIRE_OVERFLOW_STATUS = 413
_RELAY_WIRE_OVERFLOW_CODE = "upstream_request_too_large"

# Lowercased message fragments that name a context-length overflow. Covers the
# Anthropic ("prompt is too long"), OpenAI ("maximum context length",
# "context_length_exceeded", "reduce the length") and common OpenAI-compatible
# proxy phrasings. Matched as substrings so version/wording drift still trips.
_OVERFLOW_MESSAGE_FRAGMENTS = (
    "context length",
    "context_length_exceeded",
    "maximum context",
    "context window",
    "prompt is too long",
    "too many tokens",
    "reduce the length",
    "reduce the number of tokens",
    "string too long",  # some proxies phrase the oversize prompt this way
)


def _status_of(error: Exception) -> int | None:
    """Best-effort HTTP status for ``error`` (direct attr or on ``.response``)."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    if response is not None:
        resp_status = getattr(response, "status_code", None)
        if isinstance(resp_status, int):
            return resp_status
    return None


def _error_code_of(error: Exception) -> str:
    """Best-effort provider error ``code`` string, lowercased (e.g. OpenAI's
    ``context_length_exceeded``). Empty string when absent."""
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code.lower()
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            inner_code = inner.get("code")
            if isinstance(inner_code, str):
                return inner_code.lower()
    return ""


def is_context_overflow_error(error: Exception) -> bool:
    """Whether ``error`` is a context-window overflow (prompt too large).

    A 400 requires an explicit context-length code or message. A 413 requires
    the relay's exact wire-size code. Other client errors remain unclassified.
    """
    status = _status_of(error)
    code = _error_code_of(error)
    if status == _RELAY_WIRE_OVERFLOW_STATUS:
        return code == _RELAY_WIRE_OVERFLOW_CODE
    if status != _CONTEXT_OVERFLOW_STATUS:
        return False
    if "context_length_exceeded" in code or "context length" in code:
        return True

    message = str(error).lower()
    return any(fragment in message for fragment in _OVERFLOW_MESSAGE_FRAGMENTS)
