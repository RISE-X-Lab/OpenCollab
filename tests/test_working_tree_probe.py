"""EnvWorkingTreeProbe.changed_excluding — source-scoped working-tree probe.

An SDK consumer may apply support files without committing them, so
``git status --porcelain`` remains non-empty for the whole run and ``changed()``
is always true. ``changed_excluding(injected_paths)`` drops those files so the
probe answers whether the agent edited source.

The unit tests use a fake env that records the exact ``exec_cmd`` it was issued;
the (skip-if-no-git) integration tests run real git to prove an untracked
injected test file is excluded.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencollab.adapters._env_base import ExecResult
from opencollab.adapters.working_tree import EnvWorkingTreeProbe


def run(coro):
    return asyncio.run(coro)


class RecordingEnv:
    """Fake Environment recording every exec_cmd; returns a scripted stdout."""

    def __init__(self, *, stdout: str = "", workspace: str = "/ws") -> None:
        self.workspace = workspace
        self._stdout = stdout
        self.commands: list[str] = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.commands.append(cmd)
        return SimpleNamespace(stdout=self._stdout)


class ScriptedEnv:
    def __init__(self, results, *, workspace: str = "/ws") -> None:
        self.workspace = workspace
        self._results = list(results)
        self.commands: list[str] = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self.commands.append(cmd)
        return self._results.pop(0)


def test_changed_excluding_issues_exclude_pathspec_and_returns_bool():
    env = RecordingEnv(stdout=" M opencollab/foo.py\n", workspace="/ws")
    probe = EnvWorkingTreeProbe(env, workspace="/ws")

    result = run(probe.changed_excluding(["tests/test_inj.py"]))

    assert result is True  # bool(stdout)
    assert env.commands[-1] == (
        "git -C /ws status --porcelain --untracked-files=all "
        "-- . ':(exclude)tests/test_inj.py'"
    )


def test_changed_excluding_returns_false_on_empty_status():
    # Only the injected test was dirty -> excluding it leaves empty output.
    env = RecordingEnv(stdout="", workspace="/ws")
    probe = EnvWorkingTreeProbe(env, workspace="/ws")

    assert run(probe.changed_excluding(["tests/test_inj.py"])) is False


def test_changed_excluding_quotes_each_exclude_token_separately():
    env = RecordingEnv(stdout="x\n", workspace="/ws")
    probe = EnvWorkingTreeProbe(env, workspace="/ws")

    run(probe.changed_excluding(["a/b.py", "c/d.py"]))

    assert env.commands[-1] == (
        "git -C /ws status --porcelain --untracked-files=all "
        "-- . ':(exclude)a/b.py' ':(exclude)c/d.py'"
    )


def test_changed_excluding_empty_falls_back_to_plain_status():
    # Empty excludes -> identical command/answer to changed() for ordinary runs.
    env = RecordingEnv(stdout=" M x.py\n", workspace="/ws")
    probe = EnvWorkingTreeProbe(env, workspace="/ws")

    excl = run(probe.changed_excluding([]))
    plain = run(probe.changed())

    assert excl is True and plain is True
    # Both issue the SAME plain status command (no pathspec).
    assert env.commands == [
        "git -C /ws status --porcelain",
        "git -C /ws status --porcelain",
    ]


@pytest.mark.parametrize(
    "result",
    [
        ExecResult(1, "partial", "failed"),
        ExecResult(0, "partial", "", stdout_truncated=True),
        ExecResult(0, "partial", "", stdout_dropped_bytes=9),
        ExecResult(0, "", "partial", stderr_truncated=True),
    ],
)
def test_diff_rejects_incomplete_status_evidence(result):
    probe = EnvWorkingTreeProbe(ScriptedEnv([result]), workspace="/ws")

    with pytest.raises(RuntimeError, match="working-tree status"):
        run(probe.diff())


def test_diff_rejects_truncated_tracked_patch():
    env = ScriptedEnv(
        [
            ExecResult(0, " M source.py\n", ""),
            ExecResult(
                0,
                "diff --git a/source.py b/source.py\n",
                "",
                stdout_truncated=True,
                stdout_dropped_bytes=128,
            ),
        ]
    )
    probe = EnvWorkingTreeProbe(env, workspace="/ws")

    with pytest.raises(RuntimeError, match="tracked diff exceeded capture limit"):
        run(probe.diff())


# --------------------------------------------------------------------------- #
# real-git integration (skipped where git is unavailable)
# --------------------------------------------------------------------------- #


class _RealEnv:
    """Minimal env that shells out to real git via subprocess (no opencollab env)."""

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True
        )
        return ExecResult(proc.returncode, proc.stdout, proc.stderr)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_real_git_untracked_injected_test_is_excluded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    # The harness git-applies an UNTRACKED new test file but does NOT commit it.
    (repo / "tests").mkdir()
    inj = "tests/test_inj.py"
    (repo / inj).write_text("def test_x():\n    assert True\n")

    probe = EnvWorkingTreeProbe(_RealEnv(str(repo)), workspace=str(repo))

    # Whole tree is dirty (the untracked injected file), but SOURCE is clean.
    assert run(probe.changed()) is True
    assert run(probe.changed_excluding([inj])) is False


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_real_git_source_edit_alongside_injected_is_detected(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    # Nested untracked injected dir AND a real source edit.
    (repo / "tests" / "nested").mkdir(parents=True)
    inj = "tests/nested/test_inj.py"
    (repo / inj).write_text("def test_x():\n    assert True\n")
    (repo / "src.py").write_text("x = 2\n")  # the agent's real edit

    probe = EnvWorkingTreeProbe(_RealEnv(str(repo)), workspace=str(repo))

    assert run(probe.changed()) is True
    # Excluding only the injected test still sees the source edit -> True.
    assert run(probe.changed_excluding([inj])) is True


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_real_git_diff_covers_staged_unstaged_and_untracked_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "unstaged.py").write_text("unstaged = 1\n")
    (repo / "staged.py").write_text("staged = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")

    (repo / "unstaged.py").write_text("unstaged = 2\n")
    (repo / "staged.py").write_text("staged = 2\n")
    _git(repo, "add", "staged.py")
    (repo / "new file.py").write_text("untracked = 3\n")

    probe = EnvWorkingTreeProbe(_RealEnv(str(repo)), workspace=str(repo))
    evidence = run(probe.diff())

    assert run(probe.changed()) is True
    assert " M unstaged.py" in evidence
    assert "M  staged.py" in evidence
    assert "?? \"new file.py\"" in evidence
    assert "-unstaged = 1" in evidence
    assert "+unstaged = 2" in evidence
    assert "-staged = 1" in evidence
    assert "+staged = 2" in evidence
    assert "+untracked = 3" in evidence


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_real_git_untracked_empty_file_remains_visible_in_diff_evidence(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    (repo / "empty.txt").touch()

    evidence = run(
        EnvWorkingTreeProbe(_RealEnv(str(repo)), workspace=str(repo)).diff()
    )

    assert "?? empty.txt" in evidence
    assert "[Untracked file: empty.txt]" in evidence
