"""Transport retry classification tests."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from anthropic import AnthropicError
from openai import APIError, OpenAIError

from opencollab.adapters.llm import retry
from opencollab.adapters.llm.retry import is_retryable_error, with_retry


class APIConnectionError(Exception):
    """Small stand-in for an SDK connection wrapper."""


class APITimeoutError(Exception):
    """Small stand-in for an SDK timeout wrapper."""


def test_sdk_connection_wrapper_is_retryable_without_http_status():
    assert is_retryable_error(APIConnectionError("Connection error.")) is True


def test_sdk_timeout_wrapper_is_retryable_without_http_status():
    assert is_retryable_error(APITimeoutError("request stalled")) is True


def test_transport_cause_chain_is_retryable():
    wrapper = RuntimeError("provider request failed")
    wrapper.__cause__ = ConnectionResetError("connection reset by peer")

    assert is_retryable_error(wrapper) is True


def test_unrelated_application_error_is_not_retryable():
    assert is_retryable_error(ValueError("connection error in candidate schema")) is False


def test_anthropic_overloaded_status_is_retryable():
    error = RuntimeError("provider unavailable")
    error.status_code = 529

    assert is_retryable_error(error) is True


def _sdk_error(message: str) -> APIError:
    request = httpx.Request("POST", "http://provider.test/chat/completions")
    return APIError(message, request=request, body=None)


@pytest.mark.parametrize(
    "message",
    [
        "Concurrency limit exceeded for user, please retry later",
        "Too many concurrent requests",
        "Upstream HTTP/2 stream failed",
    ],
)
def test_statusless_sdk_gateway_refusal_is_retryable(message):
    assert is_retryable_error(_sdk_error(message)) is True


def test_statusless_sdk_gateway_refusal_in_cause_chain_is_retryable():
    wrapper = RuntimeError("provider request failed")
    wrapper.__cause__ = _sdk_error("Concurrency limit exceeded for user")

    assert is_retryable_error(wrapper) is True


@pytest.mark.parametrize("error_class", [OpenAIError, AnthropicError])
def test_statusless_provider_base_error_is_retryable(error_class):
    assert is_retryable_error(error_class("Concurrency limit exceeded for user")) is True


@pytest.mark.parametrize("status", [400, 401, 404])
@pytest.mark.parametrize("status_location", ["direct", "response", "wrapper", "cause"])
def test_gateway_wording_keeps_explicit_client_errors_non_retryable(status, status_location):
    error = _sdk_error("Concurrency limit exceeded for user, please retry later")
    if status_location == "direct":
        error.status_code = status
    elif status_location == "response":
        error.response = SimpleNamespace(status_code=status)
    elif status_location == "wrapper":
        wrapper = RuntimeError("provider request failed")
        wrapper.status_code = status
        wrapper.__cause__ = error
        error = wrapper
    else:
        cause = RuntimeError("provider rejected request")
        cause.status_code = status
        error.__cause__ = cause

    assert is_retryable_error(error) is False


@pytest.mark.parametrize("status", [429, 503, 529])
def test_gateway_refusal_keeps_retryable_statuses(status):
    error = _sdk_error("Concurrency limit exceeded for user")
    error.status_code = status

    assert is_retryable_error(error) is True


@pytest.mark.parametrize(
    "message",
    ["concurrency limit in candidate schema", "Upstream HTTP/2 stream failed"],
)
def test_application_gateway_wording_is_not_retryable(message):
    assert is_retryable_error(ValueError(message)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Invalid value for tools[0].function",
        "Invalid tool schema, please retry later",
        "Incorrect API key provided, try again later",
    ],
)
def test_statusless_sdk_error_without_gateway_wording_is_not_retryable(message):
    assert is_retryable_error(_sdk_error(message)) is False


def test_context_overflow_keeps_gateway_wording_non_retryable():
    error = _sdk_error("This model's maximum context length is 128000 tokens, please retry later")
    error.status_code = 400

    assert is_retryable_error(error) is False


@pytest.mark.asyncio
async def test_statusless_gateway_refusal_retries_the_request(monkeypatch):
    attempts = 0
    delays = []

    async def request():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _sdk_error("Concurrency limit exceeded for user, please retry later")
        return "ok"

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(retry.asyncio, "sleep", sleep)
    monkeypatch.setattr(retry.random, "uniform", lambda *_: 0.0)

    assert await with_retry(request, max_retries=1) == "ok"
    assert attempts == 2
    assert delays == [1.0]


@pytest.mark.asyncio
async def test_explicit_client_error_keeps_request_attempt_count(monkeypatch):
    attempts = 0

    async def request():
        nonlocal attempts
        attempts += 1
        error = _sdk_error("Concurrency limit exceeded for user, please retry later")
        error.status_code = 400
        raise error

    async def sleep(delay):
        pytest.fail(f"unexpected retry delay {delay}")

    monkeypatch.setattr(retry.asyncio, "sleep", sleep)

    with pytest.raises(APIError):
        await with_retry(request, max_retries=1)
    assert attempts == 1


@pytest.mark.asyncio
async def test_statusless_gateway_refusal_obeys_retry_limit(monkeypatch):
    attempts = 0

    async def request():
        nonlocal attempts
        attempts += 1
        raise _sdk_error("Upstream HTTP/2 stream failed")

    async def sleep(delay):
        pass

    monkeypatch.setattr(retry.asyncio, "sleep", sleep)

    with pytest.raises(APIError):
        await with_retry(request, max_retries=1)
    assert attempts == 2
