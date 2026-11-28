import re

from opencollab.application.ports import ProviderTerminalEventError

_OUTPUT_UPPER_BOUND = re.compile(
    r"\b(?:at most|less than or equal to|maximum allowed value is)\s+([0-9]{1,10})\b(?![.,][0-9])",
    re.IGNORECASE,
)


def _provider_error_body(exc: Exception) -> dict:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else body


def provider_http_failure_metadata(exc: Exception) -> tuple[int | None, str | None]:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None

    error_type = _provider_error_body(exc).get("type")
    if (
        isinstance(error_type, str)
        and 0 < len(error_type) <= 128
        and all(char.isalnum() or char in "._-" for char in error_type)
    ):
        return status_code, error_type
    return status_code, None


def provider_rejected_output_limit(exc: Exception, *, retry_limit: int) -> bool:
    status_code, _ = provider_http_failure_metadata(exc)
    if status_code != 400 or isinstance(exc, ProviderTerminalEventError):
        return False
    body = _provider_error_body(exc)
    param = getattr(exc, "param", None)
    if not isinstance(param, str):
        param = body.get("param")
    if param not in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        return False
    code = getattr(exc, "code", None)
    if code is None:
        code = body.get("code")
    if code == "integer_above_max_value":
        return True
    if code not in (None, "invalid_value"):
        return False
    message = body.get("message")
    bound = _OUTPUT_UPPER_BOUND.search(message) if isinstance(message, str) else None
    return bound is not None and 0 < int(bound[1]) < retry_limit
