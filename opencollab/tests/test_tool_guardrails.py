"""Guardrails added for mid-tier backends (Kimi/DeepSeek-class models).

Covers the batch of tool-ergonomics fixes:
- bash description deflects to the dedicated tools instead of suggesting
  "running tests / git operations" via bash;
- file_read and grep bound their output by chars, not just lines/matches;
- str_replace's not-found error points at apply_patch as the fallback;
- file_write 'create' refuses to silently replace a substantial file with
  much shorter content unless overwrite is confirmed.
"""

import asyncio

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import (
    MAX_GREP_CHARS,
    MAX_READ_CHARS,
    FileReadTool,
    FileWriteTool,
    GrepTool,
)
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _runtime(workspace):
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    return ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)


def _workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def test_bash_description_deflects_to_dedicated_tools():
    desc = BashTool.description
    assert "run_tests" in desc
    assert "git_diff" in desc
    # The old phrasing steered models to bash for tests and git operations.
    assert "Use this for running tests" not in desc


def test_file_read_bounds_chars_not_just_lines(tmp_path):
    ws = _workspace(tmp_path)
    # One "line" far over the char cap — line limits alone would pass it through.
    (ws / "minified.js").write_text("x" * (MAX_READ_CHARS * 2), encoding="utf-8")

    result = run(
        FileReadTool().execute_with_runtime({"path": "minified.js"}, _runtime(ws))
    )

    assert "truncated" in result
    assert len(result) < MAX_READ_CHARS + 500  # header + marker slack


def test_file_read_small_file_is_untouched(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "f.py").write_text("a\nb\n", encoding="utf-8")

    result = run(FileReadTool().execute_with_runtime({"path": "f.py"}, _runtime(ws)))

    assert "truncated" not in result
    assert "1\ta" in result and "2\tb" in result


def test_grep_bounds_output_chars(tmp_path):
    ws = _workspace(tmp_path)
    long_match = "needle " + "y" * 600
    (ws / "big.txt").write_text("\n".join([long_match] * 40), encoding="utf-8")

    result = run(
        GrepTool().execute_with_runtime({"pattern": "needle"}, _runtime(ws))
    )

    assert "truncated" in result
    assert len(result) < MAX_GREP_CHARS + 500


def test_headless_command_tools_and_grep_cannot_read_outside_secret(tmp_path):
    ws = _workspace(tmp_path)
    secret = tmp_path / "ground_truth_secret.txt"
    secret.write_text("SWE_ANSWER_TOKEN", encoding="utf-8")
    runtime = _runtime(ws)

    grep_result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "SWE_ANSWER_TOKEN", "path": str(secret)}, runtime
        )
    )
    bash_result = run(
        BashTool(require_process_isolation=True).execute_with_runtime(
            {"command": f"cat {secret}"}, runtime
        )
    )
    tests_result = run(
        RunTestsTool(
            allow_runner_override=False,
            allow_extra_args=False,
            require_process_isolation=True,
        ).execute_with_runtime(
            {"runner": f"cat {secret}"}, runtime
        )
    )

    for result in (grep_result, bash_result, tests_result):
        assert "SWE_ANSWER_TOKEN" not in result
        assert result.startswith("Error:")


def test_headless_command_tools_cannot_modify_outside_file(tmp_path):
    ws = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("original", encoding="utf-8")
    runtime = _runtime(ws)

    result = run(
        BashTool(require_process_isolation=True).execute_with_runtime(
            {"command": f"printf pwned > {outside}"}, runtime
        )
    )

    assert result.startswith("Error:")
    assert outside.read_text(encoding="utf-8") == "original"


def test_str_replace_not_found_points_at_apply_patch(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "f.py").write_text("hello\n", encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "f.py", "mode": "str_replace", "old_str": "absent", "new_str": "x"},
            _runtime(ws),
        )
    )

    assert result.startswith("Error: old_str not found")
    assert "apply_patch" in result


def test_create_refuses_shrinking_overwrite_without_confirmation(tmp_path):
    ws = _workspace(tmp_path)
    original = "line\n" * 400  # 2000 chars, over the guard minimum
    (ws / "big.py").write_text(original, encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "big.py", "mode": "create", "content": "tiny\n"},
            _runtime(ws),
        )
    )

    assert result.startswith("Error: refusing to overwrite")
    assert "overwrite: true" in result
    assert (ws / "big.py").read_text(encoding="utf-8") == original


def test_create_shrinking_overwrite_allowed_when_confirmed(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "big.py").write_text("line\n" * 400, encoding="utf-8")

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "big.py", "mode": "create", "content": "tiny\n", "overwrite": True},
            _runtime(ws),
        )
    )

    assert result.startswith("Created/wrote")
    assert (ws / "big.py").read_text(encoding="utf-8") == "tiny\n"


def test_create_new_file_and_comparable_rewrite_need_no_confirmation(tmp_path):
    ws = _workspace(tmp_path)
    tool = FileWriteTool()
    runtime = _runtime(ws)

    fresh = run(
        tool.execute_with_runtime(
            {"path": "new.py", "mode": "create", "content": "x\n"}, runtime
        )
    )
    assert fresh.startswith("Created/wrote")

    # Small files and comparable-size rewrites pass the guard untouched.
    (ws / "small.py").write_text("old\n", encoding="utf-8")
    rewritten = run(
        tool.execute_with_runtime(
            {"path": "small.py", "mode": "create", "content": "new\n"}, runtime
        )
    )
    assert rewritten.startswith("Created/wrote")
    assert (ws / "small.py").read_text(encoding="utf-8") == "new\n"
