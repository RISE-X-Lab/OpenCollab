"""Test execution remains available after retiring the built-in test wrapper."""

import shlex
import sys

import pytest
from typer.testing import CliRunner

from opencollab.adapters.cli.main import app
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.team_config import TESTER_TOOL_NAMES
from opencollab.bootstrap.tool_registry import build_tools_for_role
from opencollab.tools import builtin_tools


async def test_default_tester_runs_native_tests_and_reports_actual_exit(tmp_path):
    source = tmp_path / "test_behavior.py"
    source.write_text(
        "import unittest\nclass Behavior(unittest.TestCase):\n"
        "    def test_value(self): self.assertEqual(4, 4)\n"
    )
    tools = build_tools_for_role(list(TESTER_TOOL_NAMES), allow_unisolated_shell=True)
    bash = next(tool for tool in tools if tool.name == "bash")
    environment = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=environment, safety_policy=None, permission_policy=None)
    command = f"{shlex.quote(sys.executable)} -m unittest test_behavior -v"
    try:
        passed = await bash.execute_with_runtime({"command": command}, runtime)
        assert "Exit code: 0" in passed
        assert "Ran 1 test" in passed
        assert "OK" in passed
        source.write_text("raise RuntimeError('deliberate test failure')\n")
        failed = await bash.execute_with_runtime({"command": command}, runtime)
        assert "Exit code: 1" in failed
        assert "deliberate test failure" in failed
    finally:
        await environment.cleanup()


async def test_headless_testing_never_implicitly_grants_host_shell(tmp_path):
    (bash,) = builtin_tools("bash")
    environment = LocalEnvironment(str(tmp_path))
    marker = tmp_path / "executed"
    runtime = ToolRuntime(environment=environment, safety_policy=None, permission_policy=None)
    try:
        result = await bash.execute_with_runtime({"command": "touch executed"}, runtime)
        assert result.startswith("Error:")
        assert "isolation" in result or "sandbox" in result
        assert not marker.exists()
    finally:
        await environment.cleanup()


def test_public_composition_rejects_retired_tool():
    with pytest.raises(ValueError, match="unsupported built-in"):
        builtin_tools("run_tests")


def test_removed_cli_switch_is_rejected_without_starting_model():
    result = CliRunner().invoke(app, ["--allow-local-child-tests"])
    assert result.exit_code == 2
    assert "No such option" in result.output
