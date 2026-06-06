"""Pure domain for layered context assembly — no I/O, no asyncio.

The agent's context is not a single concatenated string but an editable bundle
of *sources*, each tagged with which layer it belongs to (identity, team,
project, memory, task, tool-meta), when it loads (startup vs. later), and where
it lands structurally (the system prompt vs. a user-context message).

A ``ContextBuilder`` (in bootstrap) emits an ordered ``ContextPlan``; the plan
knows how to turn its STARTUP sources into provider-shaped messages. Adding a
new kind of context is registering a new ``ContextSource`` — the assembly in
``ContextPlan`` is generic over ``ContextPosition`` and never special-cases a
specific layer.

Non-startup sources (memory, lazy tool schemas, project conventions) are
*registered* this period via ``loader_key`` but not loaded: ``ContextPlan``
exposes them through ``deferred_sources`` without injecting any content, so a
later lazy-loading feature only adds sources and never touches assembly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextLayer(Enum):
    """Which conceptual layer a context source belongs to."""

    IDENTITY = "identity"      # who the agent is (role prompt)
    TEAM = "team"              # topology-aware "your team" section
    PROJECT = "project"        # repo/project conventions
    MEMORY = "memory"          # recalled cross-session memory
    TASK = "task"              # the concrete assignment (DelegationTask / first turn)
    TOOL_META = "tool_meta"    # tool schemas / usage notes


class LoadTiming(Enum):
    """When a source is loaded into the context window."""

    STARTUP = "startup"                    # assembled immediately at build time
    PER_TURN = "per_turn"                  # accumulated each user turn (future)
    DURING_EXECUTION = "during_execution"  # injected mid-run (future)
    ON_DEMAND = "on_demand"                # lazily loaded when referenced (future)


class ContextPosition(Enum):
    """Where a source lands in the provider-facing message structure."""

    SYSTEM = "system"              # folded into the single system prompt
    USER_CONTEXT = "user_context"  # injected as its own user message


@dataclass(frozen=True)
class ContextSource:
    """One tagged context fragment.

    ``content`` is populated only for ``STARTUP`` sources; deferred sources
    carry a ``loader_key`` placeholder (registered, not yet loaded). ``visible``
    is reserved for transparency/audit — a future shaper can record what it
    dropped or replaced per source.
    """

    name: str
    layer: ContextLayer
    timing: LoadTiming
    position: ContextPosition
    content: str = ""
    visible: bool = True
    loader_key: str | None = None


@dataclass(frozen=True)
class ContextPlan:
    """An ordered set of sources plus the rules for assembling startup ones.

    Assembly is generic over ``ContextPosition``: all STARTUP+SYSTEM sources are
    joined into one system message, all STARTUP+USER_CONTEXT sources become
    user messages, in source order. No method inspects ``ContextLayer`` — that
    is what keeps "new context = register a source" true.
    """

    sources: tuple[ContextSource, ...] = field(default_factory=tuple)

    def _startup(self, position: ContextPosition) -> tuple[ContextSource, ...]:
        return tuple(
            s
            for s in self.sources
            if s.timing is LoadTiming.STARTUP
            and s.position is position
            and s.content
        )

    def system_prompt(self) -> str:
        """The assembled system message body (identity + team, in order)."""
        return "\n\n".join(s.content for s in self._startup(ContextPosition.SYSTEM))

    def startup_user_messages(self) -> list[dict[str, Any]]:
        """Startup user-context sources as provider-shaped user messages."""
        return [
            {"role": "user", "content": s.content}
            for s in self._startup(ContextPosition.USER_CONTEXT)
        ]

    def messages(self) -> list[dict[str, Any]]:
        """The full startup seed: the system message followed by user-context."""
        return [
            {"role": "system", "content": self.system_prompt()},
            *self.startup_user_messages(),
        ]

    def deferred_sources(self) -> tuple[ContextSource, ...]:
        """Sources registered for a non-startup timing — not loaded this period."""
        return tuple(s for s in self.sources if s.timing is not LoadTiming.STARTUP)


__all__ = [
    "ContextLayer",
    "LoadTiming",
    "ContextPosition",
    "ContextSource",
    "ContextPlan",
]
