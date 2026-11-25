from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

import pytest

from opencollab.adapters import _candidate_physical
from opencollab.adapters._env_local import LocalEnvironment
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.workflow import WorkflowContext
from opencollab.environments import attach_container
from tests.runtime.test_team_candidate_delivery import _adoption_scheduler
from tests.support.worktree_delivery_support import CONTAINER_ID, git, make_repo
from tests.support.worktree_delivery_support import worktree_transport as worktree_transport


@pytest.mark.parametrize("backend", ["workflow", "team"])
@pytest.mark.parametrize("change", ["new", "retarget", "rename"])
@pytest.mark.parametrize("staged", [False, True])
async def test_candidate_delivery_preserves_tested_links_when_git_disables_links(
    tmp_path, backend, change, staged,
):
    source = make_repo(tmp_path / "source")
    (source / "data.txt").write_text("42\n")
    (source / "other.txt").write_text("1\n")
    if change != "new":
        (source / "alias").symlink_to("other.txt" if change == "retarget" else "data.txt")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Prepare source links")
    git(source, "config", "core.symlinks", "false")
    original_index = (source / ".git/index").read_bytes()
    environment = LocalEnvironment(str(source))
    pool = WorktreePool(str(source), use_worktrees=True, enable_adoption=True)
    workspace = EnvCandidateWorkspace(environment)
    leases = []
    acquire = workspace.acquire

    async def remember(label):
        lease = await acquire(label)
        leases.append(lease)
        return lease

    workspace.acquire = remember
    context = WorkflowContext(object(), candidate_workspace=workspace)
    link_name = 'renamed [link] "x"' if change == "rename" else "alias"

    async def edit(candidate_environment):
        root = Path(candidate_environment.workspace)
        if change == "rename":
            (root / "alias").rename(root / link_name)
        else:
            (root / "alias").unlink(missing_ok=True)
            (root / "alias").symlink_to("data.txt")
        (root / "app.py").write_text(
            f"from pathlib import Path\nprint(Path({link_name!r}).read_text().strip())\n",
        )
        (root / "plain.txt").write_text("candidate text\n")
        (root / "binary.dat").write_bytes(b"candidate\0\xff\n")
        (root / "run.sh").write_text('#!/bin/sh\nprintf "42\\n"\n')
        (root / "run.sh").chmod(0o755)
        if staged:
            git(root, "add", "--all")
        tested = await candidate_environment.exec_cmd(shlex.join((sys.executable, "-B", "app.py")))
        assert (tested.returncode, tested.stdout) == (0, "42\n")
        assert (await candidate_environment.exec_cmd("./run.sh")).stdout == "42\n"

    coder = None
    try:
        if backend == "workflow":
            async def candidate(child, _args):
                await edit(leases[0].environment)
                return "tested link"
            selected = await context.candidate_workflow(candidate, {}, label="link-delivery")
            assert link_name in selected.delivery_paths
            await context.adopt_candidate(selected)
            assert not Path(leases[0].candidate_workspace).exists()
        else:
            coder = await pool.acquire("coder")
            await edit(coder)
            scheduler = _adoption_scheduler(pool, coder)
            await scheduler._append_worktree_diff(coder, "tested", aid=7, role="coder")
            assert link_name in scheduler._completed_coder_diffs[7].delivery_paths
            await scheduler.adopt_agent(0, 7)
            tester = await pool.acquire("tester")
            tested = await tester.exec_cmd(shlex.join((sys.executable, "-B", "app.py")))
            assert (tested.returncode, tested.stdout) == (0, "42\n")
            assert await tester.get_diff() == ""
        assert (source / link_name).is_symlink()
        assert (source / link_name).readlink() == Path("data.txt")
        adopted = await environment.exec_cmd(shlex.join((sys.executable, "-B", "app.py")))
        assert (adopted.returncode, adopted.stdout) == (0, "42\n")
        assert (source / "plain.txt").read_bytes() == b"candidate text\n"
        assert (source / "binary.dat").read_bytes() == b"candidate\0\xff\n"
        assert (source / "run.sh").stat().st_mode & 0o111 == 0o111
        assert (source / ".git/index").read_bytes() == original_index
    finally:
        for lease in leases:
            await lease.cleanup()
        if coder is not None:
            await coder.get_diff()
        await pool.release()
        await environment.cleanup()
    assert git(source, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("value", ["false", "0", "off", "no", ""])
async def test_link_conversion_keeps_plain_file_adoption_on_its_existing_path(tmp_path, value, monkeypatch):
    source = make_repo(tmp_path / "source")
    git(source, "config", "core.symlinks", value)
    environment = LocalEnvironment(str(source))
    workspace = EnvCandidateWorkspace(environment)
    context = WorkflowContext(object(), candidate_workspace=workspace)

    def unexpected_physical_apply(*args, **kwargs):
        raise AssertionError("plain file adoption constructed physical metadata")

    async def candidate(child, _args):
        Path(child.workspace_root, "app.py").write_text("print(42)\n")

    monkeypatch.setattr(_candidate_physical, "_physical_git", unexpected_physical_apply)
    try:
        selected = await context.candidate_workflow(candidate, {}, label="plain-file")
        assert selected.delivery_paths == ()
        await context.adopt_candidate(selected)
        adopted = await environment.exec_cmd(shlex.join((sys.executable, "-B", "app.py")))
        assert (adopted.returncode, adopted.stdout) == (0, "42\n")
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("transport", ["local", "container"])
@pytest.mark.parametrize("paths", ["empty", "quoted", "long"])
async def test_untracked_link_probe_preserves_nul_paths_and_empty_input(
    tmp_path, worktree_transport, transport, paths,
):
    source = make_repo(tmp_path / 'source with "quotes"')
    git(source, "rm", "initial.txt", ".gitignore")
    git(source, "commit", "-qm", "Prepare empty link probe")
    git(source, "config", "core.symlinks", "false")
    environment = (
        attach_container(container_id=CONTAINER_ID, workspace=str(source))
        if transport == "container" else LocalEnvironment(str(source))
    )
    original_index = (source / ".git/index").read_bytes()
    try:
        available = await environment.exec_cmd("command -v bash")
        assert available.returncode == 0
        assert not await _candidate_physical._has_worktree_conversions(environment, str(source))
        if paths != "empty":
            directory = source / "directory with spaces\n'\""
            directory.mkdir()
            if paths == "long":
                for number in range(1800):
                    (directory / (f"item {number:04d} " + "x" * 160 + " '\".txt")).write_text("data\n")
                assert not await _candidate_physical._has_worktree_conversions(environment, str(source))
            link = directory / 'zz link\nwith "quotes" and \'single\''
            link.symlink_to("missing target.txt")
            assert await _candidate_physical._has_worktree_conversions(environment, str(source))
            excluded = (directory.relative_to(source).as_posix(),)
            assert not await _candidate_physical._has_worktree_conversions(
                environment, str(source), exclude_untracked_paths=excluded,
            )
        assert (source / ".git/index").read_bytes() == original_index
    finally:
        await environment.cleanup()


async def test_link_probe_without_bash_keeps_executable_physical_delivery(tmp_path):
    source = make_repo(tmp_path / "source")
    (source / "data.txt").write_text("42\n")
    (source / "app.py").write_text('from pathlib import Path\nprint(Path("alias").read_text().strip())\n')
    git(source, "add", ".")
    git(source, "commit", "-qm", "Prepare portable link delivery")
    git(source, "config", "core.symlinks", "false")
    (source / "alias").symlink_to("data.txt")
    original_index = (source / ".git/index").read_bytes()
    binaries = tmp_path / "commands"
    binaries.mkdir()
    for name in ("git", "mktemp", "rm", "cat", "grep", "mkdir", "sh", "sed", "awk", "basename",
                 "dirname", "uname", "expr", "tr", "wc", "cut", "pwd", "sort", "cp", "stat", "mv", "chmod"):
        command = shutil.which(name)
        assert command is not None
        (binaries / name).symlink_to(command)
    assert shutil.which("bash", path=str(binaries)) is None

    class PosixEnvironment(LocalEnvironment):
        async def exec_cmd(self, command, **kwargs):
            return await super().exec_cmd(f"export PATH={shlex.quote(str(binaries))}; {command}", **kwargs)

    environment = PosixEnvironment(str(source))
    context = WorkflowContext(object(), candidate_workspace=EnvCandidateWorkspace(environment))

    async def candidate(child, _args):
        Path(child.workspace_root, "data.txt").write_text("43\n")

    try:
        assert (await environment.exec_cmd("command -v bash")).returncode != 0
        assert await _candidate_physical._has_worktree_conversions(environment, str(source))
        selected = await context.candidate_workflow(candidate, {}, label="portable-links")
        await context.adopt_candidate(selected)
        tested = await environment.exec_cmd(shlex.join((sys.executable, "-B", "app.py")))
        assert (tested.returncode, tested.stdout) == (0, "43\n")
        assert (source / "alias").is_symlink()
        assert (source / ".git/index").read_bytes() == original_index
    finally:
        await environment.cleanup()
