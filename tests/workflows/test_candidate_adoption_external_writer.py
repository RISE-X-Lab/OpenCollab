from __future__ import annotations

import asyncio
import os
import shlex
import stat
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


def _delete_original(candidate):
    _, replacement = candidate.diff.split("diff --git a/settings", 1)
    return replace(candidate, diff=(
        "diff --git a/record.txt b/record.txt\ndeleted file mode 100644\n"
        "--- a/record.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-original\n"
        "diff --git a/settings" + replacement
    ))


async def test_candidate_adopts_with_readonly_common_git_directory(tmp_path):
    source, candidate = _partial_publication(tmp_path)
    repo = tmp_path / "linked"
    _git(source, "worktree", "add", "--detach", str(repo))
    (repo / "user-note.txt").write_text("staged user note\n")
    _git(repo, "add", "--", "user-note.txt")
    index = Path(_git(repo, "rev-parse", "--absolute-git-dir").removesuffix("\n")) / "index"
    index_before = index.read_bytes()
    common = source / ".git"
    common_mode = stat.S_IMODE(common.stat().st_mode)
    target = repo / "record.txt"
    environment = LocalEnvironment(str(repo))

    async def flow(ctx, _):
        await ctx.adopt_candidate(candidate)

    try:
        common.chmod(0o555)
        if os.access(common, os.W_OK):
            pytest.skip("requires an account subject to directory permissions")
        assert os.access(index.parent, os.W_OK) and os.access(repo, os.W_OK)
        with target.open("rb") as original:
            result = await OpenCollab(repo, environment=environment, model="offline").workflow(
                flow, trace=False, timeout=30,
            )
            assert result.ok, result.reason
            assert target.read_bytes() == b"candidate\n"
            assert (repo / "settings").read_bytes() == b"replacement settings\n"
            assert index.read_bytes() == index_before
            assert original.read() == b"original\n"
            assert os.fstat(original.fileno()).st_nlink == 0
        assert stat.S_IMODE(common.stat().st_mode) == 0o555
        for storage in (common, index.parent, tmp_path, repo):
            assert not list(storage.glob("*candidate-recovery-*"))
        assert not list(index.parent.glob("opencollab-candidate-*"))
    finally:
        common.chmod(common_mode)
        await environment.cleanup()


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


@pytest.mark.parametrize("opened_before_apply,linked,worktree_parent,hard_links,readonly_common", [
    pytest.param(True, False, False, True, False, id="original-inode"),
    pytest.param(False, True, False, True, False, id="candidate-inode-after-worktree-removal"),
    pytest.param(False, True, True, True, False, id="separate-worktree-filesystem"),
    pytest.param(True, True, False, False, False, id="original-inode-without-hard-links"),
    pytest.param(True, True, False, True, True, id="readonly-common-original-inode"),
    pytest.param(False, True, False, True, True, id="readonly-common-candidate-inode"),
], indirect=["worktree_parent"])
async def test_candidate_rollback_retains_inode_for_external_writer(
    tmp_path, monkeypatch, caplog, opened_before_apply, linked, worktree_parent, hard_links, readonly_common,
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
    common = source / ".git"
    common_mode = stat.S_IMODE(common.stat().st_mode)
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
        if not hard_links and "hold_originals()" in command:
            command = "ln() { return 1; }; " + command
        if " apply --binary " not in command or ".candidate-adopt-" not in command:
            return await execute(command, timeout, **kwargs)
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
        if readonly_common:
            common.chmod(0o555)
            if os.access(common, os.W_OK):
                pytest.skip("requires an account subject to directory permissions")
            assert os.access(index.parent, os.W_OK) and os.access(repo, os.W_OK)
        if opened_before_apply:
            await start_writer(original)
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

        storage = common if worktree_parent == tmp_path and not readonly_common else worktree_parent
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
        if readonly_common:
            common.chmod(common_mode)
        if writer is not None and writer.returncode is None:
            writer.kill()
            await writer.communicate()
        await environment.cleanup()


@pytest.mark.parametrize("external_delete", [False, True], ids=["interrupted-move", "external-delete"])
async def test_failed_preparation_recovers_only_owned_moves(tmp_path, monkeypatch, external_delete):
    repo, candidate = _partial_publication(tmp_path)
    if external_delete:
        candidate = _delete_original(candidate)
    target = repo / "record.txt"
    identity = _identity(target)
    index = repo / ".git/index"
    index_before = index.read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    reached_activation = False
    published = False

    async def interrupt(command, timeout=120.0, **kwargs):
        nonlocal reached_activation, published
        if "hold_originals()" in command:
            command = "ln() { return 1; }; " + command
        if 'restore_saved "$replacement" "$live"' in command:
            reached_activation = True
            if external_delete:
                writer = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", "import os, sys; os.unlink(sys.argv[1])", str(target),
                )
                assert await asyncio.wait_for(writer.wait(), 10) == 0
            else:
                command = r'''
mv() {
    case "$3" in
        */replacements/*) printf 'replacement move interrupted\n' >&2; return 1;;
        *) command mv "$@";;
    esac
}
''' + command
        if " apply --binary " in command and ".candidate-adopt-" in command:
            published = True
        return await execute(command, timeout, **kwargs)

    monkeypatch.setattr(environment, "exec_cmd", interrupt)

    async def flow(ctx, _):
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(flow, trace=False)
        assert reached_activation and not published
        assert type(result.error) is RuntimeError
        if external_delete:
            assert "concurrent change" in result.reason
            assert not target.exists()
        else:
            assert "replacement move interrupted" in result.reason
            assert _identity(target) == identity
            assert target.read_bytes() == b"original\n"
        assert index.read_bytes() == index_before
        assert not list((repo / ".git").glob("candidate-recovery-*"))
    finally:
        await environment.cleanup()


async def test_detached_original_only_preserves_publication_error_during_shell_execution(tmp_path, monkeypatch):
    repo, candidate = _partial_publication(tmp_path)
    candidate = _delete_original(candidate)
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
