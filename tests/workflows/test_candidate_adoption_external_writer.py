from __future__ import annotations

import asyncio
import os
import shlex
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.bash import BashTool
from opencollab.application.tool_execution import ToolRuntime
from tests.workflows.test_candidate_adoption_recovery import _partial_publication
from tests.workflows.test_workflow_candidate_workspace import _git

_WRITER = """
import os
import sys

with open(sys.argv[1], "ab") as stream:
    opened = os.fstat(stream.fileno())
    print(opened.st_dev, opened.st_ino, flush=True)
    data = sys.stdin.buffer.read()
    if not data:
        raise RuntimeError("writer exited before release")
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())
print("written")
"""


def _identity(path: Path) -> tuple[int, int]:
    status = path.stat()
    return status.st_dev, status.st_ino


@pytest.fixture
def worktree_parent(tmp_path, request):
    if not request.param:
        yield tmp_path
        return
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir() or not os.access(shared_memory, os.W_OK):
        pytest.skip("requires writable shared memory on a separate filesystem")
    if shared_memory.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("requires Git metadata and worktree on different filesystems")
    with tempfile.TemporaryDirectory(prefix="oc-worktree-", dir=shared_memory) as path:
        yield Path(path)


@pytest.mark.parametrize("opened_before_apply,linked,worktree_parent", [
    pytest.param(True, False, False, id="original-inode"),
    pytest.param(False, True, False, id="candidate-inode-after-worktree-removal"),
    pytest.param(False, True, True, id="separate-worktree-filesystem"),
], indirect=["worktree_parent"])
async def test_candidate_rollback_retains_inode_for_external_writer(
    tmp_path, monkeypatch, caplog, opened_before_apply, linked, worktree_parent,
):
    source, candidate = _partial_publication(tmp_path)
    repo = source
    if linked:
        repo = worktree_parent / "linked"
        _git(source, "worktree", "add", "--detach", str(repo))
        (repo / "settings").mkdir()
        (repo / "settings/notes.txt").write_text("existing settings\n")
    (repo / "user-note.txt").write_text("staged user note\n")
    _git(repo, "add", "--", "user-note.txt")
    target = repo / "record.txt"
    original = target.read_bytes()
    original_inode = _identity(target)
    published = b"candidate\n"
    appended = b"external write after rollback\n"
    index = Path(_git(repo, "rev-parse", "--absolute-git-dir").removesuffix("\n")) / "index"
    index_before = index.read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    writer = None
    writer_inode = None

    async def start_writer(expected: bytes) -> None:
        nonlocal writer, writer_inode
        assert writer is None
        assert target.read_bytes() == expected
        writer = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _WRITER, str(target),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert writer.stdout is not None
        ready = await asyncio.wait_for(writer.stdout.readline(), 10)
        writer_inode = _identity(target)
        assert ready.split() == [str(value).encode() for value in writer_inode]

    async def observe(command, timeout=120.0, **kwargs):
        if " apply --binary " not in command or ".candidate-adopt-" not in command:
            return await execute(command, timeout, **kwargs)
        if opened_before_apply:
            await start_writer(original)
        outcome = await execute(command, timeout, **kwargs)
        assert outcome.returncode == 128, outcome.stderr
        assert target.read_bytes() == published
        assert _identity(target) != original_inode
        if not opened_before_apply:
            await start_writer(published)
        return outcome

    monkeypatch.setattr(environment, "exec_cmd", observe)

    async def flow(ctx, _):
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(
            flow, trace=False, timeout=30,
        )
        assert type(result.error) is RuntimeError, result.reason
        assert "candidate adoption failed" in result.reason
        assert "settings" in result.reason
        assert target.read_bytes() == original
        assert (repo / "settings/notes.txt").read_bytes() == b"existing settings\n"
        assert index.read_bytes() == index_before
        assert writer is not None, result.reason
        assert writer_inode is not None

        if linked:
            _git(source, "worktree", "remove", "--force", str(repo))
            assert not repo.exists()
            assert not index.exists()
        assert writer.returncode is None
        stdout, stderr = await asyncio.wait_for(writer.communicate(appended), 10)
        assert writer.returncode == 0, stderr
        assert stdout == b"written\n"
        assert (source / "record.txt").read_bytes() == original

        storage = source / ".git" if worktree_parent == tmp_path else worktree_parent
        backups = list(storage.glob("candidate-recovery-*"))
        assert backups, "candidate rollback left no recovery storage for the open descriptor"
        backup, = backups
        assert all(path.name == "original-inodes" or path.name.startswith("observed-") for path in backup.iterdir())
        if opened_before_apply:
            path_record, = (path for path in backup.glob("original-inodes/*.path") if path.read_text() == str(target))
            retained = path_record.with_suffix("")
            initial = original
        else:
            path_record, = (path for path in backup.glob("observed-*/path") if path.read_text() == str(target))
            retained = path_record.with_name("entry")
            initial = published
        assert _identity(retained) == writer_inode
        assert retained.read_bytes() == initial + appended
        assert any(str(backup) in message for message in caplog.messages)
        assert any(str(backup) in note for note in getattr(result.error, "__notes__", ()))
    finally:
        if writer is not None and writer.returncode is None:
            writer.kill()
            await writer.communicate()
        await environment.cleanup()


async def test_detached_original_only_preserves_publication_error_during_shell_execution(tmp_path, monkeypatch):
    repo, candidate = _partial_publication(tmp_path)
    _, replacement = candidate.diff.split("diff --git a/settings", 1)
    candidate = replace(candidate, diff=(
        "diff --git a/record.txt b/record.txt\ndeleted file mode 100644\n"
        "--- a/record.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-original\n"
        "diff --git a/settings" + replacement
    ))
    target = repo / "record.txt"
    original_inode = _identity(target)
    index = repo / ".git/index"
    index_before = index.read_bytes()
    ready = tmp_path / "shell-ready"
    release = tmp_path / "shell-release"
    command = (
        f"touch {shlex.quote(str(ready))} && "
        f"while [ ! -f {shlex.quote(str(release))} ]; do sleep 0.01; done"
    )
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd

    async def observe(command, timeout=120.0, **kwargs):
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command and ".candidate-adopt-" in command:
            assert result.returncode == 128, result.stderr
            assert not target.exists()
        return result

    monkeypatch.setattr(environment, "exec_cmd", observe)

    async def wait_for_shell():
        while not ready.exists():
            await asyncio.sleep(0.01)

    async def flow(ctx, _):
        shell = asyncio.create_task(BashTool().execute_with_runtime(
            {"command": command, "timeout": 20}, ToolRuntime(environment, None, None),
        ))
        try:
            await asyncio.wait_for(wait_for_shell(), 10)
            assert not shell.done()
            await ctx.adopt_candidate(candidate)
        finally:
            release.touch()
            assert await shell == "Exit code: 0"

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(
            flow, trace=False, timeout=30,
        )
        assert type(result.error) is RuntimeError, result.reason
        assert "candidate adoption failed" in result.reason
        assert target.read_bytes() == b"original\n"
        assert index.read_bytes() == index_before
        backup, = (repo / ".git").glob("candidate-recovery-*")
        record, = (backup / "original-inodes").glob("*.path")
        assert record.read_text() == str(target)
        assert _identity(record.with_suffix("")) == original_inode
        assert not list(backup.glob("observed-*"))
    finally:
        await environment.cleanup()
