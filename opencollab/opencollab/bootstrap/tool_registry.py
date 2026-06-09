"""Tool name -> concrete Tool resolution.

The team config declares each role's tools by *name*; this module owns the
mapping from those names to concrete ``Tool`` instances and the curated name
sets used elsewhere in the composition root (coordination tools that gate the
team prompt section, and the bulky read-only tools whose old results may be
cleared in place by ``ToolOutputClearShaper``).
"""

from __future__ import annotations

from typing import Any, Callable

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.human import AskUserTool
from opencollab.adapters.tools.message import MessageAgentTool, TeamStatusTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.tools.spawn import SpawnAgentTool, SpawnWithReviewTool

# Tool name -> factory. Stateless tools need nothing; scheduler-bound tools take
# the scheduler so an agent can spawn/message via the SchedulerPort.
STATELESS_TOOL_FACTORIES: dict[str, Callable[[], Tool]] = {
    "bash": BashTool,
    "file_read": FileReadTool,
    "file_write": FileWriteTool,
    "apply_patch": ApplyPatchTool,
    "run_tests": RunTestsTool,
    "git_diff": GitDiffTool,
    "grep": GrepTool,
    "ask_user": AskUserTool,
}
SCHEDULER_TOOL_FACTORIES: dict[str, Callable[[Any], Tool]] = {
    "spawn_agent": SpawnAgentTool,
    "spawn_with_review": SpawnWithReviewTool,
    "message_agent": MessageAgentTool,
    "team_status": TeamStatusTool,
}
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(STATELESS_TOOL_FACTORIES) | frozenset(SCHEDULER_TOOL_FACTORIES)
# Tools that let a role act on teammates — used to decide whether to render the
# topology-aware "Your team" prompt section.
COORDINATION_TOOL_NAMES: frozenset[str] = frozenset(SCHEDULER_TOOL_FACTORIES)
# Bulky, reconstructible read-only tool outputs whose OLD results may be cleared
# in place by ``ToolOutputClearShaper``. Intersected with the real registry so a
# renamed/removed tool drops out automatically (driven from real names, not a
# hardcoded library set). Edits/writes and coordination tools are excluded.
COMPACTABLE_TOOL_NAMES: frozenset[str] = (
    frozenset({"bash", "file_read", "grep", "git_diff", "run_tests"}) & KNOWN_TOOL_NAMES
)


def build_tools_for_role(
    tool_names: list[str],
    *,
    scheduler: Any = None,
    interactive: bool = False,
) -> list[Tool]:
    """Resolve tool names to Tool instances.

    ``ask_user`` is dropped in non-interactive (headless) mode. Scheduler-bound
    tools require a ``scheduler``. Unknown names raise — fail fast at startup.
    """
    tools: list[Tool] = []
    for name in tool_names:
        if name == "ask_user" and not interactive:
            continue
        if name in STATELESS_TOOL_FACTORIES:
            tools.append(STATELESS_TOOL_FACTORIES[name]())
        elif name in SCHEDULER_TOOL_FACTORIES:
            if scheduler is None:
                raise ValueError(
                    f"Tool '{name}' requires a scheduler but none was provided."
                )
            tools.append(SCHEDULER_TOOL_FACTORIES[name](scheduler))
        else:
            raise ValueError(
                f"Unknown tool '{name}' in team config. "
                f"Known tools: {sorted(KNOWN_TOOL_NAMES)}"
            )
    return tools


def build_default_tools(*, include_ask_user: bool = False) -> list[Tool]:
    """Canonical tool bundle: bash, file_read, file_write, grep, [ask_user]."""
    tools: list[Tool] = [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        GrepTool(),
    ]
    if include_ask_user:
        tools.append(AskUserTool())
    return tools


__all__ = [
    "STATELESS_TOOL_FACTORIES",
    "SCHEDULER_TOOL_FACTORIES",
    "KNOWN_TOOL_NAMES",
    "COORDINATION_TOOL_NAMES",
    "COMPACTABLE_TOOL_NAMES",
    "build_tools_for_role",
    "build_default_tools",
]
