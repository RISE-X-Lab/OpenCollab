"""Per-tool output caps configurable from the team file (``tool_limits``).

The YAML ``tool_limits`` section maps tool name -> constructor kwargs; the
registry applies them when building a role's tools, so a team can tune output
budgets to its backend's context size. Unknown tools/kwargs fail fast.
"""

import asyncio

import pytest
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools._output import truncate
from opencollab.adapters.tools.bash import MAX_OUTPUT_CHARS
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.team_config import _build_team_config
from opencollab.bootstrap.tool_registry import build_tools_for_role


def run(coro):
    return asyncio.run(coro)


def _team(data, base_dir):
    return _build_team_config(data, base_dir)


def test_team_config_parses_tool_limits(tmp_path):
    cfg = _team(
        {
            "roles": {"lead": {"prompt": "p", "tools": ["bash"]}},
            "tool_limits": {"bash": {"max_output_chars": 1234}},
        },
        tmp_path,
    )
    assert cfg.tool_limits == {"bash": {"max_output_chars": 1234}}


def test_team_config_tool_limits_default_empty(tmp_path):
    cfg = _team({"roles": {"lead": {"prompt": "p"}}}, tmp_path)
    assert cfg.tool_limits == {}


def test_registry_applies_limits_to_constructed_tools():
    [bash] = build_tools_for_role(
        ["bash"], tool_limits={"bash": {"max_output_chars": 200}}
    )
    assert bash.max_output_chars == 200


def test_registry_defaults_used_when_no_limits():
    [bash] = build_tools_for_role(["bash"])
    assert bash.max_output_chars == MAX_OUTPUT_CHARS


def test_registry_rejects_unknown_tool_in_limits():
    with pytest.raises(ValueError, match="unknown tools"):
        build_tools_for_role(["bash"], tool_limits={"nope": {"x": 1}})


def test_registry_rejects_unknown_kwarg_for_tool():
    with pytest.raises(ValueError, match="unsupported keys"):
        build_tools_for_role(["bash"], tool_limits={"bash": {"bogus_cap": 1}})


def test_registry_rejects_limits_on_coordination_tools():
    with pytest.raises(ValueError, match="coordination tools"):
        build_tools_for_role(["bash"], tool_limits={"spawn_agent": {"x": 1}})


@pytest.mark.parametrize("value", [0, -1, True, False, 10_000_001])
def test_registry_rejects_invalid_output_caps(value):
    with pytest.raises(ValueError, match="must be an integer"):
        build_tools_for_role(
            ["bash"], tool_limits={"bash": {"max_output_chars": value}}
        )


def test_registry_rejects_safety_constructor_keys_as_limits():
    with pytest.raises(ValueError, match="unsupported keys"):
        build_tools_for_role(
            ["bash"],
            tool_limits={"bash": {"require_process_isolation": 1}},
        )


@pytest.mark.parametrize("cap", [1, 2, 7, 31, 64])
def test_truncate_including_marker_never_exceeds_cap(cap):
    result = truncate("x" * 1000, cap, "stdout")
    assert len(result) <= cap


def test_configured_cap_actually_bounds_bash_output(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    [bash] = build_tools_for_role(
        ["bash"],
        interactive=True,
        tool_limits={"bash": {"max_output_chars": 100}},
    )
    runtime = ToolRuntime(
        environment=LocalEnvironment(str(ws)), safety_policy=None, permission_policy=None
    )

    result = run(
        bash.execute_with_runtime(
            {"command": "printf 'x%.0s' $(seq 1 5000)"}, runtime
        )
    )

    assert "truncated" in result
    assert len(result) < 400  # 100-char cap + exit-code line + marker


def test_all_capped_tools_accept_their_documented_kwargs():
    # The kwargs documented in configs/team.example.yaml must stay constructible.
    limits = {
        "bash": {"max_output_chars": 1000},
        "git_diff": {"max_diff_chars": 1000, "max_status_chars": 500},
        "run_tests": {"max_traceback_chars": 1000},
        "file_read": {"max_read_chars": 1000},
        "grep": {"max_grep_chars": 1000},
    }
    tools = build_tools_for_role(list(limits), tool_limits=limits)
    assert [t.name for t in tools] == list(limits)
