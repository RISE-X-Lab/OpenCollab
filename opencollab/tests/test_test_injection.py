"""Tests for ``harness.test_injection.apply_test_patch``.

The helper stages a benchmark test_patch inside the env, runs ``git apply``,
and returns the test files it touched. A failed partial apply returns ``[]``
only after Git proves every touched path and reject artifact clean.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid

import pytest

from opencollab.adapters.env import Environment, ExecResult
from opencollab.harness import test_injection as injection
from opencollab.harness.test_injection import (
    TestPatchIsolationError,
    _numstat_paths,
    _touched_files,
    apply_test_patch,
)


def run(coro):
    return asyncio.run(coro)


SAMPLE_PATCH = (
    "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
    "--- a/tests/test_foo.py\n"
    "+++ b/tests/test_foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " x = 1\n"
    "+y = 2\n"
)


class FakeEnv(Environment):
    """Records exec_cmd calls and written files; scriptable git-apply rc."""

    def __init__(
        self,
        *,
        apply_check_rc: int = 0,
        apply_rc: int = 0,
        apply_exception: BaseException | None = None,
        numstat_rc: int = 0,
        numstat_stdout: str = "",
        numstat_exception: Exception | None = None,
        numstat_stdout_truncated: bool = False,
        numstat_stderr_truncated: bool = False,
        rollback_rc: int = 0,
        rollback_status_rc: int = 0,
        rollback_status_stdout: str = "",
        rollback_status_truncated: bool = False,
        remove_exception: BaseException | None = None,
    ):
        self.cmds: list[str] = []
        self.written: dict[str, str] = {}
        self.staged_paths: list[str] = []
        self.removed_paths: list[str] = []
        self._apply_check_rc = apply_check_rc
        self._apply_rc = apply_rc
        self._apply_exception = apply_exception
        self._numstat_rc = numstat_rc
        self._numstat_stdout = numstat_stdout
        self._numstat_exception = numstat_exception
        self._numstat_stdout_truncated = numstat_stdout_truncated
        self._numstat_stderr_truncated = numstat_stderr_truncated
        self._rollback_rc = rollback_rc
        self._rollback_status_rc = rollback_status_rc
        self._rollback_status_stdout = rollback_status_stdout
        self._rollback_status_truncated = rollback_status_truncated
        self._remove_exception = remove_exception

    async def write_file(self, path: str, content: str) -> None:
        self.written[path] = content

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        path = f"/tmp/{prefix}{uuid.uuid4().hex}{suffix}"
        self.staged_paths.append(path)
        self.written[path] = content
        return path

    async def remove_file(self, path: str) -> None:
        self.removed_paths.append(path)
        if self._remove_exception is not None:
            raise self._remove_exception
        self.written.pop(path, None)

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git apply --numstat"):
            if self._numstat_exception is not None:
                raise self._numstat_exception
            return ExecResult(
                returncode=self._numstat_rc,
                stdout=self._numstat_stdout,
                stderr="numstat boom" if self._numstat_rc else "",
                stdout_truncated=self._numstat_stdout_truncated,
                stderr_truncated=self._numstat_stderr_truncated,
                stdout_dropped_bytes=4096 if self._numstat_stdout_truncated else 0,
                stderr_dropped_bytes=4096 if self._numstat_stderr_truncated else 0,
            )
        if cmd.startswith("git apply --check"):
            return ExecResult(returncode=self._apply_check_rc, stdout="", stderr="check boom")
        if cmd.startswith("git apply"):
            if self._apply_exception is not None:
                raise self._apply_exception
            return ExecResult(returncode=self._apply_rc, stdout="", stderr="apply boom")
        if cmd.startswith("git --literal-pathspecs status"):
            return ExecResult(
                returncode=self._rollback_status_rc,
                stdout=self._rollback_status_stdout,
                stderr="status boom" if self._rollback_status_rc else "",
                stdout_truncated=self._rollback_status_truncated,
                stdout_dropped_bytes=4096 if self._rollback_status_truncated else 0,
            )
        if cmd.startswith("git --literal-pathspecs"):
            return ExecResult(
                returncode=self._rollback_rc,
                stdout="",
                stderr="rollback boom" if self._rollback_rc else "",
            )
        return ExecResult(returncode=0, stdout="", stderr="")


def test_apply_test_patch_builds_git_apply_and_returns_touched_files():
    env = FakeEnv(apply_rc=0)
    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    # The patch was staged and applied via git apply.
    assert len(env.staged_paths) == 1
    staged_path = env.staged_paths[0]
    apply_cmds = [c for c in env.cmds if c.startswith("git apply")]
    assert len(apply_cmds) == 3
    assert apply_cmds[0].startswith("git apply --numstat -z")
    assert apply_cmds[1].startswith("git apply --check")
    assert all(staged_path in command for command in apply_cmds)
    assert env.removed_paths == [staged_path]
    assert env.written == {}
    # No fallback needed when git apply succeeds.
    assert not any(c.startswith("patch -p1") for c in env.cmds)
    # Touched files parsed from the +++ b/ headers.
    assert touched == ["tests/test_foo.py"]


def test_git_apply_preflight_failure_skips_without_non_git_fallback():
    env = FakeEnv(apply_check_rc=1)
    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    assert touched == []
    assert any(c.startswith("git apply --check") for c in env.cmds)
    assert not any(c.startswith("patch ") for c in env.cmds)


def test_parallel_injections_use_distinct_owned_staging_files():
    class BarrierEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.arrivals = 0
            self.both_staged = asyncio.Event()

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git apply --numstat"):
                self.arrivals += 1
                if self.arrivals == 2:
                    self.both_staged.set()
                await self.both_staged.wait()
            return await super().exec_cmd(cmd, timeout)

    async def scenario():
        env = BarrierEnv()
        results = await asyncio.gather(
            apply_test_patch(env, SAMPLE_PATCH),
            apply_test_patch(env, SAMPLE_PATCH),
        )
        return env, results

    env, results = run(scenario())

    assert results == [["tests/test_foo.py"], ["tests/test_foo.py"]]
    assert len(env.staged_paths) == 2
    assert len(set(env.staged_paths)) == 2
    assert set(env.removed_paths) == set(env.staged_paths)
    assert env.written == {}


def test_staging_cleanup_failure_after_apply_is_an_isolation_failure():
    env = FakeEnv(remove_exception=OSError("temporary filesystem busy"))

    with pytest.raises(TestPatchIsolationError, match="staging-file cleanup failed") as raised:
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert raised.value.touched_paths == ("tests/test_foo.py",)
    assert env.staged_paths == env.removed_paths


def test_cancel_during_post_apply_cleanup_preserves_paths_for_final_cleanup():
    class BlockingRemovalEnv(FakeEnv):
        def __init__(self):
            super().__init__()
            self.removal_started = asyncio.Event()
            self.release_removal = asyncio.Event()

        async def remove_file(self, path: str) -> None:
            self.removal_started.set()
            await self.release_removal.wait()
            await super().remove_file(path)

    async def scenario():
        env = BlockingRemovalEnv()
        task = asyncio.create_task(apply_test_patch(env, SAMPLE_PATCH))
        await env.removal_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        env.release_removal.set()
        with pytest.raises(TestPatchIsolationError) as raised:
            await task
        return env, raised.value

    env, error = run(scenario())

    assert error.touched_paths == ("tests/test_foo.py",)
    assert isinstance(error.cancellation, asyncio.CancelledError)
    assert env.removed_paths == env.staged_paths
    assert env.written == {}


def test_numstat_parser_preserves_order_for_rename_paths():
    output = (
        "1\t0\ttests/a.py\0"
        "0\t0\t\0tests/old.py\0tests/new.py\0"
    )

    assert _numstat_paths(output) == [
        "tests/a.py",
        "tests/old.py",
        "tests/new.py",
    ]


def test_oversized_test_patch_is_rejected_before_write_or_exec():
    env = FakeEnv()
    patch = "x" * (injection.MAX_TEST_PATCH_BYTES + 1)

    assert run(apply_test_patch(env, patch)) == []
    assert env.written == {}
    assert env.staged_paths == []
    assert env.cmds == []


def test_header_path_count_is_rejected_before_write_or_exec():
    patch = "".join(
        f"--- a/tests/test_{index}.py\n+++ b/tests/test_{index}.py\n"
        for index in range(injection.MAX_TEST_PATCH_PATHS + 1)
    )
    env = FakeEnv()

    assert run(apply_test_patch(env, patch)) == []
    assert env.written == {}
    assert env.staged_paths == []
    assert env.cmds == []


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../../outside.py",
        "/tmp/outside.py",
        "C:\\outside.py",
        "tests\\outside.py",
        ".",
        "tests//outside.py",
        "bad\udcff.py",
    ],
    ids=[
        "parent",
        "absolute",
        "windows-drive",
        "backslash",
        "dot",
        "empty-component",
        "surrogate",
    ],
)
def test_unsafe_header_path_is_rejected_before_staging(unsafe_path):
    patch = (
        f"--- a/{unsafe_path}\n"
        f"+++ b/{unsafe_path}\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    env = FakeEnv()

    assert run(apply_test_patch(env, patch)) == []
    assert env.staged_paths == []
    assert env.cmds == []


def test_unsafe_numstat_path_is_rejected_before_mutating_command():
    env = FakeEnv(numstat_stdout="1\t0\t../../outside.py\0")

    assert run(apply_test_patch(env, SAMPLE_PATCH)) == []
    assert len(env.staged_paths) == 1
    assert env.removed_paths == env.staged_paths
    assert len(env.cmds) == 1
    assert env.cmds[0].startswith("git apply --numstat")


@pytest.mark.parametrize(
    "env",
    [
        FakeEnv(numstat_rc=1),
        FakeEnv(numstat_exception=OSError("numstat transport failed")),
        FakeEnv(numstat_stdout_truncated=True),
        FakeEnv(numstat_stderr_truncated=True),
    ],
    ids=["nonzero", "exception", "stdout-truncated", "stderr-truncated"],
)
def test_incomplete_numstat_skips_injection_before_mutating_command(env):
    assert run(apply_test_patch(env, SAMPLE_PATCH)) == []
    assert len(env.staged_paths) == 1
    assert env.written == {}
    assert env.removed_paths == env.staged_paths
    assert len(env.cmds) == 1
    assert env.cmds[0] == f"git apply --numstat -z {env.staged_paths[0]}"


def test_numstat_added_paths_cannot_exceed_path_count_bound():
    numstat = "".join(
        f"1\t0\ttests/generated_{index}.py\0"
        for index in range(injection.MAX_TEST_PATCH_PATHS)
    )
    env = FakeEnv(numstat_stdout=numstat)

    assert run(apply_test_patch(env, SAMPLE_PATCH)) == []
    assert env.written == {}
    assert env.removed_paths == env.staged_paths
    assert len(env.cmds) == 1
    assert env.cmds[0].startswith("git apply --numstat")


def test_numstat_added_paths_cannot_exceed_aggregate_path_bytes():
    long_path = "tests/" + "x" * injection.MAX_TEST_PATCH_PATH_BYTES
    env = FakeEnv(numstat_stdout=f"1\t0\t{long_path}\0")

    assert run(apply_test_patch(env, SAMPLE_PATCH)) == []
    assert env.written == {}
    assert env.removed_paths == env.staged_paths
    assert len(env.cmds) == 1
    assert env.cmds[0].startswith("git apply --numstat")


def test_rollback_aggregate_deadline_is_an_isolation_failure(monkeypatch):
    monkeypatch.setattr(injection, "MAX_TEST_PATCH_ROLLBACK_SECONDS", 0.0)
    env = FakeEnv(apply_rc=1)

    with pytest.raises(TestPatchIsolationError, match="final deadline"):
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert not any(
        cmd.startswith("git --literal-pathspecs") for cmd in env.cmds
    )


def test_rollback_task_that_consumes_cancel_has_a_final_deadline(monkeypatch):
    monkeypatch.setattr(injection, "MAX_TEST_PATCH_ROLLBACK_SECONDS", 0.02)
    monkeypatch.setattr(
        injection,
        "MAX_TEST_PATCH_FORCED_TASK_STOP_SECONDS",
        0.02,
    )

    class StubbornRollbackEnv(FakeEnv):
        def __init__(self):
            super().__init__(apply_rc=1)

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs checkout -- "):
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        continue
            return await super().exec_cmd(cmd, timeout)

    env = StubbornRollbackEnv()

    with pytest.raises(TestPatchIsolationError, match="final deadline"):
        run(asyncio.wait_for(apply_test_patch(env, SAMPLE_PATCH), timeout=0.5))

    assert env._aborted is True


def test_staging_cleanup_that_consumes_cancel_has_a_final_deadline(monkeypatch):
    monkeypatch.setattr(injection, "MAX_TEST_PATCH_TEMP_CLEANUP_SECONDS", 0.02)
    monkeypatch.setattr(
        injection,
        "MAX_TEST_PATCH_FORCED_TASK_STOP_SECONDS",
        0.02,
    )

    class StubbornRemovalEnv(FakeEnv):
        async def remove_file(self, path: str) -> None:
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

    env = StubbornRemovalEnv()

    with pytest.raises(TestPatchIsolationError, match="cleanup failed"):
        run(asyncio.wait_for(apply_test_patch(env, SAMPLE_PATCH), timeout=0.5))

    assert env._aborted is True


def test_git_apply_exception_rolls_back_before_skipping_injection():
    env = FakeEnv(apply_exception=OSError("git transport failed"))

    assert run(apply_test_patch(env, SAMPLE_PATCH)) == []
    assert any(
        cmd.startswith("git --literal-pathspecs status") for cmd in env.cmds
    )


def test_cancelled_mutating_apply_finishes_rollback_then_propagates_cancel():
    env = FakeEnv(apply_exception=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert any(
        cmd.startswith("git --literal-pathspecs status") for cmd in env.cmds
    )
    assert env.removed_paths == env.staged_paths
    assert env.written == {}


def test_keyboard_interrupting_mutating_apply_rolls_back_then_propagates():
    interrupt = KeyboardInterrupt("stop now")
    env = FakeEnv(apply_exception=interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert raised.value is interrupt
    assert any(
        cmd.startswith("git --literal-pathspecs status") for cmd in env.cmds
    )


def test_cancelled_apply_with_unproven_rollback_carries_paths_and_cancel():
    cancellation = asyncio.CancelledError()
    env = FakeEnv(
        apply_exception=cancellation,
        rollback_status_rc=1,
    )

    with pytest.raises(TestPatchIsolationError) as raised:
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert raised.value.touched_paths == ("tests/test_foo.py",)
    assert raised.value.cancellation is cancellation


def test_caller_cancel_during_nonzero_apply_rollback_waits_for_clean_proof():
    class BlockingRollbackEnv(FakeEnv):
        def __init__(self):
            super().__init__(apply_rc=1)
            self.rollback_started = asyncio.Event()
            self.release_rollback = asyncio.Event()

        async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
            if cmd.startswith("git --literal-pathspecs checkout -- "):
                self.rollback_started.set()
                await self.release_rollback.wait()
            return await super().exec_cmd(cmd, timeout)

    async def scenario():
        env = BlockingRollbackEnv()
        task = asyncio.create_task(apply_test_patch(env, SAMPLE_PATCH))
        await env.rollback_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        env.release_rollback.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert any(
            cmd.startswith("git --literal-pathspecs status") for cmd in env.cmds
        )

    run(scenario())


def test_touched_files_decodes_quoted_and_octal_git_paths():
    patch = (
        'diff --git "a/tests/foo bar.py" "b/tests/foo bar.py"\n'
        '--- "a/tests/foo bar.py"\n'
        '+++ "b/tests/foo bar.py"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        'diff --git "a/tests/test_\\303\\251.py" '
        '"b/tests/test_\\303\\251.py"\n'
        '--- "a/tests/test_\\303\\251.py"\n'
        '+++ "b/tests/test_\\303\\251.py"\n'
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )

    assert _touched_files(patch) == ["tests/foo bar.py", "tests/test_é.py"]


def test_quoted_git_path_is_returned_for_later_exclusion(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    target = tests_dir / "foo bar.py"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    patch = (
        'diff --git "a/tests/foo bar.py" "b/tests/foo bar.py"\n'
        '--- "a/tests/foo bar.py"\n'
        '+++ "b/tests/foo bar.py"\n'
        "@@ -1 +1,2 @@\n"
        " old\n"
        "+injected\n"
    )

    touched = run(apply_test_patch(LocalEnv(tmp_path), patch))

    assert touched == ["tests/foo bar.py"]
    assert target.read_text(encoding="utf-8") == "old\ninjected\n"


def test_failed_git_apply_rolls_back_paths_without_deleting_sibling_files():
    env = FakeEnv(apply_rc=1)

    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    assert touched == []
    assert "git --literal-pathspecs checkout -- tests/test_foo.py" in env.cmds
    assert "git --literal-pathspecs clean -fq -- tests/test_foo.py" in env.cmds
    assert not any("tests/test_foo.py.orig" in cmd for cmd in env.cmds)
    assert not any("tests/test_foo.py.rej" in cmd for cmd in env.cmds)
    assert any(
        cmd.startswith("git --literal-pathspecs status --porcelain=v1 -z --")
        for cmd in env.cmds
    )


def test_failed_partial_apply_raises_when_rollback_state_is_unknown():
    env = FakeEnv(
        apply_rc=1,
        rollback_rc=1,
        rollback_status_rc=1,
    )

    with pytest.raises(TestPatchIsolationError) as raised:
        run(apply_test_patch(env, SAMPLE_PATCH))

    assert raised.value.touched_paths == ("tests/test_foo.py",)
    assert "could not prove clean state" in str(raised.value)


def test_failed_partial_apply_raises_when_status_remains_dirty():
    env = FakeEnv(
        apply_rc=1,
        rollback_status_stdout=" M tests/test_foo.py\0",
    )

    with pytest.raises(TestPatchIsolationError, match="tests/test_foo.py"):
        run(apply_test_patch(env, SAMPLE_PATCH))


def test_failed_partial_apply_raises_when_status_output_is_truncated():
    env = FakeEnv(
        apply_rc=1,
        rollback_status_truncated=True,
    )

    with pytest.raises(TestPatchIsolationError, match="stdout_truncated=True"):
        run(apply_test_patch(env, SAMPLE_PATCH))


def test_partial_apply_rollback_uses_literal_pathspecs_for_magic_names():
    patch = SAMPLE_PATCH.replace("tests/test_foo.py", ":(glob)tests/*.py")
    env = FakeEnv(apply_rc=1)

    assert run(apply_test_patch(env, patch)) == []

    rollback_commands = [
        cmd for cmd in env.cmds if cmd.startswith("git --literal-pathspecs")
    ]
    assert len(rollback_commands) == 3
    assert all(":(glob)tests/" in cmd for cmd in rollback_commands)


class LocalEnv(Environment):
    def __init__(self, root):
        self.root = root
        self.cmds: list[str] = []

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        (self.root / "test.patch").write_text(content, encoding="utf-8")
        return "test.patch"

    async def remove_file(self, path: str) -> None:
        (self.root / path).unlink(missing_ok=True)

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        result = subprocess.run(
            cmd,
            cwd=self.root,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return ExecResult(result.returncode, result.stdout, result.stderr)


def test_failed_git_preflight_never_mutates_real_worktree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("old-a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("old-b\n", encoding="utf-8")
    (tmp_path / "a.txt.orig").write_text("preexisting orig\n", encoding="utf-8")
    (tmp_path / "a.txt.rej").write_text("preexisting reject\n", encoding="utf-8")
    patch = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old-a\n"
        "+new-a\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -1 +1 @@\n"
        "-does-not-match\n"
        "+new-b\n"
    )

    touched = run(apply_test_patch(LocalEnv(tmp_path), patch))

    assert touched == []
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "old-a\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "old-b\n"
    assert (tmp_path / "a.txt.orig").read_text() == "preexisting orig\n"
    assert (tmp_path / "a.txt.rej").read_text() == "preexisting reject\n"


def test_git_preflight_rejection_cannot_fall_back_through_repo_symlink(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    target = outside / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    (repo / "link").symlink_to(outside, target_is_directory=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "link"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    patch = (
        "diff --git a/link/target.txt b/link/target.txt\n"
        "--- a/link/target.txt\n"
        "+++ b/link/target.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    env = LocalEnv(repo)

    assert run(apply_test_patch(env, patch)) == []

    assert target.read_text(encoding="utf-8") == "old\n"
    assert not any(cmd.startswith("patch ") for cmd in env.cmds)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert status == ""


def test_apply_test_patch_empty_patch_is_noop():
    env = FakeEnv()
    assert run(apply_test_patch(env, "")) == []
    assert env.cmds == []  # nothing executed for an empty patch
