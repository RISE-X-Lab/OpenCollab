"""The context policy a team file names for every session it seats.

A policy decides which shaper layers reshape a session's history before each
model call. ``default`` is the lazy-degradation pipeline every session ran
before the policy could be named, so a team file that omits ``context`` runs
exactly what it ran before. ``no_history_compaction`` keeps the per-result
budget, which bounds any one tool result, and turns off the three
pressure-triggered history layers (clear old tool output, snip old turns,
summarize), so the model sees its whole history until the provider's window
refuses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CONTEXT_POLICIES = ("default", "no_history_compaction")
_POLICY_KEYS = frozenset({"policy", "tool_result_budget"})


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """A named shaper policy, with the one parameter a team may set."""

    name: str = "default"
    #: Character cap on any one tool result; ``None`` keeps the built-in cap.
    tool_result_budget: int | None = None

    def __post_init__(self) -> None:
        if self.name not in CONTEXT_POLICIES:
            raise ValueError(
                f"unknown context policy {self.name!r}. Choose one of {list(CONTEXT_POLICIES)}"
            )
        budget = self.tool_result_budget
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0):
            raise ValueError("context tool_result_budget must be a positive integer")

    @property
    def history_compaction(self) -> bool:
        return self.name == "default"

    def shaper_options(self) -> dict[str, Any]:
        """Keyword arguments for ``_build_default_shaper`` that realize this policy."""
        options: dict[str, Any] = {"history_compaction": self.history_compaction}
        if self.tool_result_budget is not None:
            options["tool_result_budget"] = self.tool_result_budget
        return options


def _normalize_name(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def resolve_context_policy(value: object) -> ContextPolicy:
    """Read a team file's ``context`` entry: absent, a name, or a mapping."""
    if value is None:
        return ContextPolicy()
    if isinstance(value, ContextPolicy):
        return value
    if isinstance(value, str):
        return ContextPolicy(name=_normalize_name(value))
    if isinstance(value, dict):
        unknown = sorted(str(key) for key in value if key not in _POLICY_KEYS)
        if unknown:
            raise ValueError(
                f"unknown context keys {unknown}. Allowed: {sorted(_POLICY_KEYS)}"
            )
        name = value.get("policy", "default")
        if not isinstance(name, str):
            raise ValueError("context policy must be a name")
        return ContextPolicy(
            name=_normalize_name(name),
            tool_result_budget=value.get("tool_result_budget"),
        )
    raise ValueError("context must be a policy name or a mapping")


__all__ = ["CONTEXT_POLICIES", "ContextPolicy", "resolve_context_policy"]
