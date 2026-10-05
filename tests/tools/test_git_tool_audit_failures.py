"""Regression tests for incomplete Git change audits."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.adopt import AdoptTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class ProxyEnvironment:
    """Delegate to Git while allowing a selected audit result to be damaged."""

    def __init__(self, path, *, damaged_command, damage):
        self.delegate = LocalEnvironment(str(path))
        self.damaged_command = damaged_command
        self.damage = damage

    async def exec_cmd(self, command, timeout=120.0):
        result = await self.delegate.exec_cmd(command, timeout)
        if self.damaged_command not in command:
            return result
        if self.damage == "failure":
            return ExecResult(128, result.stdout, "audit command failed")
        return ExecResult(
            result.returncode,
            result.stdout[: max(len(result.stdout) // 2, 1)],
            result.stderr,
            stdout_truncated=True,
        )


def _runtime(environment):
    return ToolRuntime(environment=environment, safety_policy=None, permission_policy=None)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.mark.parametrize("include_status", [True, False])
async def test_git_diff_reports_truncated_untracked_enumeration(tmp_path, include_status):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "untracked-one.txt").write_text("one\n", encoding="utf-8")
    (repo / "untracked-two.txt").write_text("two\n", encoding="utf-8")
    environment = ProxyEnvironment(
        repo,
        damaged_command="status --porcelain=v1 -z",
        damage="truncated",
    )

    result = await GitDiffTool().execute_with_runtime(
        {"include_status": include_status}, _runtime(environment)
    )

    assert "incomplete" in result.lower() or "truncated" in result.lower()
    assert "untracked" in result.lower()


@pytest.mark.parametrize(
    ("damaged_command", "damage"),
    [
        ("log -1 --format=%s", "failure"),
        ("log -1 --format=%s", "truncated"),
        ("diff --stat", "failure"),
        ("diff --stat", "truncated"),
        ("diff --no-ext-diff", "failure"),
        ("diff --no-ext-diff", "truncated"),
    ],
)
async def test_adopt_reports_successful_checkout_when_audit_is_unavailable(
    tmp_path, damaged_command, damage
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "app.txt")
    _git(repo, "commit", "-q", "-m", "start")
    start = _git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "commit", "-am", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", start)
    environment = ProxyEnvironment(
        repo,
        damaged_command=damaged_command,
        damage=damage,
    )

    result = await AdoptTool(require_process_isolation=False).execute_with_runtime(
        {"sha": candidate}, _runtime(environment)
    )

    assert _git(repo, "rev-parse", "HEAD") == candidate
    assert "checked out" in result.lower()
    assert "unavailable" in result.lower() or "incomplete" in result.lower()
    assert "(none)" not in result
