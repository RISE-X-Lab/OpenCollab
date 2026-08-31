"""Agent — stateless configuration template.

First Principle: Agent = LLM config + System Prompt + Tools.
Agent holds NO state. State lives in Session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opencollab.domain.identity import validate_role_identity
from opencollab.domain.tools import (
    ToolSpec,
    tool_name_collision_key,
    validate_unique_tool_names,
)

DEFAULT_MAX_TOKENS_PER_STEP = 8_192


@dataclass
class Agent:
    """A stateless agent definition. Reusable across multiple Sessions.

    Attributes:
        name: Human-readable identifier (e.g., "lead", "coder", "reviewer").
        system_prompt: The system prompt that defines this agent's behavior.
        tools: List of Tool instances this agent is allowed to use.
        model: LLM model identifier (e.g., "claude-sonnet-4-20250514").
        provider: LLM provider ("openai", "anthropic", or any OpenAI-compatible).
        api_key: Override API key for this agent (defaults to env var).
        base_url: Override base URL (for proxies, local models, etc.).
        max_tokens_per_step: Max output tokens per LLM call.
        temperature: Sampling temperature.
        top_p: Nucleus-sampling top_p (``None`` keeps the provider default, so
            the request is unchanged; an explicit 0..1 value sends the knob).
        thinking: Enable provider "thinking"/reasoning passthrough (default off).
        thinking_params: Provider-native request parameters used when
            ``thinking`` is on.
        tool_choice: Optional override for the provider ``tool_choice`` (e.g.
            ``"required"`` to force a tool call). ``None`` keeps the provider
            default ("auto") — every ordinary agent leaves this unset.
    """

    name: str
    system_prompt: str
    tools: list[ToolSpec] = field(default_factory=list)
    model: str = "gpt-4o"
    provider: str = "openai"
    wire_protocol: str = "chat_completions"
    api_key: str | None = None
    base_url: str | None = None
    context_window: int | None = None
    max_tokens_per_step: int = DEFAULT_MAX_TOKENS_PER_STEP
    temperature: float = 0.0
    top_p: float | None = None
    thinking: bool = False
    thinking_params: dict = field(default_factory=dict)
    reasoning_effort: str | None = None
    llm_connect_timeout: float = 30.0
    llm_first_event_timeout: float = 180.0
    llm_stream_idle_timeout: float = 180.0
    tool_choice: Any = None
    llm_max_retries: int = 3
    provider_error_time_budget: float = 0.0
    reasoning_effort_policy: str = "configured"
    # Consume chat completions as a stream. OFF by default: streaming is the
    # only way some endpoints return reasoning text, but it also changes the
    # request body, so it stays opt-in and per-run. Appended last so the
    # legacy positional field order is untouched.
    llm_stream_chat: bool = False

    def __post_init__(self) -> None:
        self.name = validate_role_identity(self.name)
        self._validate_tool_names()

    def _validate_tool_names(self) -> None:
        names = [
            name
            for tool in self.tools
            if isinstance((name := getattr(tool, "name", None)), str)
        ]
        validate_unique_tool_names(names)

    def tool_schemas(self) -> list[dict]:
        """Generate OpenAI-format tool schemas for LLM function calling."""
        self._validate_tool_names()
        return [t.to_openai_schema() for t in self.tools]

    def find_tool(self, name: str) -> ToolSpec | None:
        """Case-insensitive tool lookup (ref: opencode's tool repair logic)."""
        self._validate_tool_names()
        try:
            key = tool_name_collision_key(name)
        except ValueError:
            return None
        for t in self.tools:
            if tool_name_collision_key(t.name) == key:
                return t
        return None
