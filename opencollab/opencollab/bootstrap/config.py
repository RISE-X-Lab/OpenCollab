"""Pydantic-backed configuration loading.

Reads configs/.env from the current working directory or workspace, then falls
back to legacy .env files. Env-file parsing is built in, so python-dotenv is
not required.

Supported variables:
    OPENCOLLAB_MODEL      — default LLM model (e.g., "claude-sonnet-4-20250514")
    OPENCOLLAB_PROVIDER   — LLM provider ("openai", "anthropic")
    OPENCOLLAB_API_KEY    — API key (also reads OPENAI_API_KEY /
                            ANTHROPIC_API_KEY / DASHSCOPE_API_KEY)
    OPENCOLLAB_BASE_URL   — API base URL (also reads OPENAI_BASE_URL)
    OPENCOLLAB_BUDGET     — default token budget
    OPENCOLLAB_FILTER_MESSAGES — TUI: show only the selected agent's stream (bool)
    OPENCOLLAB_CONFIG_FILE — explicit path to an env file
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OpenCollabConfig(BaseModel):
    """Runtime configuration for OpenCollab."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(default="gpt-4o", min_length=1)
    provider: str = Field(default="openai", min_length=1)
    api_key: str | None = Field(default=None, repr=False)
    base_url: str | None = None
    budget: int = Field(default=200_000, ge=1)
    filter_messages: bool = Field(default=False)

    @field_validator("model", "provider", mode="before")
    @classmethod
    def _strip_required_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return value.lower()

    @field_validator("api_key", "base_url", mode="before")
    @classmethod
    def _empty_string_as_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def load_dotenv(path: str | None = None) -> dict[str, str]:
    """Parse a .env file into a dict. Handles quotes and comments."""
    env_path = path or os.path.join(os.getcwd(), ".env")
    result: dict[str, str] = {}

    if not os.path.isfile(env_path):
        return result

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value

    return result


def _candidate_env_paths(workspace: str | None = None) -> list[str]:
    """Return config file candidates in priority order."""
    explicit = os.environ.get("OPENCOLLAB_CONFIG_FILE")
    if explicit:
        return [explicit]

    bases: list[Path] = []
    if workspace:
        bases.append(Path(workspace))
    bases.append(Path.cwd())

    paths: list[str] = []
    seen: set[str] = set()
    for base in bases:
        for candidate in (base / "configs" / ".env", base / ".env"):
            key = str(candidate.resolve() if candidate.exists() else candidate.absolute())
            if key not in seen:
                paths.append(str(candidate))
                seen.add(key)
    return paths


def load_config_env(workspace: str | None = None) -> dict[str, str]:
    """Load env-file defaults from configs/.env and legacy .env locations.

    Earlier paths have higher priority.
    """
    values: dict[str, str] = {}
    for path in _candidate_env_paths(workspace):
        for key, value in load_dotenv(path).items():
            values.setdefault(key, value)
    return values


def build_config(workspace: str | None = None, overrides: dict[str, Any] | None = None) -> OpenCollabConfig:
    """Build and validate runtime configuration.

    Priority: environment variable > configs/.env > .env > built-in default
    """
    dotenv = load_config_env(workspace)

    def resolve(key: str, *fallback_keys: str, default: str | None = None) -> str | None:
        # Check env vars first (highest priority)
        val = os.environ.get(key)
        if val:
            return val
        for fk in fallback_keys:
            val = os.environ.get(fk)
            if val:
                return val
        # Check .env file
        val = dotenv.get(key)
        if val:
            return val
        for fk in fallback_keys:
            val = dotenv.get(fk)
            if val:
                return val
        return default

    def resolve_ordered(*keys: str, default: str | None = None) -> str | None:
        # For provider-specific secrets, key specificity matters more than
        # source. This prevents a generic exported OPENAI_API_KEY from being sent
        # to a provider-specific compatible endpoint such as DashScope.
        for key in keys:
            val = os.environ.get(key)
            if val:
                return val
            val = dotenv.get(key)
            if val:
                return val
        return default

    provider_value = resolve("OPENCOLLAB_PROVIDER", default="openai")
    base_url_value = resolve("OPENCOLLAB_BASE_URL", "OPENAI_BASE_URL")
    base_url_lower = (base_url_value or "").lower()
    if "dashscope" in base_url_lower or "aliyuncs" in base_url_lower:
        api_key_value = resolve_ordered(
            "DASHSCOPE_API_KEY",
            "OPENCOLLAB_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        )
    elif (provider_value or "").lower() == "anthropic":
        api_key_value = resolve_ordered(
            "ANTHROPIC_API_KEY",
            "OPENCOLLAB_API_KEY",
            "OPENAI_API_KEY",
            "DASHSCOPE_API_KEY",
        )
    else:
        api_key_value = resolve_ordered(
            "OPENCOLLAB_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "DASHSCOPE_API_KEY",
        )

    values: dict[str, Any] = {
        "model": resolve("OPENCOLLAB_MODEL", default="gpt-4o"),
        "provider": provider_value,
        "api_key": api_key_value,
        "base_url": base_url_value,
        "budget": resolve("OPENCOLLAB_BUDGET", default="200000"),
        "filter_messages": resolve("OPENCOLLAB_FILTER_MESSAGES", default="false"),
    }
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})
    return OpenCollabConfig.model_validate(values)


def get_config(workspace: str | None = None) -> dict[str, Any]:
    """Get resolved configuration as a dict for existing callers."""
    return build_config(workspace).model_dump()
