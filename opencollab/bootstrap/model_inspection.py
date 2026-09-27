"""Offline inspection of the model settings used by the runtime."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from opencollab.adapters.llm.errors import is_context_overflow_error
from opencollab.adapters.llm.openai_provider import _uses_reasoning_request_fields
from opencollab.adapters.llm.retry import is_retryable_error
from opencollab.adapters.llm.types import model_capabilities
from opencollab.adapters.llm.usage_ledger import pricing_for_model
from opencollab.application.shaping.pipeline import history_trigger_target


def _sample_exception(sample: dict[str, Any]) -> Exception:
    error_type = type(str(sample.get("class_name") or "Exception"), (Exception,), {})
    error = error_type(str(sample.get("message") or ""))
    if sample.get("status") is not None:
        error.status_code = sample["status"]
    return error


def inspect_model_runtime(
    model: str,
    *,
    overflow_samples: list[dict[str, Any]] | None = None,
    retry_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe model settings and classify supplied errors without a request.

    Samples contain ``label``, ``message``, optional ``status`` and an optional
    ``class_name``. Class names label local exception stand-ins; they are never
    imported. This inspects the installed runtime's capability table and the
    pricing mode selected by the current environment. Request-field flags
    describe Chat Completions.
    """
    capabilities = model_capabilities(model)
    trigger, target = history_trigger_target(capabilities.context_window)
    reasoning = _uses_reasoning_request_fields(model)
    return {
        "capabilities": asdict(capabilities),
        "history": {
            "trigger": trigger,
            "target": target,
            "from": "context_window" if capabilities.context_window else "fixed_default",
        },
        "reasoning_request_fields": reasoning,
        "max_output_token_field": "max_completion_tokens" if reasoning else "max_tokens",
        "sends_temperature": not reasoning,
        "sends_top_p": not reasoning,
        "pricing_mode": pricing_for_model(model)["mode"],
        "overflow": {
            str(sample["label"]): is_context_overflow_error(_sample_exception(sample))
            for sample in overflow_samples or ()
        },
        "retry": {
            str(sample["label"]): is_retryable_error(_sample_exception(sample))
            for sample in retry_samples or ()
        },
    }
