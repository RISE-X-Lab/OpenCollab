"""Configured model construction and persisted-session inspection."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.storage import SessionStore
from opencollab.application.session_snapshot import decode_anthropic_history
from opencollab.bootstrap.config import resolve_model_route


def configured_model_client(
    config: Mapping[str, Any], *, client_type: type[Any] | None = None,
) -> Any:
    """Create an independently owned transport with the resolved configuration."""
    config = resolve_model_route(config)
    return (client_type or LLMClient)(
        model=config["model"],
        provider=config["provider"],
        api_key=config.get("api_key"),
        **({"auth_token": config["auth_token"]} if config.get("auth_token") else {}),
        base_url=config.get("base_url"),
        **({"request_pool": config["request_pool"], "request_capacity": config["request_capacity"]}
           if config.get("request_pool") is not None else {}),
        wire_protocol=config.get("wire_protocol", "chat_completions"),
        context_window=config.get("context_window"),
        max_retries=config.get("llm_max_retries", 3),
        request_timeout=config.get("llm_timeout", 600.0),
        connect_timeout=config.get("llm_connect_timeout", 30.0),
        first_event_timeout=config.get("llm_first_event_timeout", 180.0),
        stream_idle_timeout=config.get("llm_stream_idle_timeout", 180.0),
        stream_chat=bool(config.get("llm_stream_chat", False)),
        provider_error_time_budget=config.get("provider_error_time_budget", 0.0),
    )

def read_session_snapshot(path: str | Path, password: str = "") -> dict[str, Any]:
    """Read the native snapshot and journal through their owning storage adapter."""
    snapshot = SessionStore().load_snapshot(str(path), password)
    state = snapshot.get("session_state")
    history = state.get("anthropic_history") if isinstance(state, dict) else None
    if isinstance(history, dict):
        decoded = decode_anthropic_history(history, snapshot["messages"])
        if decoded is not None:
            history["messages"] = copy.deepcopy(decoded["messages"])
    return snapshot
