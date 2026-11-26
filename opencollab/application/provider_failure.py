def provider_http_failure_metadata(exc: Exception) -> tuple[int | None, str | None]:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None

    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = body if isinstance(body, dict) else {}
    error_type = error.get("type")
    if (
        isinstance(error_type, str)
        and 0 < len(error_type) <= 128
        and all(char.isalnum() or char in "._-" for char in error_type)
    ):
        return status_code, error_type
    return status_code, None
