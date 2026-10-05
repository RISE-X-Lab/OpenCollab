"""Candidate delivery retains executable bits independently of Git filemode config."""

from __future__ import annotations

from pathlib import Path

import pytest

from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


@pytest.mark.parametrize("filemode", [False, True])
@pytest.mark.parametrize("initial,target", [(0o644, 0o755), (0o755, 0o644)])
@pytest.mark.parametrize("edit_content", [False, True])
async def test_candidate_delivers_actual_script_mode(tmp_path, filemode, initial, target, edit_content):
    repo = _repository(tmp_path)
    script = repo / "message.sh"
    script.write_text("#!/bin/sh\necho original\n")
    script.chmod(initial)
    _git(repo, "add", "message.sh")
    _git(repo, "commit", "-m", "\u8bb0\u5f55\u811a\u672c\u6587\u4ef6")
    _git(repo, "config", "core.filemode", str(filemode).lower())
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    lease = None
    try:
        lease = await workspace.acquire("script-mode")
        candidate_script = Path(lease.candidate_workspace) / "message.sh"
        if edit_content:
            candidate_script.write_text("#!/bin/sh\necho edited\n")
        candidate_script.chmod(target)
        candidate_execution = await lease.environment.exec_cmd("./message.sh")
        assert (candidate_execution.returncode == 0) == (target == 0o755)
        patch = await lease.diff()
        assert f"old mode 100{initial:o}" in patch
        assert f"new mode 100{target:o}" in patch
        await workspace.adopt(patch)
        assert script.stat().st_mode & 0o777 == target
        assert script.read_text() == candidate_script.read_text()
        delivered_execution = await environment.exec_cmd("./message.sh")
        assert delivered_execution.returncode == candidate_execution.returncode
        assert delivered_execution.stdout == candidate_execution.stdout
        assert _git(repo, "config", "core.filemode").strip() == str(filemode).lower()
        assert _git(repo, "ls-files", "--stage", "message.sh").startswith(f"100{initial:o} ")
    finally:
        if lease is not None:
            await lease.cleanup()
        await environment.cleanup()


@pytest.mark.parametrize("filemode", [False, True])
@pytest.mark.parametrize("initial,target", [(0o644, 0o755), (0o755, 0o644)])
@pytest.mark.parametrize("edit_source_content", [False, True])
async def test_candidate_inherits_source_script_mode_as_initial_contents(
    tmp_path, filemode, initial, target, edit_source_content,
):
    repo = _repository(tmp_path)
    script = repo / "message.sh"
    script.write_text("#!/bin/sh\necho original\n")
    script.chmod(initial)
    _git(repo, "add", "message.sh")
    _git(repo, "commit", "-m", "\u8bb0\u5f55\u811a\u672c\u6587\u4ef6")
    _git(repo, "config", "core.filemode", str(filemode).lower())
    script.chmod(target)
    if edit_source_content:
        script.write_text("#!/bin/sh\necho inherited\n")
        (repo / "source.py").write_text("value = 2\n")
        _git(repo, "add", "source.py")
    source_contents = script.read_text()
    source_status = _git(repo, "status", "--porcelain=v1")
    source_index = _git(repo, "ls-files", "--stage")
    source_config = (repo / ".git" / "config").read_bytes()
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    lease = None
    try:
        source_execution = await environment.exec_cmd("./message.sh")
        lease = await workspace.acquire("source-script-mode")
        candidate_script = Path(lease.candidate_workspace) / "message.sh"
        candidate_execution = await lease.environment.exec_cmd("./message.sh")
        assert candidate_execution.returncode == source_execution.returncode
        assert candidate_execution.stdout == source_execution.stdout
        assert candidate_script.stat().st_mode & 0o777 == target
        assert candidate_script.read_text() == source_contents
        assert await lease.diff() == ""
        source_patch = await workspace.source_diff()
        assert f"old mode 100{initial:o}" in source_patch
        assert f"new mode 100{target:o}" in source_patch
        assert _git(repo, "status", "--porcelain=v1") == source_status
        assert _git(repo, "ls-files", "--stage") == source_index
        assert (repo / ".git" / "config").read_bytes() == source_config

        candidate_script.write_text("#!/bin/sh\necho edited\n")
        patch = await lease.diff()
        assert f"-{source_contents.splitlines()[1]}" in patch
        assert "+echo edited" in patch
        assert "old mode" not in patch
        assert "new mode" not in patch
        assert "source.py" not in patch
        await workspace.adopt(patch)
        assert script.stat().st_mode & 0o777 == target
        assert script.read_text() == candidate_script.read_text()
        delivered_execution = await environment.exec_cmd("./message.sh")
        candidate_execution = await lease.environment.exec_cmd("./message.sh")
        assert delivered_execution.returncode == candidate_execution.returncode
        assert delivered_execution.stdout == candidate_execution.stdout
        assert _git(repo, "ls-files", "--stage") == source_index
        assert (repo / ".git" / "config").read_bytes() == source_config
    finally:
        if lease is not None:
            await lease.cleanup()
        await environment.cleanup()
