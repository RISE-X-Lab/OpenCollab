"""Public structural tool contract and built-in tool composition.

Pass custom implementations directly to ``OpenCollab.agent``; built-in tools
can be selected by preset name or composed explicitly for workflow roles.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, TypeAlias, runtime_checkable

from opencollab.application.ports import ToolPort as Tool
from opencollab.bootstrap.tool_registry import build_tools_for_role

BuiltinToolName: TypeAlias = Literal[
    "bash",
    "file_read",
    "file_write",
    "apply_patch",
    "run_tests",
    "git_diff",
    "grep",
]

_BUILTIN_TOOL_NAMES = frozenset(
    {
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "git_diff",
        "grep",
    }
)


@runtime_checkable
class VerificationTool(Tool, Protocol):
    """A test tool exposing targets with a latest parser-backed green verdict."""

    @property
    def verified_targets(self) -> frozenset[str]: ...


def builtin_tools(
    *names: BuiltinToolName,
    headless: bool = True,
    allow_file_creation: bool = True,
    limits: Mapping[str, Mapping[str, int]] | None = None,
) -> tuple[Tool, ...]:
    """Build a fresh ordered set of public, stateless tools.

    Headless composition requires process isolation for shell and test
    execution, and disables model-supplied test runner overrides. Coordination
    and human-interaction tools remain scheduler-owned and are not available
    through this helper.
    """
    if not isinstance(headless, bool):
        raise ValueError("headless must be a boolean")
    if not isinstance(allow_file_creation, bool):
        raise ValueError("allow_file_creation must be a boolean")
    unsupported = sorted({name for name in names if name not in _BUILTIN_TOOL_NAMES})
    if unsupported:
        raise ValueError(f"unsupported built-in tools: {unsupported}")
    if len(set(names)) != len(names):
        raise ValueError("built-in tool names must be unique")
    if limits is not None and not isinstance(limits, Mapping):
        raise ValueError("limits must be a mapping")
    unselected_limits = sorted(set(limits or {}) - set(names))
    if unselected_limits:
        raise ValueError(
            f"limits reference unselected built-in tools: {unselected_limits}"
        )
    for name, values in (limits or {}).items():
        if not isinstance(values, Mapping):
            raise ValueError(f"limits for {name!r} must be a mapping")
    normalized_limits = {
        name: dict(values) for name, values in (limits or {}).items()
    }
    return tuple(
        build_tools_for_role(
            list(names),
            interactive=not headless,
            allow_file_creation=allow_file_creation,
            tool_limits=normalized_limits,
        )
    )


__all__ = ["BuiltinToolName", "Tool", "VerificationTool", "builtin_tools"]
