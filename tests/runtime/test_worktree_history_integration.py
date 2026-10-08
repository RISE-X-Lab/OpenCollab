"""History integration retains an agent's committed repair in its delivered patch."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from opencollab.adapters._env_container_worktree import ContainerWorktreeEnvironment
from opencollab.adapters.env import WorktreeEnvironment
from opencollab.application._scheduler_team import _parse_worktree_diff
from tests.support.docker_native_edits_support import LocalDockerTransport


def _git(workspace: Path | str, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *args], input=input_text,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "OpenCollab Tests")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "commit.gpgsign", "false")
    _answer(path, 1)
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


def _answer(path: Path, value: int) -> None:
    (path / "answer.py").write_text(f"def answer():\n    return {value}\n", encoding="utf-8")


def _environment(source: Path, tmp_path: Path, backend: str):
    if backend == "local":
        return WorktreeEnvironment(str(source))
    environment = ContainerWorktreeEnvironment(
        container_id="c" * 64, repository_root=str(source),
        worktree_root=str(tmp_path / "worktrees"),
    )
    LocalDockerTransport(source, pair_reads=False).attach(environment)
    return environment


def _execute_answer(workspace: Path, value: int) -> str:
    return subprocess.run(
        [sys.executable, "-B", "-c", f"from answer import answer; assert answer() == {value}; print(answer())"],
        cwd=workspace, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.mark.parametrize("backend", ["local", "docker-shell"])
@pytest.mark.parametrize("operation", [
    "rebase", "rebase-apply", "rebase-squash", "rebase-abort", "rebase-noop",
    "rebase-conflict", "cherry-pick", "amend", "revert", "merge", "merge-conflict",
])
async def test_a_repair_survives_history_integration(tmp_path, backend, operation):
    source = _repo(tmp_path / "source")
    environment = _environment(source, tmp_path, backend)
    workspace = Path(await environment.setup())
    creation_base = _git(workspace, "rev-parse", "HEAD")
    expected_files = {"answer.py"}
    expected_count = 1
    expected_value = 2
    expected_base = creation_base
    try:
        _answer(workspace, 2)
        assert (await environment.exec_cmd("git add answer.py && git commit -qm repair")).returncode == 0
        original_repair = _git(workspace, "rev-parse", "HEAD")
        assert "answer.py" in await environment.get_diff()
        if operation == "rebase-squash":
            (workspace / "extra.txt").write_text("extra repair\n", encoding="utf-8")
            _git(workspace, "add", "extra.txt")
            _git(workspace, "commit", "-qm", "extra repair")
            expected_files.add("extra.txt")
        if operation.endswith("conflict"):
            _answer(source, 3)
        (source / "upstream.txt").write_text("upstream helper\n", encoding="utf-8")
        _git(source, "add", ".")
        _git(source, "commit", "-qm", "upstream helper")
        upstream = _git(source, "rev-parse", "HEAD")

        if operation in {"rebase", "rebase-apply", "rebase-squash", "rebase-conflict"}:
            expected_base = upstream
            flags = "--apply " if operation == "rebase-apply" else ""
            if operation == "rebase-squash":
                flags = "-i "
                editor = shlex.quote("sed -i.bak '2s/^pick/squash/'")
                command = f"git -c sequence.editor={editor} -c core.editor=true rebase {flags}main"
            else:
                command = f"git rebase {flags}main"
            result = await environment.exec_cmd(command)
            if operation == "rebase-conflict":
                assert result.returncode == 1
                _answer(workspace, 4)
                expected_value = 4
                result = await environment.exec_cmd("git add answer.py && git -c core.editor=true rebase --continue")
            assert result.returncode == 0, result.stderr
        elif operation == "rebase-abort":
            editor = shlex.quote("sed -i.bak 's/^pick/edit/'")
            assert (await environment.exec_cmd(f"git -c sequence.editor={editor} rebase -i main")).returncode == 0
            assert _git(workspace, "rev-parse", "HEAD") != original_repair
            assert (await environment.exec_cmd("git rebase --abort")).returncode == 0
        elif operation == "rebase-noop":
            assert (await environment.exec_cmd("git rebase --force-rebase HEAD")).returncode == 0
        elif operation in {"cherry-pick", "revert"}:
            assert (await environment.exec_cmd(f"git cherry-pick {upstream}")).returncode == 0
            expected_count = 2
            if operation == "revert":
                assert (await environment.exec_cmd("git revert --no-edit HEAD")).returncode == 0
                expected_count = 3
            else:
                expected_files.add("upstream.txt")
        elif operation == "amend":
            _answer(workspace, 3)
            expected_value = 3
            assert (await environment.exec_cmd("git add answer.py && git commit --amend -qm amended")).returncode == 0
        else:
            expected_base = upstream
            expected_count = 2
            result = await environment.exec_cmd("git merge --no-edit main")
            if operation == "merge-conflict":
                assert result.returncode == 1
                _answer(workspace, 4)
                expected_value = 4
                result = await environment.exec_cmd("git add answer.py && git commit -qm resolved")
            assert result.returncode == 0, result.stderr

        diff = await environment.get_diff()
        assert {path for path, _ in _parse_worktree_diff(diff)} == expected_files
        assert environment.diff_base == expected_base
        assert environment.head_commit == _git(workspace, "rev-parse", "HEAD")
        assert environment.head_commit in environment.own_commits
        assert environment.own_commit_count == expected_count
        assert _execute_answer(workspace, expected_value) == str(expected_value)

        # Apply the actual delivery to its advertised base and execute the result.
        target = tmp_path / "delivered"
        _git(source, "worktree", "add", "--detach", str(target), expected_base)
        try:
            _git(target, "apply", "--binary", "-", input_text=diff)
            assert _execute_answer(target, expected_value) == str(expected_value)
        finally:
            _git(source, "worktree", "remove", "--force", str(target))
    finally:
        await environment.cleanup()
    assert not workspace.exists()
    assert _execute_answer(source, 3 if operation.endswith("conflict") else 1) in {"1", "3"}


@pytest.mark.parametrize("backend", ["local", "docker-shell"])
async def test_fast_forward_adopts_the_incoming_merge_commit(tmp_path, backend):
    """Even a two-parent incoming tip is a handoff when it is fast-forwarded."""
    source = _repo(tmp_path / "source")
    environment = _environment(source, tmp_path, backend)
    workspace = Path(await environment.setup())
    try:
        _git(source, "checkout", "-qb", "helper")
        (source / "upstream.txt").write_text("upstream helper\n", encoding="utf-8")
        _git(source, "add", "upstream.txt")
        _git(source, "commit", "-qm", "helper")
        _git(source, "checkout", "-q", "main")
        _git(source, "merge", "--no-ff", "--no-edit", "helper")
        adopted = _git(source, "rev-parse", "HEAD")
        assert len(_git(source, "show", "-s", "--format=%P", "HEAD").split()) == 2
        assert (await environment.exec_cmd("git merge --no-edit main")).returncode == 0
        assert await environment.get_diff() == ""
        assert environment.diff_base == adopted
        assert environment.own_commits == ()

        _answer(workspace, 2)
        assert (await environment.exec_cmd("git add answer.py && git commit -qm repair")).returncode == 0
        assert {path for path, _ in _parse_worktree_diff(await environment.get_diff())} == {"answer.py"}
        assert environment.diff_base == adopted
        assert environment.own_commits == (environment.head_commit,)
    finally:
        await environment.cleanup()
