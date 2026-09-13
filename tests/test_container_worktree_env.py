"""A worktree inside the task container reports the same evidence as one outside.

The Team arm's per-agent attribution rests on four values a worktree reports --
the base its diff was measured against, where its HEAD stands, and the commits
it made itself -- and a handoff is read as ``tester.diff_base in
coder.commits``. Those readings have to mean the same thing when the repository
under test lives inside a container, because that is where the evaluation
harness keeps it and the host is never allowed to see it.

These tests run real ``git`` against real linked worktrees, with ``docker exec``
replaced by running the same argv locally. What is deliberately not covered here
is the Docker plumbing itself -- ``tests/test_docker_env.py`` owns that. What is
covered is everything this class decides: which commands it builds, where it
puts the worktree, and what it concludes from what Git answers.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters import _env_docker as docker_module
from opencollab.adapters._env_container_worktree import ContainerWorktreeEnvironment
from opencollab.adapters._env_process import ProcessResult
from opencollab.adapters._env_process import run_process as real_run_process
from opencollab.application._scheduler_team import _parse_worktree_diff

CONTAINER_ID = "c" * 64


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path, *, object_format: str = "sha1"):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main", f"--object-format={object_format}")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _changed_files(diff: str) -> list[str]:
    """What the scheduler's ``worktree_changes`` record would list."""
    return [path for path, _ in _parse_worktree_diff(diff)]


@pytest.fixture
def local_docker(monkeypatch):
    """Run what would go to ``docker exec`` as a local process instead.

    The container is the only thing faked. Everything the command does -- the
    Git it runs, the files it touches, the worktrees it registers -- is real, so
    a wrong command or a wrong conclusion still fails the test.
    """

    async def shim(command, **kwargs):
        argv = list(command)
        assert argv[0] == "docker", argv
        if argv[1] == "inspect":
            return ProcessResult(0, f"{CONTAINER_ID}\t/oc-test\ttrue".encode(), b"")
        assert argv[1] == "exec", argv
        rest = argv[2:]
        workdir = None
        while rest and rest[0] != "--":
            if rest[0] == "-w":
                workdir = rest[1]
                rest = rest[2:]
            else:  # -i, and anything else Docker takes before the container
                rest = rest[1:]
        assert rest[0] == "--" and rest[1] == CONTAINER_ID, rest
        return await real_run_process(
            tuple(rest[2:]),
            shell=False,
            cwd=workdir,
            timeout=kwargs.get("timeout", 60.0),
            input_bytes=kwargs.get("input_bytes"),
        )

    monkeypatch.setattr(docker_module, "run_process", shim)
    return shim


def _env(repo, tmp_path, branch: str) -> ContainerWorktreeEnvironment:
    return ContainerWorktreeEnvironment(
        container_id=CONTAINER_ID,
        repository_root=str(repo),
        worktree_root=str(tmp_path / "worktrees"),
        branch_name=branch,
    )


async def test_verified_write_accepts_bsd_wc_padding(local_docker, monkeypatch, tmp_path):
    """A BSD-style padded ``wc -c`` count still verifies the write."""
    repo = _repo(tmp_path / "testbed")
    env = _env(repo, tmp_path, "container-bsd-wc")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_wc = fake_bin / "wc"
    fake_wc.write_text("#!/bin/sh\nprintf '      5\\n'\n", encoding="utf-8")
    fake_wc.chmod(0o755)
    try:
        await env.setup()
        monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
        await env.write_file("padded.txt", "hello")
        assert Path(env.workspace, "padded.txt").read_text(encoding="utf-8") == "hello"
    finally:
        await env.cleanup()


async def test_an_agents_own_commits_stay_in_its_own_diff(local_docker, tmp_path):
    repo = _repo(tmp_path / "testbed")
    env = _env(repo, tmp_path, "container-coder")
    try:
        await env.setup()
        creation_base = _git(env.workspace, "rev-parse", "HEAD")
        await env.write_file("f1.txt", "coder work\n")
        assert (await env.exec_cmd("git add -A && git commit -qm coder")).returncode == 0

        diff = await env.get_diff()

        assert _changed_files(diff) == ["f1.txt"]
        assert env.diff_base == creation_base
        assert env.head_commit != creation_base
        assert env.own_commits == (env.head_commit,)
        assert env.own_commit_count == 1
    finally:
        await env.cleanup()


async def test_a_checked_out_teammate_commit_is_not_this_agents_work(
    local_docker, tmp_path
):
    """The join the information-flow reading depends on, inside a container."""
    repo = _repo(tmp_path / "testbed")
    coder = _env(repo, tmp_path, "container-handoff-coder")
    tester = _env(repo, tmp_path, "container-handoff-tester")
    try:
        await coder.setup()
        await coder.write_file("f1.txt", "coder work\n")
        assert (await coder.exec_cmd("git add -A && git commit -qm coder")).returncode == 0
        handoff_sha = _git(coder.workspace, "rev-parse", "HEAD")
        assert _changed_files(await coder.get_diff()) == ["f1.txt"]

        await tester.setup()
        assert (await tester.exec_cmd(f"git checkout -q {handoff_sha}")).returncode == 0
        await tester.write_file("f2.txt", "tester work\n")

        diff = await tester.get_diff()

        assert _changed_files(diff) == ["f2.txt"]
        assert "f1.txt" not in diff
        assert tester.diff_base == handoff_sha
        assert tester.diff_base in coder.own_commits
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_a_sibling_cannot_see_work_that_was_not_committed(local_docker, tmp_path):
    """What isolation buys: an edit nobody committed reaches nobody else."""
    repo = _repo(tmp_path / "testbed")
    coder = _env(repo, tmp_path, "container-solo-coder")
    tester = _env(repo, tmp_path, "container-solo-tester")
    try:
        await coder.setup()
        await tester.setup()
        await coder.write_file("only_here.txt", "uncommitted\n")

        listing = await tester.exec_cmd("ls only_here.txt")

        assert listing.returncode != 0
        assert not (repo / "only_here.txt").exists()
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_the_worktree_is_not_inside_the_repository_it_checks_out(
    local_docker, tmp_path
):
    """An archive of the repository root is what the harness reads its patch from.

    A worktree nested inside that root would arrive in the archive as a
    directory full of files no agent wrote.
    """
    repo = _repo(tmp_path / "testbed")
    env = _env(repo, tmp_path, "container-outside")
    try:
        await env.setup()
        await env.write_file("f1.txt", "coder work\n")

        assert not str(env.workspace).startswith(f"{repo}/")
        assert _git(repo, "status", "--porcelain") == ""
    finally:
        await env.cleanup()


async def test_an_agents_commits_do_not_move_the_branch_it_leased(
    local_docker, tmp_path
):
    """The claimed ref is an ownership lease, not the worktree's HEAD.

    A detached worktree keeps its own commits off that ref, which is what lets
    cleanup delete it only if it still stands where it was claimed -- so a ref
    something else advanced stays visible instead of being quietly removed.
    """
    repo = _repo(tmp_path / "testbed")
    env = _env(repo, tmp_path, "container-lease")
    try:
        await env.setup()
        leased_at = _git(repo, "rev-parse", "refs/heads/container-lease")
        await env.write_file("f1.txt", "coder work\n")
        assert (await env.exec_cmd("git add -A && git commit -qm coder")).returncode == 0

        assert _git(env.workspace, "rev-parse", "HEAD") != leased_at
        assert _git(repo, "rev-parse", "refs/heads/container-lease") == leased_at
        # Empty means HEAD is detached; a checked-out branch would name it.
        assert _git(env.workspace, "branch", "--show-current") == ""
    finally:
        await env.cleanup()
    assert "container-lease" not in _git(repo, "branch", "--list")


async def test_cleanup_gives_back_the_worktree_and_its_branch(local_docker, tmp_path):
    repo = _repo(tmp_path / "testbed")
    env = _env(repo, tmp_path, "container-released")
    await env.setup()
    workspace = env.workspace
    assert "container-released" in _git(repo, "worktree", "list")

    await env.cleanup()

    assert "container-released" not in _git(repo, "worktree", "list")
    assert "container-released" not in _git(repo, "branch", "--list")
    assert not (tmp_path / "worktrees" / "container-released").exists()
    assert workspace.endswith("container-released")


async def test_a_handoff_is_read_in_a_sha256_repository_too(local_docker, tmp_path):
    """The harness rebuilds some repositories with 64-character object ids."""
    repo = _repo(tmp_path / "testbed", object_format="sha256")
    coder = _env(repo, tmp_path, "container-sha256-coder")
    tester = _env(repo, tmp_path, "container-sha256-tester")
    try:
        await coder.setup()
        await coder.write_file("f1.txt", "coder work\n")
        assert (await coder.exec_cmd("git add -A && git commit -qm coder")).returncode == 0
        handoff_sha = _git(coder.workspace, "rev-parse", "HEAD")
        assert len(handoff_sha) == 64
        assert _changed_files(await coder.get_diff()) == ["f1.txt"]

        await tester.setup()
        assert (await tester.exec_cmd(f"git checkout -q {handoff_sha}")).returncode == 0
        await tester.write_file("f2.txt", "tester work\n")

        assert _changed_files(await tester.get_diff()) == ["f2.txt"]
        assert tester.diff_base == handoff_sha
        assert tester.diff_base in coder.own_commits
    finally:
        await tester.cleanup()
        await coder.cleanup()


async def test_a_repository_hardened_by_its_harness_still_reports_a_diff(
    local_docker, tmp_path
):
    """The three local settings the evaluation harness writes into its snapshot."""
    repo = _repo(tmp_path / "testbed")
    _git(repo, "config", "core.attributesFile", "/dev/null")
    _git(repo, "config", "core.fsmonitor", "false")
    _git(repo, "config", "diff.ignoreSubmodules", "all")
    env = _env(repo, tmp_path, "container-hardened")
    try:
        await env.setup()
        await env.write_file("f1.txt", "coder work\n")

        assert _changed_files(await env.get_diff()) == ["f1.txt"]
    finally:
        await env.cleanup()


@pytest.mark.parametrize(
    ("repository_root", "worktree_root"),
    [("relative/testbed", "/worktrees"), ("/testbed", "worktrees"), ("/", "/worktrees")],
)
def test_container_paths_must_be_absolute_and_not_the_root(repository_root, worktree_root):
    with pytest.raises(ValueError):
        ContainerWorktreeEnvironment(
            container_id=CONTAINER_ID,
            repository_root=repository_root,
            worktree_root=worktree_root,
        )
