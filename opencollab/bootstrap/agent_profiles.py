"""Explicit agent profiles resolved by the SDK composition root."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from opencollab.application.ports import LLMPort, SafetyPolicyPort, ShaperPort

from .single2_prompt import SINGLE2_SYSTEM_PROMPT

_PROFILE_TOOL_LIMITS: contextvars.ContextVar[Mapping[str, Mapping[str, int]] | None] = (
    contextvars.ContextVar("agent_profile_tool_limits", default=None)
)


@dataclass(frozen=True, slots=True)
class SingleAgentProfile:
    """Per-session choices that must not alter teams or the default agent."""

    name: str
    system_prompt: str
    default_steps: int
    resolve_tools: Callable[[str | Sequence[Any] | None], tuple[Any, ...]]
    build_shaper: Callable[[LLMPort, Any], ShaperPort]
    wrap_safety: Callable[[SafetyPolicyPort | None, str], SafetyPolicyPort]
    honor_explicit_limits: bool = False
    tool_limits: Mapping[str, Mapping[str, int]] = field(
        default_factory=lambda: MappingProxyType({})
    )


def _single2_tools(value: str | Sequence[Any] | None) -> tuple[Any, ...]:
    from opencollab.adapters.tools.single2 import build_single2_tools
    from opencollab.bootstrap.programmatic import resolve_tools

    if isinstance(value, str) and value in {"coding", "read"}:
        tools = build_single2_tools()
        if value == "coding":
            return tools
        by_name = {tool.name: tool for tool in tools}
        return tuple(by_name[name] for name in ("file_read", "grep", "git_diff"))
    return resolve_tools(value)


def _single2_shaper(llm: LLMPort, summarizer: Any) -> ShaperPort:
    from opencollab.bootstrap.container import _build_default_shaper

    return _build_default_shaper(
        llm,
        summarizer,
        preserve_tool_result_tail=True,
    )


def resolve_agent_profile(name: str | None) -> SingleAgentProfile | None:
    """Resolve an opt-in profile without constructing shared mutable tools."""
    if name is None or name == "default":
        return None
    if name != "single2":
        raise ValueError("profile must be 'default' or 'single2'")

    from opencollab.adapters.single2_safety import wrap_single2_safety
    from opencollab.adapters.tools.single2 import SINGLE2_BASH_OUTPUT_CHARS

    return SingleAgentProfile(
        name="single2",
        system_prompt=SINGLE2_SYSTEM_PROMPT,
        default_steps=200,
        resolve_tools=_single2_tools,
        build_shaper=_single2_shaper,
        wrap_safety=wrap_single2_safety,
        honor_explicit_limits=True,
        tool_limits=MappingProxyType({
            "bash": MappingProxyType({"max_output_chars": SINGLE2_BASH_OUTPUT_CHARS})
        }),
    )


__all__ = ["SingleAgentProfile", "resolve_agent_profile"]
