"""A shortened partial delivery keeps full files until a complete export."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.env import WorktreeEnvironment
from tests.support.worktree_delivery_support import git, make_repo


@pytest.mark.parametrize("git_repository", [False, True])
async def test_retained_capture_can_be_exported_and_released(tmp_path, git_repository):
    source = make_repo(tmp_path / "source") if git_repository else tmp_path / "source"
    if not git_repository:
        source.mkdir()
        (source / "initial.txt").write_text("initial\n")
    env = WorktreeEnvironment(str(source))
    workspace = Path(await env.setup())
    content = "payload line\n" * 3000
    try:
        await env.write_file("payload.txt", content)
        original = await env.get_diff()
        assert env.recovery_location is None
        assert Path(env.retain_changes()) == workspace
        assert Path(env.recovery_location) == workspace
        for _ in range(2):
            with pytest.raises(RuntimeError, match="unexported|undelivered"):
                await env.cleanup()
            assert (workspace / "payload.txt").read_text() == content
        recovered = await env.get_diff()
        assert recovered == original and env.recovery_location is None
        subprocess.run(["git", "apply", "-"], cwd=source, input=recovered, text=True, check=True, capture_output=True)
        assert (source / "payload.txt").read_text() == content
        await env.cleanup()
        assert not workspace.exists()
        await env.cleanup()
    finally:
        if workspace.exists():
            await env.get_diff()
            await env.cleanup()
    if git_repository:
        assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 1
