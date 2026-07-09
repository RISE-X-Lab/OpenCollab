"""Append-only API usage ledger for provider calls."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from opencollab.adapters.llm.types import LLMResponse, Usage

DEFAULT_GLM52_INPUT_USD_PER_MTOK = 1.4
DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK = 0.26
DEFAULT_GLM52_OUTPUT_USD_PER_MTOK = 4.4
SECRET_ENV_NAME_PARTS = ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "CLIENT_TOKEN", "SECRET")
URL_RE = re.compile(r"https?://[^\s'\"<>]+")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)"
    r"((?:api[-_ ]?key|auth[-_ ]?token|access[-_ ]?token|client[-_ ]?token|token|secret|password)"
    r"\s*[:=]\s*)[^\s,;'\")\]}]+"
)


def usage_log_path() -> Path | None:
    raw = os.environ.get("OPENCOLLAB_API_USAGE_LOG")
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return Path(stripped).expanduser()


def _float_env(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def pricing_for_model(model: str | None) -> dict[str, float | str]:
    lowered = (model or "").lower()
    if "glm" in lowered:
        input_price = _float_env("GLM_INPUT_USD_PER_MTOK", DEFAULT_GLM52_INPUT_USD_PER_MTOK)
        return {
            "mode": "glm-5.2-default",
            "input_usd_per_mtok": input_price,
            "cached_input_usd_per_mtok": _float_env(
                "GLM_CACHED_INPUT_USD_PER_MTOK", DEFAULT_GLM52_CACHED_INPUT_USD_PER_MTOK
            ),
            "cache_creation_usd_per_mtok": _float_env(
                "GLM_CACHE_CREATION_USD_PER_MTOK",
                input_price,
            ),
            "output_usd_per_mtok": _float_env(
                "GLM_OUTPUT_USD_PER_MTOK", DEFAULT_GLM52_OUTPUT_USD_PER_MTOK
            ),
        }
    input_price = _float_env("OPENCOLLAB_INPUT_USD_PER_MTOK", 0.0)
    return {
        "mode": "unset",
        "input_usd_per_mtok": input_price,
        "cached_input_usd_per_mtok": _float_env("OPENCOLLAB_CACHED_INPUT_USD_PER_MTOK", 0.0),
        "cache_creation_usd_per_mtok": _float_env(
            "OPENCOLLAB_CACHE_CREATION_USD_PER_MTOK",
            input_price,
        ),
        "output_usd_per_mtok": _float_env("OPENCOLLAB_OUTPUT_USD_PER_MTOK", 0.0),
    }


def usage_cost_usd(usage: Usage, model: str | None) -> float:
    pricing = pricing_for_model(model)
    cached = max(int(getattr(usage, "cache_read_tokens", 0) or 0), 0)
    cache_creation = max(int(getattr(usage, "cache_creation_tokens", 0) or 0), 0)
    uncached_input = max(int(usage.input_tokens or 0) - cached - cache_creation, 0)
    input_price = float(pricing["input_usd_per_mtok"])
    cached_price = float(pricing["cached_input_usd_per_mtok"])
    cache_creation_price = float(pricing["cache_creation_usd_per_mtok"])
    output_price = float(pricing["output_usd_per_mtok"])
    return (
        uncached_input / 1_000_000 * input_price
        + cached / 1_000_000 * cached_price
        + cache_creation / 1_000_000 * cache_creation_price
        + int(usage.output_tokens or 0) / 1_000_000 * output_price
    )


def _safe_base_url(base_url: str | None) -> dict[str, str | None]:
    if not base_url:
        return {"base_url": None, "base_url_host": None}
    parsed = urlparse(base_url)
    if parsed.scheme and parsed.hostname:
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        host_with_port = f"{host}:{port}" if port is not None else host
        safe = f"{parsed.scheme}://{host_with_port}"
        return {"base_url": safe, "base_url_host": host_with_port}
    return {"base_url": "(unparsed)", "base_url_host": None}


def _redact_secrets(text: str) -> str:
    redacted = URL_RE.sub(lambda match: str(_safe_base_url(match.group(0))["base_url"]), text)
    for name, value in list(os.environ.items()):
        if not value or len(value) < 8:
            continue
        upper = name.upper()
        if any(part in upper for part in SECRET_ENV_NAME_PARTS):
            redacted = redacted.replace(value, "[redacted]")
    redacted = re.sub(
        r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}",
        r"\1[redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)((?:api[-_ ]?key|auth[-_ ]?token|access[-_ ]?token)\s*[:=]\s*)\S+",
        r"\1[redacted]",
        redacted,
    )
    redacted = SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", redacted)
    return redacted


def _usage_payload(usage: Usage, model: str | None) -> dict[str, Any]:
    cached = max(int(getattr(usage, "cache_read_tokens", 0) or 0), 0)
    cache_creation = max(int(getattr(usage, "cache_creation_tokens", 0) or 0), 0)
    input_tokens = int(usage.input_tokens or 0)
    payload: dict[str, Any] = {
        "input_tokens": input_tokens,
        "uncached_input_tokens": max(input_tokens - cached - cache_creation, 0),
        "cached_input_tokens": cached,
        "cache_creation_tokens": cache_creation,
        "output_tokens": int(usage.output_tokens or 0),
        "total_tokens": int(usage.total_tokens or 0),
        "estimated": bool(getattr(usage, "estimated", False)),
        "cost_usd": usage_cost_usd(usage, model),
        "pricing": pricing_for_model(model),
    }
    raw_usage = getattr(usage, "raw_usage", None)
    if raw_usage:
        payload["raw_usage"] = raw_usage
    return payload


def build_usage_record(
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    latency_s: float,
    status: str,
    response: LLMResponse | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    usage = response.usage if response is not None else Usage()
    record: dict[str, Any] = {
        "schema": "opencollab.api_usage.v1",
        "timestamp": time.time(),
        "request_id": str(uuid.uuid4()),
        "status": status,
        "provider": provider,
        "model": model,
        "latency_s": round(latency_s, 4),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "argv0": Path(sys.argv[0]).name if sys.argv else None,
        "run_id": os.environ.get("OPENCOLLAB_RUN_ID") or os.environ.get("OPENCOLLAB_USAGE_RUN_ID"),
        "label": os.environ.get("OPENCOLLAB_USAGE_LABEL"),
        **_safe_base_url(base_url),
        "usage": _usage_payload(usage, model),
    }
    if response is not None:
        record["finish_reason"] = response.finish_reason
    if error is not None:
        record["error"] = {
            "type": type(error).__name__,
            "message": _redact_secrets(str(error))[:1000],
        }
    return record


def append_usage_record(record: dict[str, Any], path: Path | None = None) -> None:
    target = path if path is not None else usage_log_path()
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        return


def record_api_usage(
    *,
    provider: str | None,
    model: str | None,
    base_url: str | None,
    latency_s: float,
    status: str,
    response: LLMResponse | None = None,
    error: BaseException | None = None,
) -> None:
    try:
        append_usage_record(
            build_usage_record(
                provider=provider,
                model=model,
                base_url=base_url,
                latency_s=latency_s,
                status=status,
                response=response,
                error=error,
            )
        )
    except Exception:
        return


__all__ = [
    "append_usage_record",
    "build_usage_record",
    "pricing_for_model",
    "record_api_usage",
    "usage_cost_usd",
    "usage_log_path",
]
