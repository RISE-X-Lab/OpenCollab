import asyncio
import subprocess

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    return repo


def _runtime(path):
    return ToolRuntime(environment=LocalEnvironment(str(path)), safety_policy=None, permission_policy=None)


def test_git_diff_shows_status_and_working_tree_patch(repo):
    result = run(GitDiffTool().execute_with_runtime({}, _runtime(repo)))

    assert "Status (git status --short):" in result
    assert "diff vs HEAD:" in result
    assert "-    return 1" in result
    assert "+    return 2" in result


def test_git_diff_stat_only_summarizes_without_raw_patch(repo):
    result = run(GitDiffTool().execute_with_runtime({"stat_only": True}, _runtime(repo)))

    assert "diff --stat:" in result
    assert "app.py" in result
    assert "1 file changed" in result
    assert "-    return 1" not in result


def test_git_diff_stat_only_uses_same_head_baseline_for_staged_changes(repo):
    _git(repo, "add", "app.py")

    result = run(GitDiffTool().execute_with_runtime({"stat_only": True}, _runtime(repo)))

    assert "diff --stat:" in result
    assert "app.py" in result
    assert "1 file changed" in result


def test_git_diff_path_filter_can_skip_status(repo):
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")

    result = run(
        GitDiffTool().execute_with_runtime(
            {"path": "app.py", "include_status": False},
            _runtime(repo),
        )
    )

    assert "Status" not in result
    assert "app.py" in result
    assert "other.py" not in result.split("diff vs HEAD:")[1]


def test_git_diff_reports_environment_errors(tmp_path):
    no_env = run(
        GitDiffTool().execute_with_runtime(
            {},
            ToolRuntime(environment=None, safety_policy=None, permission_policy=None),
        )
    )
    plain = tmp_path / "plain"
    plain.mkdir()
    non_git = run(GitDiffTool().execute_with_runtime({}, _runtime(plain)))

    assert no_env == "Error: no execution environment available."
    assert non_git == "Error: not a git repository."
