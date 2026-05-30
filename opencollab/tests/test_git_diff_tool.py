import asyncio
import subprocess

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.git_status import GitDiffTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A git repo with one committed file, then an uncommitted modification."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "init")
    # Uncommitted change.
    (repo / "app.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    return repo


def _runtime(repo):
    return ToolRuntime(environment=LocalEnvironment(str(repo)), safety_policy=None, permission_policy=None)


def test_git_diff_shows_working_tree_changes(repo):
    result = run(GitDiffTool().execute_with_runtime({}, _runtime(repo)))

    assert "diff vs HEAD:" in result
    assert "-    return 1" in result
    assert "+    return 2" in result
    assert "Status (git status --short):" in result
    assert "app.py" in result


def test_git_diff_clean_tree_reports_no_changes(repo):
    _git(repo, "checkout", "--", "app.py")  # discard the uncommitted edit

    result = run(GitDiffTool().execute_with_runtime({}, _runtime(repo)))

    assert "diff vs HEAD: (no changes)" in result
    assert "Status: working tree clean." in result


def test_git_diff_stat_only(repo):
    result = run(GitDiffTool().execute_with_runtime({"stat_only": True}, _runtime(repo)))

    assert "diff --stat:" in result
    assert "app.py" in result
    # --stat output mentions insertions/deletions, not the raw +/- lines.
    assert "1 file changed" in result


def test_git_diff_path_filter(repo):
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")

    result = run(GitDiffTool().execute_with_runtime({"path": "app.py"}, _runtime(repo)))

    assert "app.py" in result
    # The untracked other.py shouldn't appear in a path-filtered diff body.
    assert "other.py" not in result.split("diff vs HEAD:")[1]


def test_git_diff_include_status_false(repo):
    result = run(GitDiffTool().execute_with_runtime({"include_status": False}, _runtime(repo)))

    assert "Status" not in result
    assert "diff vs HEAD:" in result


def test_git_diff_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    runtime = ToolRuntime(environment=LocalEnvironment(str(plain)), safety_policy=None, permission_policy=None)

    result = run(GitDiffTool().execute_with_runtime({}, runtime))

    assert result == "Error: not a git repository."


def test_git_diff_without_env_errors():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(GitDiffTool().execute_with_runtime({}, runtime))

    assert result == "Error: no execution environment available."


def test_git_diff_has_native_execute_with_runtime():
    assert GitDiffTool.execute_with_runtime is not Tool.execute_with_runtime
