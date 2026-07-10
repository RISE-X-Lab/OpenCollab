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


def test_validation_council_tester_tools_can_probe_but_not_author():
    # Gate roles must be able to run an executable probe (bash) so a PASS is
    # backed by a real check, not prose alone — removing it caused a silent
    # false green (2026-07-09 review). They still cannot author the fix.
    tools = _workflow_globals()["_tester_tools"]()
    names = _names(tools)

    assert "bash" in names
    assert {"file_read", "run_tests", "grep", "git_diff"} <= set(names)
    assert "file_write" not in names
    assert "apply_patch" not in names
    run_tests = next(tool for tool in tools if tool.name == "run_tests")
    assert run_tests.allow_runner_override is False
    assert run_tests.allow_extra_args is False


def test_validation_council_risk_tools_can_read_the_diff():
    # An auditor with no tools (was []) cannot inspect what it judges — it must
    # at least read the diff and sources, while staying read-only.
    names = _names(_workflow_globals()["_risk_tools"]())
    assert "file_read" in names
    assert "git_diff" in names
    assert "bash" not in names
    assert "file_write" not in names


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
