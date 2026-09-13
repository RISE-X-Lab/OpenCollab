"""Configured model construction and persisted-session inspection."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.storage import SessionStore


def configured_model_client(config: Mapping[str, Any]) -> LLMClient:
    """Create an independently owned transport with the resolved configuration."""
    return LLMClient(
        model=config["model"],
        provider=config["provider"],
        api_key=config.get("api_key"),
        base_url=config.get("base_url"),
        wire_protocol=config.get("wire_protocol", "chat_completions"),
        context_window=config.get("context_window"),
        max_retries=config.get("llm_max_retries", 3),
        request_timeout=config.get("llm_timeout", 600.0),
        connect_timeout=config.get("llm_connect_timeout", 30.0),
        first_event_timeout=config.get("llm_first_event_timeout", 180.0),
        stream_idle_timeout=config.get("llm_stream_idle_timeout", 180.0),
        provider_error_time_budget=config.get("provider_error_time_budget", 0.0),
    )


def read_session_snapshot(path: str | Path, password: str = "") -> dict[str, Any]:
    """Read the native snapshot and journal through their owning storage adapter."""
    return SessionStore().load_snapshot(str(path), password)
