"""Transport retry classification tests."""

from __future__ import annotations

from opencollab.adapters.llm.retry import is_retryable_error


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


class APIError(Exception):
    """Stand-in for the SDK base error a gateway refusal arrives as.

    The real ``openai.APIError`` carries ``message``/``body``/``request`` but no
    ``status_code``: the concurrency meter sits in front of the provider, so
    there is no upstream HTTP response to take a status from.
    """


class BadRequestError(Exception):
    """Stand-in for a 4xx the SDK does attach a status to."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# The four provider-error wordings that ended a seat session in
# luna-cmdprimary40 (2026-09-05, gpt-5.6-luna, 91 error terminals).
LUNA_CONCURRENCY_REFUSAL = "Concurrency limit exceeded for user, please retry later"
LUNA_HTTP2_STREAM_FAILURE = "Upstream HTTP/2 stream failed"
LUNA_503 = (
    "Error code: 503 - {'error': {'message': 'Service temporarily unavailable', "
    "'type': 'api_error'}}"
)
LUNA_429 = (
    "Error code: 429 - {'error': {'message': 'Upstream rate limit exceeded, "
    "please retry later', 'type': 'rate_limit_error'}}"
)


def test_statusless_gateway_concurrency_refusal_is_retryable():
    assert is_retryable_error(APIError(LUNA_CONCURRENCY_REFUSAL)) is True


def test_statusless_gateway_stream_failure_is_retryable():
    assert is_retryable_error(APIError(LUNA_HTTP2_STREAM_FAILURE)) is True


def test_gateway_refusal_reached_through_the_cause_chain_is_retryable():
    wrapper = RuntimeError("provider request failed")
    wrapper.__cause__ = APIError(LUNA_CONCURRENCY_REFUSAL)

    assert is_retryable_error(wrapper) is True


def test_gateway_503_and_429_stay_retryable_by_status():
    assert is_retryable_error(BadRequestError(LUNA_503, 503)) is True
    assert is_retryable_error(BadRequestError(LUNA_429, 429)) is True


def test_application_error_naming_a_concurrency_limit_is_not_retryable():
    # No provider-SDK class, so the gateway fragments must not apply.
    assert is_retryable_error(ValueError("concurrency limit in candidate schema")) is False


def test_gateway_fragment_on_a_4xx_is_not_retryable():
    # The class matches but the error carries a status, so the status set --
    # which excludes 400 -- decides it. Guards the "no HTTP status" half of the
    # gate: without it a 400 that merely says "please retry later" would be
    # retried forever.
    error = APIError("Invalid tool schema; please retry later")
    error.status_code = 400

    assert is_retryable_error(error) is False


def test_auth_failure_is_not_retryable_even_when_it_invites_a_retry():
    error = APIError("Incorrect API key provided, try again later")
    error.status_code = 401

    assert is_retryable_error(error) is False


def test_context_overflow_is_not_retryable_even_with_a_gateway_fragment():
    error = APIError(
        "This model's maximum context length is 128000 tokens; please retry later"
    )
    error.status_code = 400

    assert is_retryable_error(error) is False


def test_statusless_provider_error_without_a_gateway_fragment_is_not_retryable():
    # The class gate alone must not make every bare APIError retryable.
    assert is_retryable_error(APIError("Invalid value for 'tools[0].function'")) is False
