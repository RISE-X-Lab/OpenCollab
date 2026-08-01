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

import pytest

from opencollab.adapters.llm import retry as retry_module
from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.adapters.llm.retry import (
    extract_retry_after_seconds,
    is_retryable_error,
    with_retry,
)


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


def test_status_on_response_object_is_overflow():
    err = FakeErrorWithResponse(
        "input is too long: too many tokens for the context window", 400
    )
    assert is_context_overflow_error(err) is True


def test_relay_wire_limit_413_is_compactable_overflow():
    err = FakeProviderError(
        "encoded upstream request exceeds the configured wire byte limit",
        status_code=413,
        body={"error": {"code": "upstream_request_too_large"}},
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


@pytest.mark.parametrize("code", [None, "payload_too_large", "context_length_exceeded"])
def test_unrelated_413_is_not_compactable_overflow(code):
    err = FakeProviderError("request too large", status_code=413, code=code)
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


@pytest.mark.parametrize(
    ("message", "status"),
    [
        ("timeout", 400),
        ("rate limit", 403),
        ("timeout", 401),
    ],
)
def test_explicit_non_retryable_status_overrides_transient_wording(message, status):
    assert is_retryable_error(FakeProviderError(message, status_code=status)) is False


def test_response_non_retryable_status_overrides_transient_wording():
    assert is_retryable_error(FakeErrorWithResponse("timeout", status_code=400)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 502 - {'error': 'upstream_request_failed'}",
        "Responses stream failed: upstream_request_failed",
        "upstream request failed before completion",
        "Connection error.",
        "Connection reset by peer",
        "Connection refused",
        "Server disconnected without sending a response",
        "Our service encountered an error. You can retry your request.",
        "The service is busy. Please try again later.",
    ],
)
def test_relay_upstream_failures_without_status_attribute_are_retryable(message):
    assert is_retryable_error(RuntimeError(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 400 - invalid request",
        "Error code: 400 - upstream_request_failed",
        "Error code: 400 - please try again later",
        "HTTP 400 Bad Request: please try again later",
        "HTTP/1.1 403 overloaded",
        "Error code: 403 - rate limit",
        "Status code 401: timeout",
    ],
)
def test_non_retryable_status_embedded_in_message_overrides_transient_wording(message):
    assert is_retryable_error(RuntimeError(message)) is False


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


@pytest.mark.asyncio
async def test_retry_backoff_caps_long_provider_outages(monkeypatch):
    attempts = 0
    delays = []

    async def fail_until_recovered():
        nonlocal attempts
        attempts += 1
        if attempts < 10:
            raise FakeProviderError("overloaded", status_code=502)
        return "ok"

    async def record_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(retry_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(retry_module.random, "uniform", lambda _low, _high: 0.0)

    assert await with_retry(fail_until_recovered, max_retries=20) == "ok"
    assert attempts == 10
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_retry_recovers_from_relay_error_without_status_attribute(monkeypatch):
    attempts = 0

    async def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Error code: 502 - {'error': 'upstream_request_failed'}")
        return "ok"

    async def skip_delay(_delay):
        return None

    monkeypatch.setattr(retry_module.asyncio, "sleep", skip_delay)
    assert await with_retry(fail_once, max_retries=1) == "ok"
    assert attempts == 2
