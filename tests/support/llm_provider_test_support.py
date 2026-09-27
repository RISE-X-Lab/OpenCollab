"""Shared OpenAI-compatible response fixture for the provider test modules."""

from __future__ import annotations

from types import SimpleNamespace


def _openai_resp(usage, content="hello world", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage)
