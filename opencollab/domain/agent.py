"""Agent — stateless configuration template.

First Principle: Agent = LLM config + System Prompt + Tools.
Agent holds NO state. State lives in Session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.domain.identity import validate_role_identity
from opencollab.domain.tools import ToolSpec

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
    api_key: str | None = None
    base_url: str | None = None
    max_tokens_per_step: int = DEFAULT_MAX_TOKENS_PER_STEP
    temperature: float = 0.0
    top_p: float | None = None
    thinking: bool = False
    thinking_params: dict = field(default_factory=dict)
    tool_choice: str | None = None

    def __post_init__(self) -> None:
        self.name = validate_role_identity(self.name)

    def tool_schemas(self) -> list[dict]:
        """Generate OpenAI-format tool schemas for LLM function calling."""
        return [t.to_openai_schema() for t in self.tools]

    def find_tool(self, name: str) -> ToolSpec | None:
        """Case-insensitive tool lookup (ref: opencode's tool repair logic)."""
        for t in self.tools:
            if t.name.lower() == name.lower():
                return t
        return None
