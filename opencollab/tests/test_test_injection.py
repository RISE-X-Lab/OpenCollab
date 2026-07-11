"""Tests for ``harness.test_injection.apply_test_patch``.

The helper stages a benchmark test_patch inside the env, runs ``git apply``,
and returns the test files it touched. A failing apply must never raise — it
logs and returns ``[]`` so a bad patch never aborts the eval run.
"""

from __future__ import annotations

import asyncio

from opencollab.adapters.env import Environment, ExecResult
from opencollab.harness.test_injection import apply_test_patch


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

    def __init__(self, *, apply_rc: int = 0, patch_rc: int = 0):
        self.cmds: list[str] = []
        self.written: dict[str, str] = {}
        self._apply_rc = apply_rc
        self._patch_rc = patch_rc

    async def write_file(self, path: str, content: str) -> None:
        self.written[path] = content

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self.cmds.append(cmd)
        if cmd.startswith("git apply"):
            return ExecResult(returncode=self._apply_rc, stdout="", stderr="apply boom")
        if cmd.startswith("patch -p1"):
            return ExecResult(returncode=self._patch_rc, stdout="", stderr="patch boom")
        return ExecResult(returncode=0, stdout="", stderr="")


def test_apply_test_patch_builds_git_apply_and_returns_touched_files():
    env = FakeEnv(apply_rc=0)
    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    # The patch was staged and applied via git apply.
    assert env.written  # patch content written to the staging file
    staged_path = next(iter(env.written))
    apply_cmds = [c for c in env.cmds if c.startswith("git apply")]
    assert len(apply_cmds) == 1
    assert staged_path in apply_cmds[0]
    # No fallback needed when git apply succeeds.
    assert not any(c.startswith("patch -p1") for c in env.cmds)
    # Touched files parsed from the +++ b/ headers.
    assert touched == ["tests/test_foo.py"]


def test_apply_test_patch_returns_empty_on_failure_without_raising():
    # Both git apply AND the patch fallback fail -> [] and no exception.
    env = FakeEnv(apply_rc=1, patch_rc=1)
    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    assert touched == []
    # It tried git apply, then the patch -p1 fallback.
    assert any(c.startswith("git apply") for c in env.cmds)
    assert any(c.startswith("patch -p1") for c in env.cmds)


def test_apply_test_patch_falls_back_to_patch_when_git_apply_fails():
    # git apply fails, patch -p1 succeeds -> touched files still returned.
    env = FakeEnv(apply_rc=1, patch_rc=0)
    touched = run(apply_test_patch(env, SAMPLE_PATCH))

    assert touched == ["tests/test_foo.py"]


def test_apply_test_patch_empty_patch_is_noop():
    env = FakeEnv()
    assert run(apply_test_patch(env, "")) == []
    assert env.cmds == []  # nothing executed for an empty patch
