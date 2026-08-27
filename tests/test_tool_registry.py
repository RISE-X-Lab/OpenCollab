"""Unit tests for build_tools_for_role skill wiring + fail-fast resolution."""

from __future__ import annotations

import pytest

from opencollab.adapters.tools.use_skill import UseSkillTool
from opencollab.bootstrap.tool_registry import (
    KNOWN_TOOL_NAMES,
    SKILL_TOOL_FACTORIES,
    build_tools_for_role,
)
from opencollab.domain.skill import SkillManifest


class FakeSkillStore:
    def list_manifests(self) -> tuple[SkillManifest, ...]:
        return ()

    def get_body(self, name: str) -> str | None:
        return None


def test_use_skill_is_a_known_tool_name():
    assert "use_skill" in KNOWN_TOOL_NAMES
    assert "use_skill" in SKILL_TOOL_FACTORIES


def test_build_wires_use_skill_when_store_provided():
    store = FakeSkillStore()
    tools = build_tools_for_role(["bash", "use_skill"], skill_store=store)
    by_name = {t.name: t for t in tools}
    assert "use_skill" in by_name
    assert isinstance(by_name["use_skill"], UseSkillTool)
    # The dispatcher is bound to the provided store (construction injection).
    assert by_name["use_skill"]._store is store


def test_build_raises_when_use_skill_requested_without_store():
    with pytest.raises(ValueError, match="requires a skill store"):
        build_tools_for_role(["use_skill"], skill_store=None)


def test_unknown_tool_error_lists_known_tools_including_skill():
    with pytest.raises(ValueError) as exc:
        build_tools_for_role(["frobnicate"])
    msg = str(exc.value)
    assert "Unknown tool 'frobnicate'" in msg
    assert "use_skill" in msg


@pytest.mark.parametrize(
    "tool_names",
    [
        ["bash", "bash"],
        ["bash", "BASH"],
        ["bash", "ｂａｓｈ"],
    ],
)
def test_build_rejects_duplicate_tool_names_before_resolution(tool_names):
    with pytest.raises(ValueError, match="duplicate tool names"):
        build_tools_for_role(tool_names)


def test_stateless_tools_resolve_without_any_dependency():
    tools = build_tools_for_role(["bash", "file_read"])
    assert {t.name for t in tools} == {"bash", "file_read"}


def test_a_registry_denied_an_unisolated_shell_restricts_command_tools():
    bash, run_tests = build_tools_for_role(
        ["bash", "run_tests"], allow_unisolated_shell=False
    )

    assert bash.require_process_isolation is True
    assert run_tests.require_process_isolation is True
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_a_denied_shell_can_explicitly_allow_only_unisolated_tests():
    bash, run_tests = build_tools_for_role(
        ["bash", "run_tests"],
        allow_unisolated_shell=False,
        allow_unisolated_tests=True,
    )

    assert bash.require_process_isolation is True
    assert run_tests.require_process_isolation is False
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_an_allowed_unisolated_shell_keeps_user_command_controls():
    bash, run_tests = build_tools_for_role(
        ["bash", "run_tests"], allow_unisolated_shell=True
    )

    assert bash.require_process_isolation is False
    assert run_tests.require_process_isolation is False
    assert run_tests.allow_runner_override is True
    assert run_tests.allow_extra_args is True


def test_the_shell_decision_is_independent_of_the_ask_user_decision():
    """The split itself: neither input may move the other's answer.

    These were one ``interactive`` flag, and that is what left a prebuilt
    team's peers without a shell — they are non-entry roles, so they were built
    with ``ask_user`` off, and the same flag then took ``bash`` away.
    """
    tools = build_tools_for_role(
        ["ask_user", "bash"],
        ask_user_available=False,
        allow_unisolated_shell=True,
    )
    assert [t.name for t in tools] == ["bash"]
    assert tools[0].require_process_isolation is False

    ask_user, bash = build_tools_for_role(
        ["ask_user", "bash"],
        ask_user_available=True,
        allow_unisolated_shell=False,
    )
    assert (ask_user.name, bash.name) == ("ask_user", "bash")
    assert bash.require_process_isolation is True
