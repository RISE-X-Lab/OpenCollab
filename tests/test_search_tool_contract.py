"""Contract checks for GrepTool: the command it builds and how it fails.

Split out of ``test_tool_runtime_contract.py``. These pin the search command
(option termination, hidden-path inclusion, the ERE fallback used when rg is
missing) and keep backend failures from being reported as an empty result.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tool_runtime_test_support import FakeEnv, SpySafetyPolicy, run

from opencollab.adapters.tools.fs import GrepTool
from opencollab.application.tool_execution import ToolRuntime

# ---------------------------------------------------------------------------
# GrepTool
# ---------------------------------------------------------------------------


def test_grep_tool_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(GrepTool().execute_with_runtime({"pattern": "needle"}, runtime))

    assert result == "Error: no execution environment available."


def test_grep_tool_routes_search_path_through_safety_policy():
    env = FakeEnv(stdout="src/app.py:1:needle\n")
    safety = SpySafetyPolicy()
    runtime = ToolRuntime(environment=env, safety_policy=safety, permission_policy=None)

    result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "needle", "path": "../outside", "glob": "*.py", "max_results": 3},
            runtime,
        )
    )

    assert result == "src/app.py:1:needle"
    assert safety.path_calls == ["../outside"]
    assert len(env.exec_calls) == 1
    cmd, timeout = env.exec_calls[0]
    assert "needle" in cmd
    assert "../outside" in cmd
    assert timeout == 30


def test_grep_tool_rejects_non_integer_max_results_before_exec():
    env = FakeEnv(stdout="src/app.py:1:needle\n")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "needle", "max_results": "1; touch pwned"},
            runtime,
        )
    )

    assert result == "Error: max_results must be an integer."
    assert env.exec_calls == []


def test_grep_tool_applies_max_results_globally():
    env = FakeEnv(
        stdout="\n".join(f"src/file_{index}.py:1:needle" for index in range(6))
    )
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        GrepTool().execute_with_runtime(
            {"pattern": "needle", "max_results": 2},
            runtime,
        )
    )

    assert result.splitlines() == [
        "src/file_0.py:1:needle",
        "src/file_1.py:1:needle",
    ]


def test_grep_tool_does_not_fallback_after_normal_rg_no_match():
    class NoMatchEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0):
            self.exec_calls.append((cmd, timeout))
            return SimpleNamespace(returncode=1, stdout="", stderr="")

    env = NoMatchEnv()
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(GrepTool().execute_with_runtime({"pattern": "absent"}, runtime))

    assert result == "No matches found for pattern: absent"
    assert len(env.exec_calls) == 1
    assert "grep -r" not in env.exec_calls[0][0]


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (2, "regex parse error", "regex parse error"),
        (2, "path does not exist", "path does not exist"),
        (-1, "Command timed out after 30s", "timed out"),
    ],
)
def test_grep_tool_reports_backend_failures(returncode, stderr, expected):
    class FailedSearchEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0):
            self.exec_calls.append((cmd, timeout))
            return SimpleNamespace(
                returncode=returncode,
                stdout="",
                stderr=stderr,
                stdout_truncated=False,
                stderr_truncated=False,
            )

    env = FailedSearchEnv()
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(GrepTool().execute_with_runtime({"pattern": "["}, runtime))

    assert result.startswith("Error: rg search failed")
    assert expected in result
    assert "No matches" not in result
    assert "2>/dev/null" not in env.exec_calls[0][0]


def test_grep_tool_includes_hidden_project_paths_with_noise_exclusions():
    env = FakeEnv(stdout=".github/workflows/ci.yml:1:needle\n")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(GrepTool().execute_with_runtime({"pattern": "needle"}, runtime))

    assert result == ".github/workflows/ci.yml:1:needle"
    command = env.exec_calls[0][0]
    assert "rg -n --hidden " in command
    assert "-g '!.git/**'" in command
    assert "-g '!.venv/**'" in command
    assert "-g '!.opencollab/**'" in command


def test_grep_tool_terminates_options_before_pattern_and_path():
    env = FakeEnv(stdout="")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(
        GrepTool().execute_with_runtime(
            {"pattern": "--pre=touch pwned", "path": "--debug"},
            runtime,
        )
    )

    cmd, _ = env.exec_calls[0]
    assert "rg -n --hidden" in cmd
    assert "-- '--pre=touch pwned' --debug" in cmd


def test_grep_tool_fallback_uses_ere_and_skips_session_dir():
    # When rg is missing from PATH the command falls back to grep. That fallback
    # MUST use -E (ERE) so `a|b|c` alternation works — plain `grep -rn` is BRE,
    # where `|` is a literal char and an alternation pattern silently matches
    # nothing. It must also skip .opencollab so it can't "match" its own logged
    # pattern strings instead of real source. (Regression: a 100-step run stalled
    # because every alternation grep returned "No matches found".)
    class MissingRgEnv(FakeEnv):
        async def exec_cmd(self, cmd: str, timeout: float = 120.0):
            self.exec_calls.append((cmd, timeout))
            if len(self.exec_calls) == 1:
                return SimpleNamespace(returncode=127, stdout="", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="")

    env = MissingRgEnv(stdout="")
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    run(
        GrepTool().execute_with_runtime(
            {"pattern": "Scheduler|RowStatus"}, runtime
        )
    )

    assert len(env.exec_calls) == 2
    cmd, _ = env.exec_calls[1]
    assert cmd.startswith("grep -rEn")
    assert " grep -rn " not in cmd  # the buggy BRE fallback must be gone
    assert "--exclude-dir=.opencollab" in cmd

