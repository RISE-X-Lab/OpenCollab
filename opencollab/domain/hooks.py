"""Hook policy — event vocabulary + pure matching.

A hook binds a lifecycle event (CC-style: ``PreToolUse``, ``PostToolUse``,
``SessionStart``, ``Stop``, ``SubagentStop``, ``Notification``) to an action
(phase 1: a shell ``command``). ``HookSpec`` is the resolved, in-memory form of
one config entry; ``match_hooks`` is the pure selection rule the runner applies
on every event.

This module is policy only — no I/O, no SDK or outer-layer imports — so it sits
at the center of the dependency graph alongside the other ``domain`` modules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fnmatch import fnmatch

# CC-style lifecycle event names a hook may bind to. Validated at config load so
# an unknown key fails fast rather than silently never firing.
HOOK_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "PreToolUse",
        "PostToolUse",
        "SessionStart",
        "Stop",
        "SubagentStop",
        "Notification",
    }
)

# Events whose payload carries a tool name and therefore support a ``matcher``.
TOOL_SCOPED_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})

# Recognized action vocabulary and the subset that the installed runner can
# execute. Keeping both here gives configuration and runtime one source of truth
# while reserving future names without accepting inert configurations.
HOOK_ACTION_TYPES: frozenset[str] = frozenset({"command", "prompt", "agent"})
EXECUTABLE_HOOK_ACTION_TYPES: frozenset[str] = frozenset({"command"})


@dataclass(frozen=True)
class HookSpec:
    """One resolved hook binding.

    ``matcher`` is a tool-name glob (``fnmatch``) for tool-scoped events;
    ``None`` matches every tool / applies to non-tool events. ``action_type`` is
    the executor key (``"command"`` in phase 1; ``"prompt"``/``"agent"``
    reserved). ``timeout`` bounds a command so a slow hook cannot stall an agent.
    """

    event: str
    action_type: str
    command: str
    matcher: str | None = None
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if isinstance(self.timeout, bool):
            raise ValueError("hook timeout must be a finite positive number")
        try:
            timeout = float(self.timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("hook timeout must be a finite positive number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("hook timeout must be a finite positive number")
        object.__setattr__(self, "timeout", timeout)


@dataclass(frozen=True)
class HookOutcome:
    """Result of firing a hook. Phase-1 observe-only wiring always allows; the
    field exists so phase-2 blocking (PreToolUse deny) needs no signature change.
    """

    allow: bool = True
    feedback: str | None = None


def match_hooks(
    specs: tuple[HookSpec, ...],
    event_name: str,
    tool_name: str | None = None,
) -> list[HookSpec]:
    """Select the specs bound to ``event_name`` whose matcher admits ``tool_name``.

    A spec with no ``matcher`` always matches. A spec with a ``matcher`` matches
    only when ``tool_name`` is given and globs against it — so a tool-scoped hook
    never fires for an event that carries no tool.
    """
    selected: list[HookSpec] = []
    for spec in specs:
        if spec.event != event_name:
            continue
        if spec.matcher is None:
            selected.append(spec)
        elif tool_name is not None and fnmatch(tool_name, spec.matcher):
            selected.append(spec)
    return selected


__all__ = [
    "HOOK_EVENT_NAMES",
    "HOOK_ACTION_TYPES",
    "EXECUTABLE_HOOK_ACTION_TYPES",
    "TOOL_SCOPED_EVENTS",
    "HookSpec",
    "HookOutcome",
    "match_hooks",
]
