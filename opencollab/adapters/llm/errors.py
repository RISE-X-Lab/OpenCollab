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

import re


class TransientProviderError(RuntimeError):
    """A provider failure for which repeating the same request can succeed."""


class TransientEmptyOutputError(TransientProviderError):
    """A provider completed a request without usable model output."""


class StreamedUsageUnavailableError(RuntimeError):
    """A completed chat stream carried no usable token accounting.

    Deliberately NOT a ``TransientProviderError``: the stream finished (its
    ``finish_reason`` arrived), so the endpoint simply did not report usage for
    a request that asked for it via ``stream_options.include_usage``. Repeating
    the identical request would fail identically while paying for the output
    again. Failing loudly beats the alternative — ``_parse_usage`` would fall
    back to an *estimate*, and the budget meter, the USD ledger and every
    cross-arm token comparison would silently run on invented numbers.
    """

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
)

_INPUT_STRING_PARAM = re.compile(
    r"(?:input(?:\[(?:\d+|\*)\])?(?:\.(?:content(?:\[(?:\d+|\*)\])?"
    r"(?:\.(?:text|input_text))?|arguments|output))?"
    r"|messages\[(?:\d+|\*)\]\.content(?:\[(?:\d+|\*)\])?(?:\.(?:text|input_text))?)"
)


def _oversized_input_string(error: Exception) -> bool:
    param = getattr(error, "param", None)
    body = getattr(error, "body", None)
    if not isinstance(param, str) and isinstance(body, dict):
        inner = body.get("error")
        source = inner if isinstance(inner, dict) else body
        param = source.get("param")
    if isinstance(param, str):
        return _INPUT_STRING_PARAM.fullmatch(param.lower()) is not None
    # Some proxies omit the selector but include its exact path in the message.
    message = str(error).lower()
    paths = re.findall(
        r"(?<![\w.\[\]])(?:input|messages)(?:\[[\d*]+\])?(?:\.[a-z_]+(?:\[[\d*]+\])?)*(?![\w.\[\]])",
        message,
    )
    return any(_INPUT_STRING_PARAM.fullmatch(path) for path in paths)


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
        body_code = body.get("code")
        if isinstance(body_code, str):
            return body_code.lower()
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
    if code == "string_above_max_length":
        return _oversized_input_string(error)
    if "context_length_exceeded" in code or "context length" in code:
        return True

    message = str(error).lower()
    if "string too long" in message and _oversized_input_string(error):
        return True
    return any(fragment in message for fragment in _OVERFLOW_MESSAGE_FRAGMENTS)
