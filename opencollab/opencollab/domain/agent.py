"""Agent — stateless configuration template.

First Principle: Agent = LLM config + System Prompt + Tools.
Agent holds NO state. State lives in Session.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.domain.tools import ToolSpec


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
    """

    name: str
    system_prompt: str
    tools: list[ToolSpec] = field(default_factory=list)
    model: str = "gpt-4o"
    provider: str = "openai"
    api_key: str | None = None
    base_url: str | None = None
    max_tokens_per_step: int = 8192
    temperature: float = 0.0

    def tool_schemas(self) -> list[dict]:
        """Generate OpenAI-format tool schemas for LLM function calling."""
        return [t.to_openai_schema() for t in self.tools]

    def find_tool(self, name: str) -> ToolSpec | None:
        """Case-insensitive tool lookup (ref: opencode's tool repair logic)."""
        for t in self.tools:
            if t.name.lower() == name.lower():
                return t
        return None
