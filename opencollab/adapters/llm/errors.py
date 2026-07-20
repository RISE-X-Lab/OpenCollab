"""Provider-error classification for context-window overflow.

A context overflow is the one 400/BadRequest the run loop must treat specially:
it is *not* transient (retrying the same prompt is futile — see ``retry.py``,
which already excludes 400 from the retryable set), and it is *not* a generic
client error either. The session layer catches it to force a maximal compaction
pass and retry once, then degrade gracefully if even that overflows.

This classifier is deliberately conservative: a 400 alone is not enough (a
malformed request, an invalid tool schema, a bad model id all yield 400). We
require both a 400-shaped status AND a message fragment that names a
context-length problem, so an unrelated 400 is never misread as an overflow and
silently force-compacted.
"""

from __future__ import annotations

# Status codes a context overflow takes. Anthropic and OpenAI-compatible
# providers both surface it as an HTTP 400 (BadRequest).
_OVERFLOW_STATUS_CODES = frozenset({400})

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

    Conservative by design: True only when the error is 400-shaped *and* either
    its provider error ``code`` or its message names a context-length problem.
    A bare 400 with no overflow wording is treated as a generic client error
    (returns False) so it is never futilely force-compacted.
    """
    status = _status_of(error)
    if status not in _OVERFLOW_STATUS_CODES:
        return False

    code = _error_code_of(error)
    if "context_length_exceeded" in code or "context length" in code:
        return True

    message = str(error).lower()
    return any(fragment in message for fragment in _OVERFLOW_MESSAGE_FRAGMENTS)
