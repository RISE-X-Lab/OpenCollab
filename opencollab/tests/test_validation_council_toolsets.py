from __future__ import annotations

import importlib.util
from pathlib import Path

_WF_DIR = Path(__file__).resolve().parents[2] / "workflows"
_WF_PATH = _WF_DIR / "validation_council_solve.py"


def _workflow_globals():
    spec = importlib.util.spec_from_file_location("validation_council_solve_under_test", _WF_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.__dict__


def _names(tools) -> list[str]:
    return [tool.name for tool in tools]


def test_validation_council_read_tools_are_read_only():
    names = _names(_workflow_globals()["_read_tools"]())

    assert names == ["file_read", "grep"]
    assert "bash" not in names
    assert "file_write" not in names
    assert "apply_patch" not in names


def test_validation_council_tester_tools_cannot_edit():
    tools = _workflow_globals()["_tester_tools"]()
    names = _names(tools)

    assert names == ["file_read", "run_tests", "grep", "git_diff"]
    assert "bash" not in names
    assert "file_write" not in names
    assert "apply_patch" not in names
    run_tests = next(tool for tool in tools if tool.name == "run_tests")
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_validation_council_risk_tools_are_disabled():
    assert _workflow_globals()["_risk_tools"]() == []


def test_validation_council_coder_tools_keep_edit_path():
    names = _names(_workflow_globals()["_coder_tools"]())

    assert names == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "run_tests",
        "grep",
    ]
