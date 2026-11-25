"""Compose native tools with test execution observation."""

from opencollab.adapters.tools.evidence import BashEvidence, has_pass_evidence
from opencollab.application.ports import ToolPort


def observe_test_evidence(tools: tuple[ToolPort, ...]) -> tuple[ToolPort, ...]:
    """Share native edit observations with Bash's retained execution history."""
    bash = next((BashEvidence(tool) for tool in tools if tool.name == "bash"), None)
    if bash is None:
        return tools
    return tuple(
        bash if tool.name == "bash" else bash.observe_edits(tool)
        if tool.name in {"file_write", "apply_patch"} else tool
        for tool in tools
    )

__all__ = ["BashEvidence", "has_pass_evidence", "observe_test_evidence"]
