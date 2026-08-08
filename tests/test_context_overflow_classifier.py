"""Tests for the context-overflow classifier and its non-retryable guard.

The classifier (``adapters/llm/errors.is_context_overflow_error``) is the first
link in the context-overflow safety net: it must recognise the representative
Anthropic / OpenAI-compatible overflow rejections while staying conservative
enough that an unrelated 400 is never misread as an overflow (which would
trigger a futile force-compaction). It must also keep overflows out of the
transient-retry path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.adapters.llm.retry import extract_retry_after_seconds, is_retryable_error


class FakeProviderError(Exception):
    """A provider-error stand-in mirroring the SDK shape the classifier reads:
    a ``status_code`` (and optionally a ``code`` / nested ``body``) plus a
    human message. No real SDK / network involved.
    """

    def __init__(self, message="", *, status_code=None, code=None, body=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        if body is not None:
            self.body = body


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeErrorWithResponse(Exception):
    """A provider error that carries its status only on a ``.response`` object
    (the httpx-style shape some SDKs use)."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.response = FakeResponse(status_code)


# --- True cases: representative overflow rejections --------------------------


def test_anthropic_prompt_too_long_is_overflow():
    err = FakeProviderError(
        "prompt is too long: 250000 tokens > 200000 maximum",
        status_code=400,
    )
    assert is_context_overflow_error(err) is True


def test_openai_maximum_context_length_message_is_overflow():
    err = FakeProviderError(
        "This model's maximum context length is 128000 tokens. "
        "However, your messages resulted in 140000 tokens. "
        "Please reduce the length of the messages.",
        status_code=400,
    )
    assert is_context_overflow_error(err) is True


def test_openai_context_length_exceeded_code_is_overflow():
    # OpenAI surfaces the machine code even when the message wording varies.
    err = FakeProviderError(
        "request too large",
        status_code=400,
        code="context_length_exceeded",
    )
    assert is_context_overflow_error(err) is True


def test_overflow_code_in_nested_body_is_overflow():
    err = FakeProviderError(
        "bad request",
        status_code=400,
        body={"error": {"code": "context_length_exceeded"}},
    )
    assert is_context_overflow_error(err) is True


def test_overflow_code_at_top_level_body_is_overflow():
    err = FakeProviderError(
        "bad request",
        status_code=400,
        body={"code": "context_length_exceeded"},
    )
    assert is_context_overflow_error(err) is True


def test_status_on_response_object_is_overflow():
    err = FakeErrorWithResponse(
        "input is too long: too many tokens for the context window", 400
    )
    assert is_context_overflow_error(err) is True


def test_reduce_the_length_phrasing_is_overflow():
    err = FakeProviderError(
        "Please reduce the length of your prompt.", status_code=400
    )
    assert is_context_overflow_error(err) is True


# --- False cases: must NOT be classified as overflow -------------------------


def test_generic_400_without_overflow_wording_is_not_overflow():
    err = FakeProviderError("Invalid 'tools': schema is malformed", status_code=400)
    assert is_context_overflow_error(err) is False


def test_bad_model_id_400_is_not_overflow():
    err = FakeProviderError(
        "The model 'gpt-nonexistent' does not exist", status_code=400
    )
    assert is_context_overflow_error(err) is False


def test_rate_limit_429_is_not_overflow():
    err = FakeProviderError("Rate limit exceeded", status_code=429)
    assert is_context_overflow_error(err) is False


def test_server_error_500_is_not_overflow():
    err = FakeProviderError(
        "internal error: context length", status_code=500
    )
    # Even with overflow-looking wording, a non-400 status is not an overflow.
    assert is_context_overflow_error(err) is False


def test_overflow_wording_without_status_is_not_overflow():
    # No 400-shaped status anywhere → conservative False.
    err = Exception("prompt is too long")
    assert is_context_overflow_error(err) is False


def test_plain_exception_is_not_overflow():
    assert is_context_overflow_error(ValueError("boom")) is False


# --- Retry guard: overflow stays non-retryable -------------------------------


def test_overflow_is_not_retryable():
    err = FakeProviderError("prompt is too long", status_code=400)
    assert is_retryable_error(err) is False


def test_overflow_with_retryable_sounding_message_still_not_retryable():
    # Defensive: even if an overflow's text happened to contain a transient
    # fragment, the explicit overflow guard must keep it out of the retry loop.
    err = FakeProviderError(
        "prompt is too long; the server is overloaded", status_code=400
    )
    assert is_retryable_error(err) is False


def test_genuine_transient_error_still_retryable():
    err = FakeProviderError("Service temporarily unavailable", status_code=503)
    assert is_retryable_error(err) is True


def test_retry_after_rejects_non_finite_and_negative_values():
    class Response:
        def __init__(self, value):
            self.headers = {"Retry-After": value}

    class Error(Exception):
        def __init__(self, value):
            self.response = Response(value)

    assert extract_retry_after_seconds(Error("inf")) is None
    assert extract_retry_after_seconds(Error("nan")) is None
    assert extract_retry_after_seconds(Error("-1")) is None
    assert extract_retry_after_seconds(Error("9999")) == 300.0


def test_retry_after_accepts_standard_http_date():
    class Response:
        headers = {}

    class Error(Exception):
        response = Response()

    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    Error.response.headers["Retry-After"] = format_datetime(now + timedelta(seconds=45))

    assert extract_retry_after_seconds(Error(), now=now) == 45.0
