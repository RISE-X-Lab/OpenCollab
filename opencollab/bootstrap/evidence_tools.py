"""Compose native tools with test execution observation."""

from opencollab.adapters.tools.evidence import BashEvidence, has_pass_evidence
from opencollab.application.ports import ToolPort


def observe_test_evidence(tools: tuple[ToolPort, ...]) -> tuple[ToolPort, ...]:
    """Wrap Bash while retaining each native tool's configured behavior."""
    return tuple(BashEvidence(tool) if tool.name == "bash" else tool for tool in tools)


__all__ = ["BashEvidence", "has_pass_evidence", "observe_test_evidence"]
