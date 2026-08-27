"""Tool name -> concrete Tool resolution.

The team config declares each role's tools by *name*; this module owns the
mapping from those names to concrete ``Tool`` instances and the curated name
sets used elsewhere in the composition root (coordination tools that gate the
team prompt section, and the bulky read-only tools whose old results may be
cleared in place by ``ToolOutputClearShaper``).
"""

from __future__ import annotations

from typing import Callable

from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.human import AskUserTool
from opencollab.adapters.tools.message import MessageAgentTool, TeamStatusTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.tools.spawn import SpawnAgentTool, SpawnWithReviewTool
from opencollab.adapters.tools.use_skill import UseSkillTool
from opencollab.application.ports import SchedulerPort, SkillStorePort
from opencollab.domain.tools import validate_unique_tool_names

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
SCHEDULER_TOOL_FACTORIES: dict[str, Callable[[SchedulerPort], Tool]] = {
    "spawn_agent": SpawnAgentTool,
    "spawn_with_review": SpawnWithReviewTool,
    "message_agent": MessageAgentTool,
    "team_status": TeamStatusTool,
}
# Skill-bound tools take a ``SkillStorePort`` so the dispatcher can fetch a
# skill's body by name. One generic dispatcher serves all skills.
SKILL_TOOL_FACTORIES: dict[str, Callable[[SkillStorePort], Tool]] = {
    "use_skill": UseSkillTool,
}
KNOWN_TOOL_NAMES: frozenset[str] = (
    frozenset(STATELESS_TOOL_FACTORIES)
    | frozenset(SCHEDULER_TOOL_FACTORIES)
    | frozenset(SKILL_TOOL_FACTORIES)
)
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
MAX_CONFIGURED_TOOL_OUTPUT_CHARS = 10_000_000
TOOL_LIMIT_FIELDS: dict[str, frozenset[str]] = {
    "bash": frozenset({"max_output_chars"}),
    "git_diff": frozenset({"max_diff_chars", "max_status_chars"}),
    "run_tests": frozenset({"max_traceback_chars"}),
    "file_read": frozenset({"max_read_chars"}),
    "grep": frozenset({"max_grep_chars"}),
}


def validate_tool_limits(
    raw: object,
) -> dict[str, dict[str, int]]:
    """Validate output caps without bool coercion or constructor-key leakage."""
    if not isinstance(raw, dict):
        raise ValueError("tool_limits must be a mapping")
    normalized: dict[str, dict[str, int]] = {}
    for tool_name, kwargs in raw.items():
        if not isinstance(tool_name, str) or tool_name not in KNOWN_TOOL_NAMES:
            raise ValueError(f"tool_limits names unknown tools [{tool_name!r}]")
        if not isinstance(kwargs, dict):
            raise ValueError(f"tool_limits for '{tool_name}' must be a mapping")
        if tool_name in SCHEDULER_TOOL_FACTORIES:
            raise ValueError(
                f"tool_limits not supported for coordination tools ['{tool_name}']."
            )
        allowed = TOOL_LIMIT_FIELDS.get(tool_name, frozenset())
        unsupported = set(kwargs) - allowed
        if unsupported:
            raise ValueError(
                f"tool_limits for '{tool_name}' has unsupported keys "
                f"{sorted(unsupported)}"
            )
        normalized_kwargs: dict[str, int] = {}
        for key, value in kwargs.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value > MAX_CONFIGURED_TOOL_OUTPUT_CHARS
            ):
                raise ValueError(
                    f"tool_limits {tool_name}.{key} must be an integer in "
                    f"1..{MAX_CONFIGURED_TOOL_OUTPUT_CHARS}"
                )
            normalized_kwargs[key] = value
        normalized[tool_name] = normalized_kwargs
    return normalized


def build_tools_for_role(
    tool_names: list[str],
    *,
    scheduler: SchedulerPort | None = None,
    skill_store: SkillStorePort | None = None,
    ask_user_available: bool = False,
    allow_unisolated_shell: bool = False,
    allow_unisolated_tests: bool = False,
    allow_file_creation: bool = True,
    tool_limits: dict[str, dict[str, int]] | None = None,
) -> list[Tool]:
    """Resolve tool names to Tool instances.

    Two capability questions reach this function, and they are deliberately two
    parameters rather than one:

    * ``ask_user_available`` — is there a human this agent may put a question
      to? ``ask_user`` is dropped when there is not.
    * ``allow_unisolated_shell`` — may this agent execute commands the OS does
      not sandbox? It sets ``bash``'s ``require_process_isolation`` (and the
      matching controls on ``run_tests``, which also spawns a process).

    They used to be one ``interactive`` flag, because one fact — "a human is
    sitting at this run" — happened to answer both: a human can be asked a
    question, and a human can be shown a risky command before it runs, which is
    what let the shell run outside an OS sandbox. A prebuilt team pulls them
    apart: its teammates are declared in the team file and seated before the
    first model call, so they must get the entry agent's shell, while
    ``ask_user`` still belongs to the entry role alone — a peer has no human to
    ask. Folding them back into one boolean would hand every teammate a shell
    the entry agent does not have, or take away one it does.

    ``allow_unisolated_tests`` stays the narrower, tests-only relaxation of the
    same guard on ``run_tests``. Scheduler-bound tools require a ``scheduler``;
    skill-bound tools require a ``skill_store``. ``tool_limits`` maps a tool name
    to constructor kwargs (output caps) so a team file can tune per-tool output
    budgets to its backend. Unknown names or kwargs raise — fail fast at startup.
    """
    validate_unique_tool_names(tool_names)
    limits = validate_tool_limits(tool_limits or {})
    uncappable = set(limits) & frozenset(SCHEDULER_TOOL_FACTORIES)
    if uncappable:
        raise ValueError(
            f"tool_limits not supported for coordination tools {sorted(uncappable)}."
        )
    tools: list[Tool] = []
    for name in tool_names:
        if name == "ask_user" and not ask_user_available:
            continue
        if name in STATELESS_TOOL_FACTORIES:
            tools.append(
                _instantiate(
                    name,
                    STATELESS_TOOL_FACTORIES[name],
                    limits,
                    allow_unisolated_shell=allow_unisolated_shell,
                    allow_unisolated_tests=allow_unisolated_tests,
                    allow_file_creation=allow_file_creation,
                )
            )
        elif name in SCHEDULER_TOOL_FACTORIES:
            if scheduler is None:
                raise ValueError(
                    f"Tool '{name}' requires a scheduler but none was provided."
                )
            tools.append(SCHEDULER_TOOL_FACTORIES[name](scheduler))
        elif name in SKILL_TOOL_FACTORIES:
            if skill_store is None:
                raise ValueError(
                    f"Tool '{name}' requires a skill store but none was provided."
                )
            tools.append(SKILL_TOOL_FACTORIES[name](skill_store))
        else:
            raise ValueError(
                f"Unknown tool '{name}' in team config. "
                f"Known tools: {sorted(KNOWN_TOOL_NAMES)}"
            )
    return tools


def _instantiate(
    name: str,
    factory: Callable[..., Tool],
    limits: dict[str, dict[str, int]],
    *,
    allow_unisolated_shell: bool,
    allow_unisolated_tests: bool,
    allow_file_creation: bool,
) -> Tool:
    """Build a stateless tool, applying any configured limit kwargs.

    Both command-running tools read ``allow_unisolated_shell``: a run that may
    not open an unsandboxed shell may not reach one through the test runner
    either. ``allow_unisolated_tests`` can lift the sandbox requirement for
    ``run_tests`` alone, and never lifts it for ``bash``.
    """
    kwargs: dict[str, object] = dict(limits.get(name, {}))
    if name == "bash":
        kwargs["require_process_isolation"] = not allow_unisolated_shell
    elif name == "run_tests":
        kwargs.update(
            allow_runner_override=allow_unisolated_shell,
            allow_extra_args=allow_unisolated_shell,
            require_process_isolation=not (
                allow_unisolated_shell or allow_unisolated_tests
            ),
        )
    if name == "file_write":
        kwargs["allow_create"] = allow_file_creation
    try:
        return factory(**kwargs)
    except TypeError as e:
        raise ValueError(
            f"tool_limits for '{name}' has unsupported keys {sorted(kwargs)}: {e}"
        ) from e


__all__ = [
    "STATELESS_TOOL_FACTORIES",
    "SCHEDULER_TOOL_FACTORIES",
    "SKILL_TOOL_FACTORIES",
    "KNOWN_TOOL_NAMES",
    "COORDINATION_TOOL_NAMES",
    "COMPACTABLE_TOOL_NAMES",
    "validate_tool_limits",
    "build_tools_for_role",
]
