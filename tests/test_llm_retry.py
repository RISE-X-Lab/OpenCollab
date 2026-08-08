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
