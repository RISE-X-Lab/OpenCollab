import asyncio

import pytest
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.fs import FileWriteTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


def test_apply_patch_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 1,
                "end_line": 1,
                "new_str": "x",
            },
            runtime,
        )
    )

    assert result == "Error: no execution environment available."


def test_line_replace_applies_range_and_stale_guard_keeps_file_unchanged(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("a\nb\nc\nd\n", encoding="utf-8")
    tool = ApplyPatchTool()
    runtime = _runtime(ws)

    result = run(
        tool.execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 3,
                "new_str": "X\nY",
            },
            runtime,
        )
    )

    assert "Applied line_replace" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"

    result = run(
        tool.execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "stale write",
                "expected_str": "WRONG",
            },
            runtime,
        )
    )

    assert "expected_str does not match" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"


def test_unified_diff_matches_by_content_despite_line_drift(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("extra0\nextra1\nlineA\nlineB\nlineC\n", encoding="utf-8")
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " lineA\n"
        "-lineB\n"
        "+lineB_fixed\n"
        " lineC\n"
    )

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch},
            _runtime(ws),
        )
    )

    assert "Applied unified_diff" in result
    assert target.read_text(encoding="utf-8") == "extra0\nextra1\nlineA\nlineB_fixed\nlineC\n"


def test_unified_diff_failed_hunk_writes_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    original = "a\nb\nc\nd\ne\nf\n"
    target.write_text(original, encoding="utf-8")
    patch = (
        "@@ -1,2 +1,2 @@\n"
        " a\n"
        "-b\n"
        "+B\n"
        "@@ -5,2 +5,2 @@\n"
        " e\n"
        "-NOPE\n"
        "+X\n"
    )

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch},
            _runtime(ws),
        )
    )

    assert "hunk #2" in result
    assert target.read_text(encoding="utf-8") == original


def test_apply_patch_reports_file_and_workspace_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = ApplyPatchTool()
    runtime = _runtime(ws)

    missing = run(
        tool.execute_with_runtime(
            {
                "path": "missing.py",
                "mode": "line_replace",
                "start_line": 1,
                "end_line": 1,
                "new_str": "x",
            },
            runtime,
        )
    )
    with pytest.raises(PermissionError, match="Path escapes workspace"):
        run(
            tool.execute_with_runtime(
                {
                    "path": "/etc/passwd",
                    "mode": "line_replace",
                    "start_line": 1,
                    "end_line": 1,
                    "new_str": "x",
                },
                runtime,
            )
        )

    assert missing.startswith("Error: file not found:")
    assert missing.endswith("missing.py")


def test_str_replace_identical_old_and_new_is_explicit_noop_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "hello world",
                "new_str": "hello world",
            },
            _runtime(ws),
        )
    )

    assert "no-op" in result
    assert result.startswith("Error:")
    # File untouched.
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_str_replace_success_reports_content_changed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    target = ws / "f.py"
    target.write_text("hello world\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "str_replace",
                "old_str": "world",
                "new_str": "there",
            },
            _runtime(ws),
        )
    )

    assert "content changed" in result
    assert target.read_text(encoding="utf-8") == "hello there\n"
