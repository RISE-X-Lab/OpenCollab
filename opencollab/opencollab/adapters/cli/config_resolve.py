"""CLI config resolution: merge CLI args with .env defaults + API-key checks."""

from __future__ import annotations

import os
from typing import Any

from rich.console import Console


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def _required_env_key(provider: str | None) -> str:
    p = (provider or "openai").lower()
    return "ANTHROPIC_API_KEY" if p == "anthropic" else "OPENAI_API_KEY"


def _missing_api_key(provider: str | None, api_key: str | None) -> bool:
    if api_key:
        return False
    return not bool(os.environ.get(_required_env_key(provider)))


def _print_missing_key_hint(
    console: Console, provider: str | None, base_url: str | None = None
) -> None:
    from opencollab.adapters.tui import TUI

    tui = TUI(console)
    tui.print_welcome()
    env_key = _required_env_key(provider)
    accepted = f"OPENCOLLAB_API_KEY or {env_key}"
    if base_url and "dashscope" in base_url.lower() and env_key != "DASHSCOPE_API_KEY":
        accepted += " or DASHSCOPE_API_KEY"
    console.print(
        f"[red]Missing API key[/red]: pass [bold]--api-key[/bold] or set [bold]{accepted}[/bold]."
    )


def _resolve_config(workspace: str, model: str | None, provider: str | None,
                     api_key: str | None, base_url: str | None, budget: int | None) -> dict:
    """Merge CLI args with .env defaults. CLI args take precedence."""
    from opencollab.bootstrap.config import get_config
    cfg = get_config(workspace)
    return {
        "model": model or cfg["model"],
        "provider": provider or cfg["provider"],
        "api_key": api_key or cfg["api_key"],
        "base_url": base_url or cfg["base_url"],
        "budget": budget if budget is not None else _safe_int(cfg["budget"], 200_000),
        "llm_timeout": cfg["llm_timeout"],
        "filter_messages": bool(cfg["filter_messages"]),
    }
