import asyncio

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.edit import ApplyPatchTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


# ---------------------------------------------------------------------------
# line_replace mode
# ---------------------------------------------------------------------------


def test_line_replace_replaces_inclusive_range(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    runtime = _runtime(ws)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "line_replace", "start_line": 2, "end_line": 3, "new_str": "X\nY"},
            runtime,
        )
    )

    assert "Applied line_replace" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "a\nX\nY\nd\n"


def test_line_replace_insert_before_with_empty_range(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\n", encoding="utf-8")
    runtime = _runtime(ws)

    # end_line = start_line - 1 => insert before start_line, delete nothing.
    run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "line_replace", "start_line": 2, "end_line": 1, "new_str": "inserted"},
            runtime,
        )
    )

    assert (ws / "f.py").read_text(encoding="utf-8") == "a\ninserted\nb\n"


def test_line_replace_expected_str_guard_aborts_on_mismatch(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\nc\n", encoding="utf-8")
    runtime = _runtime(ws)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "X",
                "expected_str": "WRONG",
            },
            runtime,
        )
    )

    assert "expected_str does not match" in result
    # Nothing written.
    assert (ws / "f.py").read_text(encoding="utf-8") == "a\nb\nc\n"


def test_line_replace_out_of_range_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\n", encoding="utf-8")
    runtime = _runtime(ws)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "line_replace", "start_line": 1, "end_line": 9, "new_str": "X"},
            runtime,
        )
    )

    assert "past end of file" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "a\nb\n"


# ---------------------------------------------------------------------------
# unified_diff mode
# ---------------------------------------------------------------------------


def test_unified_diff_applies_clean_hunk(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    runtime = _runtime(ws)

    patch = (
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-line2\n"
        "+line2_modified\n"
        " line3\n"
    )
    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch}, runtime
        )
    )

    assert "Applied unified_diff" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "line1\nline2_modified\nline3\n"


def test_unified_diff_tolerates_line_number_drift(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Diff was generated against a file where the block was at line 2, but here
    # extra leading lines pushed it down. Content match should still locate it.
    (ws / "f.py").write_text("extra0\nextra1\nlineA\nlineB\nlineC\n", encoding="utf-8")
    runtime = _runtime(ws)

    patch = (
        "@@ -1,3 +1,3 @@\n"
        " lineA\n"
        "-lineB\n"
        "+lineB_fixed\n"
        " lineC\n"
    )
    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch}, runtime
        )
    )

    assert "Applied unified_diff" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "extra0\nextra1\nlineA\nlineB_fixed\nlineC\n"


def test_unified_diff_failed_match_writes_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("alpha\nbeta\n", encoding="utf-8")
    runtime = _runtime(ws)

    patch = (
        "@@ -1,2 +1,2 @@\n"
        " alpha\n"
        "-does_not_exist\n"
        "+replacement\n"
    )
    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch}, runtime
        )
    )

    assert "did not match" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_unified_diff_multi_hunk_all_or_nothing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\nc\nd\ne\nf\n", encoding="utf-8")
    runtime = _runtime(ws)

    # First hunk matches, second does not — nothing should be written.
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
            {"path": "f.py", "mode": "unified_diff", "patch": patch}, runtime
        )
    )

    assert "hunk #2" in result
    assert (ws / "f.py").read_text(encoding="utf-8") == "a\nb\nc\nd\ne\nf\n"


def test_unified_diff_pure_insertion(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\nb\n", encoding="utf-8")
    runtime = _runtime(ws)

    patch = (
        "@@ -1,2 +1,3 @@\n"
        " a\n"
        "+inserted\n"
        " b\n"
    )
    run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": patch}, runtime
        )
    )

    assert (ws / "f.py").read_text(encoding="utf-8") == "a\ninserted\nb\n"


def test_unified_diff_no_header_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.py").write_text("a\n", encoding="utf-8")
    runtime = _runtime(ws)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "f.py", "mode": "unified_diff", "patch": "just some text"}, runtime
        )
    )

    assert "no @@ hunk headers" in result


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_apply_patch_file_not_found(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runtime = _runtime(ws)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {"path": "missing.py", "mode": "line_replace", "start_line": 1, "end_line": 1, "new_str": "x"},
            runtime,
        )
    )

    assert result.startswith("Error: file not found:")
    assert result.endswith("missing.py")


def test_apply_patch_respects_path_jail(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    runtime = _runtime(ws)

    with pytest.raises(PermissionError):
        run(
            ApplyPatchTool().execute_with_runtime(
                {"path": "/etc/passwd", "mode": "line_replace", "start_line": 1, "end_line": 1, "new_str": "x"},
                runtime,
            )
        )


def test_apply_patch_has_native_execute_with_runtime():
    assert ApplyPatchTool.execute_with_runtime is not Tool.execute_with_runtime
