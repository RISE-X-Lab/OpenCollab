from __future__ import annotations

import shlex
import shutil
from pathlib import Path

import pytest

from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters._env_worktree import WorktreeEnvironment
from opencollab.adapters._env_worktree_submodules import _initialize_source_available_submodules
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


def _dependency(tmp_path: Path, name: str) -> Path:
    parent = tmp_path / name
    parent.mkdir()
    return _repository(parent)


@pytest.mark.parametrize("nested", [False, True])
async def test_candidate_reads_committed_source_available_submodules(tmp_path, monkeypatch, nested):
    dependency = _dependency(tmp_path, "dependency")
    dependency_path = "vendor/dependency"
    if nested:
        middle = _dependency(tmp_path, "middle")
        _git(middle, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "vendor/leaf")
        _git(middle, "config", "-f", ".gitmodules", "submodule.vendor/leaf.url", "https://example.invalid/leaf.git")
        _git(middle, "add", ".gitmodules", "vendor/leaf")
        _git(middle, "commit", "-m", "nested dependency")
        dependency = middle
        dependency_path += "/vendor/leaf"
    source = _repository(tmp_path)
    _git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "vendor/dependency")
    if nested:
        _git(
            source / "vendor/dependency", "-c", "protocol.file.allow=always",
            "-c", f"submodule.vendor/leaf.url={tmp_path / 'dependency/repo'}",
            "submodule", "update", "--init", "--no-fetch", "--", "vendor/leaf",
        )
    _git(source, "config", "-f", ".gitmodules", "submodule.vendor/dependency.url", "https://example.invalid/dep.git")
    _git(source, "add", ".gitmodules", "vendor/dependency")
    _git(source, "commit", "-m", "committed dependency")
    assert (source / dependency_path / "source.py").read_text() == "value = 1\n"
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    base = LocalEnvironment(str(source))
    lease = None
    try:
        lease = await EnvCandidateWorkspace(base).acquire("dependency")
        assert await lease.environment.read_file(f"{dependency_path}/source.py") == "value = 1\n"
        assert await lease.diff() == ""
        candidate_path = Path(lease.candidate_workspace)
    finally:
        if lease is not None:
            await lease.cleanup()
        await base.cleanup()
    assert not candidate_path.exists()
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1


def _partly_initialized_source(tmp_path: Path) -> Path:
    dependency = _dependency(tmp_path, "dependency")
    source = _repository(tmp_path)
    for name in ("required", "optional"):
        _git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), f"vendor/{name}")
    _git(source, "commit", "-am", "committed dependencies")
    _git(source, "submodule", "deinit", "-f", "--", "vendor/optional")
    assert _git(source, "status", "--porcelain") == ""
    return source


async def test_candidate_copies_initialized_dependency_and_leaves_optional_empty(tmp_path):
    source = _partly_initialized_source(tmp_path)
    base = LocalEnvironment(str(source))
    lease = None
    try:
        lease = await EnvCandidateWorkspace(base).acquire("partly-initialized")
        assert await lease.environment.read_file("vendor/required/source.py") == "value = 1\n"
        assert await lease.environment.read_file("source.py") == "value = 1\n"
        optional = Path(lease.candidate_workspace) / "vendor/optional"
        assert optional.is_dir()
        assert list(optional.iterdir()) == []
        assert await lease.diff() == ""
        assert _git(source, "status", "--porcelain") == ""
    finally:
        if lease is not None:
            await lease.cleanup()
        await base.cleanup()
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1


async def test_candidate_from_repository_subdirectory_initializes_only_scoped_submodules(tmp_path):
    dependency = _dependency(tmp_path, "nested-dependency")
    outside = _dependency(tmp_path, "outside-dependency")
    repository = _repository(tmp_path)
    scoped_source = repository / "src"
    scoped_source.mkdir()
    (scoped_source / "app.py").write_text("app = True\n")
    _git(repository, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "src/vendor/required")
    _git(repository, "-c", "protocol.file.allow=always", "submodule", "add", str(outside), "vendor/outside")
    _git(repository, "commit", "-am", "add source and modules")
    _git(repository, "submodule", "deinit", "-f", "--", "vendor/outside")
    base = LocalEnvironment(str(scoped_source))
    lease = None
    try:
        lease = await EnvCandidateWorkspace(base).acquire("nested-source")
        assert await lease.environment.read_file("vendor/required/source.py") == "value = 1\n"
        optional = Path(lease.candidate_workspace) / "vendor/outside"
        assert optional.is_dir() and list(optional.iterdir()) == []
    finally:
        if lease is not None:
            await lease.cleanup()
        await base.cleanup()


async def test_worktree_environment_keeps_requiring_initialized_source_submodules(tmp_path):
    source = _partly_initialized_source(tmp_path)
    environment = WorktreeEnvironment(str(source))
    try:
        with pytest.raises(RuntimeError, match="not initialized"):
            await environment.setup()
    finally:
        await environment.cleanup()
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("damage", ["invalid_path", "external_module", "broken_metadata"])
async def test_source_submodule_errors_are_reported(tmp_path, damage):
    source = _partly_initialized_source(tmp_path)
    required = source / "vendor/required"
    if damage == "invalid_path":
        _git(source, "config", "-f", ".gitmodules", "submodule.vendor/required.path", "../../dependency/repo")
    elif damage == "external_module":
        shutil.rmtree(required)
        required.symlink_to(tmp_path / "dependency/repo", target_is_directory=True)
    else:
        (required / ".git").write_text("gitdir: missing-git-directory\n")
    base = LocalEnvironment(str(source))

    async def git_in(workspace, *arguments):
        return await base.exec_cmd(shlex.join(("git", "-C", workspace, *arguments)))

    try:
        with pytest.raises(RuntimeError):
            await _initialize_source_available_submodules(
                str(source), str(tmp_path / "target"), git_in=git_in, require_initialized=False,
            )
    finally:
        await base.cleanup()
