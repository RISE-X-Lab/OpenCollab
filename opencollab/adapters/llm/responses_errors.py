"""Typed errors emitted while parsing OpenAI Responses streams."""

from __future__ import annotations

from opencollab.adapters.llm.errors import TransientEmptyOutputError, TransientProviderError

_TRANSIENT_RESPONSE_CODES = frozenset(
    {
        "account_wait_queue_full",
        "accounts_temporarily_unavailable",
        "gateway_queue_full",
        "rate_limit_error",
        "rate_limit_exceeded",
        "server_error",
        "upstream_error",
        "vector_store_timeout",
    }
)
_TRANSIENT_RESPONSE_MESSAGES = (
    "eligible openai accounts are temporarily unavailable",
    "our servers are currently overloaded",
    "rate limit exceeded",
    "temporarily unavailable",
    "too many pending requests",
    "upstream http/2 stream failed",
)


class ResponsesProtocolError(RuntimeError):
    """The Responses endpoint returned an incomplete or invalid event sequence."""


class ResponsesTerminalEventError(ResponsesProtocolError):
    """A typed terminal event preserved its provider error identity."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.body = {"error": {"code": code, "message": message}}


class ResponsesEmptyOutputError(ResponsesProtocolError, TransientEmptyOutputError):
    """A completed Responses request contained no usable assistant output."""


class ResponsesStreamInterruptedError(ResponsesProtocolError, TransientProviderError):
    """A Responses stream ended without its required terminal event."""


class ResponsesTransientEventError(ResponsesTerminalEventError, TransientProviderError):
    """A typed Responses error identified a temporary provider failure."""
