"""CLI config resolution: merge CLI args with .env defaults + API-key checks.

Provider→env-key knowledge (which keys satisfy a provider, which is missing)
lives in ``bootstrap.config``; this module only merges CLI args and renders the
missing-key hint.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from opencollab.bootstrap.config import accepted_api_key_envs, missing_api_key


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value) if value else default
    except (TypeError, ValueError):
        return default


def missing_api_key_for(provider: str | None, api_key: str | None, base_url: str | None = None) -> bool:
    return missing_api_key(provider, api_key, base_url)


def print_missing_key_hint(
    console: Console, provider: str | None, base_url: str | None = None
) -> None:
    from opencollab.adapters.tui import TUI

    tui = TUI(console)
    tui.print_welcome()
    accepted = " or ".join(accepted_api_key_envs(provider, base_url))
    console.print(
        f"[red]Missing API key[/red]: pass [bold]--api-key[/bold] or set [bold]{accepted}[/bold]."
    )


def resolve_config(workspace: str, model: str | None, provider: str | None,
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
        "temperature": cfg["temperature"],
        "thinking": cfg["thinking"],
        "thinking_params": cfg["thinking_params"],
        "llm_timeout": cfg["llm_timeout"],
        "filter_messages": bool(cfg["filter_messages"]),
    }
