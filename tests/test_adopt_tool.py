"""``adopt`` -- take a teammate's commit into the tree that is read as the answer.

In the s2dual cells the Adopter brings a Coder's work over with ``bash``:
``git checkout <sha>``. The tools cell ``s2tools-adopt`` takes ``bash`` away,
so the Adopter needs one narrow way to do that step and nothing else: check out
a commit that already exists. It cannot run the tests, so what it is shown is
what the commit changes from where the run started -- after a checkout the
working tree is clean against HEAD, and ``git_diff`` alone would show nothing.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.adopt import AdoptTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.bootstrap.tool_registry import KNOWN_TOOL_NAMES, build_tools_for_role


def run(coro):
    return asyncio.run(coro)


def _git(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _commit_on_side(repo, start: str, path: str, text: str, message: str) -> str:
    """A candidate commit off ``start``, the way a Coder's worktree makes one."""
    _git(repo, "checkout", "-q", "--detach", start)
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", start)
    return sha


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "other.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _runtime(path):
    return ToolRuntime(environment=LocalEnvironment(str(path)), safety_policy=None, permission_policy=None)


def test_adopt_checks_out_the_commit_and_shows_what_it_changes(repo):
    start = _git(repo, "rev-parse", "HEAD")
    candidate = _commit_on_side(repo, start, "app.py", "def f():\n    return 2\n", "narrow fix")

    result = run(AdoptTool().execute_with_runtime({"sha": candidate[:10]}, _runtime(repo)))

    assert _git(repo, "rev-parse", "HEAD") == candidate
    assert candidate in result
    assert "narrow fix" in result
    assert "app.py" in result
    assert "+    return 2" in result
    assert not result.startswith("Error")


def test_a_second_adopt_is_shown_against_where_the_run_started(repo):
    start = _git(repo, "rev-parse", "HEAD")
    first = _commit_on_side(repo, start, "app.py", "def f():\n    return 2\n", "a")
    second = _commit_on_side(repo, start, "other.py", "x = 2\n", "b")
    tool = AdoptTool()

    run(tool.execute_with_runtime({"sha": first}, _runtime(repo)))
    result = run(tool.execute_with_runtime({"sha": second}, _runtime(repo)))

    assert _git(repo, "rev-parse", "HEAD") == second
    # Against the start, the second candidate touches other.py only; against
    # the first candidate it would also show app.py going back.
    assert "other.py" in result
    assert "app.py" not in result


@pytest.mark.parametrize(
    "sha",
    ["", "abc", "HEAD", "HEAD~1", "--orphan=x", "z" * 40, "a" * 41, "abcdef1 && rm -rf .", "main"],
)
def test_anything_but_a_commit_sha_is_refused_and_nothing_moves(repo, sha):
    start = _git(repo, "rev-parse", "HEAD")

    result = run(AdoptTool().execute_with_runtime({"sha": sha}, _runtime(repo)))

    assert result.startswith("Error")
    assert _git(repo, "rev-parse", "HEAD") == start


def test_an_unknown_commit_is_reported_and_nothing_moves(repo):
    start = _git(repo, "rev-parse", "HEAD")

    result = run(AdoptTool().execute_with_runtime({"sha": "deadbeef"}, _runtime(repo)))

    assert result.startswith("Error")
    assert "deadbeef" in result
    assert _git(repo, "rev-parse", "HEAD") == start


def test_uncommitted_work_that_a_checkout_would_overwrite_is_not_discarded(repo):
    start = _git(repo, "rev-parse", "HEAD")
    candidate = _commit_on_side(repo, start, "app.py", "def f():\n    return 2\n", "a")
    (repo / "app.py").write_text("def f():\n    return 3\n", encoding="utf-8")

    result = run(AdoptTool().execute_with_runtime({"sha": candidate}, _runtime(repo)))

    assert result.startswith("Error")
    assert _git(repo, "rev-parse", "HEAD") == start
    assert (repo / "app.py").read_text(encoding="utf-8") == "def f():\n    return 3\n"


def test_adopt_is_a_tool_a_team_file_can_name():
    assert "adopt" in KNOWN_TOOL_NAMES
    (tool,) = build_tools_for_role(["adopt"])
    assert isinstance(tool, AdoptTool)
    assert tool.name == "adopt"
    assert tool.parameters["required"] == ["sha"]
