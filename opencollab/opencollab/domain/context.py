"""Pure domain for layered context assembly — no I/O, no asyncio.

The agent's context is not a single concatenated string but an editable bundle
of *sources*, each tagged with which layer it belongs to (identity, team,
project, task, skill) and where it lands structurally (the system prompt vs. a
user-context message).

A ``ContextBuilder`` (in bootstrap) emits an ordered ``ContextPlan``; the plan
turns its sources into provider-shaped messages. Adding a new kind of context
is registering a new ``ContextSource`` — the assembly in ``ContextPlan`` is
generic over ``ContextPosition`` and never special-cases a specific layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextLayer(Enum):
    """Which conceptual layer a context source belongs to."""

    IDENTITY = "identity"      # who the agent is (role prompt)
    TEAM = "team"              # topology-aware "your team" section
    PROJECT = "project"        # repo/project conventions (a repo map)
    TASK = "task"              # the concrete assignment (DelegationTask / first turn)
    SKILL = "skill"            # model-invocable skill catalog (name + description)


class ContextPosition(Enum):
    """Where a source lands in the provider-facing message structure."""

    SYSTEM = "system"              # folded into the single system prompt
    USER_CONTEXT = "user_context"  # injected as its own user message


# Default keep/shed priority per layer: higher = more essential (kept longer).
# This is the scale that makes layers *load-bearing* for context budgeting — the
# reactive shapers (``application.shaping``) pin sources at/above their
# ``PIN_FLOOR`` and shed lower layers cheapest-first under pressure. Pure data;
# the shaper owns the floor, the domain owns the ranking.
#
# Today only the ``PIN_FLOOR=70`` cut is load-bearing: identity/team/skill/task
# all sit above it, so every startup source is pinned and nothing is sheddable.
# The sub-floor ordering (PROJECT below) becomes live only once deferred
# long-term sources (memory/RAG) actually carry content — an honest gap today.
LAYER_PRIORITY: dict[ContextLayer, int] = {
    ContextLayer.IDENTITY: 100,
    ContextLayer.TEAM: 90,
    ContextLayer.SKILL: 85,
    ContextLayer.TASK: 80,
    ContextLayer.PROJECT: 30,
}


@dataclass(frozen=True)
class ContextSource:
    """One tagged context fragment.

    ``priority`` overrides the layer default (``LAYER_PRIORITY``) for this one
    source; ``None`` means "use the layer's". A source with empty ``content`` is
    skipped during assembly.
    """

    name: str
    layer: ContextLayer
    position: ContextPosition
    content: str = ""
    priority: int | None = None

    @property
    def effective_priority(self) -> int:
        """Resolved priority: the explicit override, else the layer default."""
        if self.priority is not None:
            return self.priority
        return LAYER_PRIORITY.get(self.layer, 0)


@dataclass(frozen=True)
class ContextPlan:
    """An ordered set of sources plus the rules for assembling them.

    Assembly is generic over ``ContextPosition``: all SYSTEM sources are joined
    into one system message, all USER_CONTEXT sources become user messages, in
    source order. No method inspects ``ContextLayer`` — that is what keeps "new
    context = register a source" true.
    """

    sources: tuple[ContextSource, ...] = field(default_factory=tuple)

    def _startup(self, position: ContextPosition) -> tuple[ContextSource, ...]:
        """The non-empty sources at ``position``, in source order."""
        return tuple(
            s for s in self.sources if s.position is position and s.content
        )

    def system_prompt(self) -> str:
        """The assembled system message body (identity + team + …, in order)."""
        return "\n\n".join(s.content for s in self._startup(ContextPosition.SYSTEM))

    def startup_user_messages(self) -> list[dict[str, Any]]:
        """Startup user-context sources as provider-shaped user messages.

        Each message is stamped with an internal ``_ctx`` tag (layer + resolved
        priority) so the reactive shapers can pin or shed it by provenance once
        it is flattened into the live history. Providers reconstruct messages
        from ``role``/``content`` and ignore the extra key (same convention as
        ``tool_call_id`` and the auto-compact ``compacted`` flag).
        """
        return [
            {
                "role": "user",
                "content": s.content,
                "_ctx": {"layer": s.layer.value, "priority": s.effective_priority},
            }
            for s in self._startup(ContextPosition.USER_CONTEXT)
        ]


__all__ = [
    "ContextLayer",
    "ContextPosition",
    "LAYER_PRIORITY",
    "ContextSource",
    "ContextPlan",
]
