"""Typed Responses failures and transient provider error identifiers."""

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


class ResponsesEmptyOutputError(ResponsesProtocolError, TransientEmptyOutputError):
    """A completed Responses request contained no usable assistant output."""


class ResponsesStreamInterruptedError(ResponsesProtocolError, TransientProviderError):
    """A Responses stream ended without its required terminal event."""


class ResponsesTransientEventError(ResponsesProtocolError, TransientProviderError):
    """A typed Responses error identified a temporary provider failure."""
