from __future__ import annotations

import os
import shlex
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.candidate_workspace import EnvCandidateWorkspace
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.workflow_candidates import CandidateWorkspaceTrackingError
from tests.workflows.test_candidate_adoption_recovery import _partial_publication
from tests.workflows.test_workflow_candidate_workspace import _git


def _mount_commands(repo: Path, mount: Path, *, bind: bool, hard_links: bool) -> str:
    return (
        f"volume={shlex.quote(str(mount))}; root={shlex.quote(str(repo))}; "
        f"volume_device={1 if bind else 2}; volume_links={int(hard_links)};\n"
        + r'''
stat() {
    case "$1:$2" in
        -c:%d|-c:%d:%m)
            mountpoint=$root; device_id=1
            case "$4" in "$volume"|"$volume"/*) mountpoint=$volume; device_id=$volume_device;; esac
            if [ "$2" = %d ]; then printf '%s\n' "$device_id"; else printf '%s:%s\n' "$device_id" "$mountpoint"; fi
            ;;
        -c:%a)
            case "$4" in
                "$volume"/*) if [ "$volume_links" -eq 0 ]; then printf '755\n'; return; fi;;
            esac
            command stat "$@"
            ;;
        *) command stat "$@";;
    esac
}
ln() {
    if [ "$1" = -P ]; then
        case "$3" in
            "$volume"/*)
                case "$4" in
                    "$volume"/*) [ "$volume_links" -eq 1 ] || return 1;;
                    *) return 1;;
                esac
                ;;
        esac
    fi
    command ln "$@"
}
'''
    )


@pytest.mark.parametrize("scenario,bind,hard_links", [
    pytest.param("success", False, True, id="mixed-device-success"),
    pytest.param("success", True, True, id="bind-mount-success"),
    pytest.param("modified", False, True, id="restore-mounted-file"),
    pytest.param("modified", False, False, id="restore-mounted-file-without-hard-links"),
    pytest.param("added", True, True, id="park-new-file-on-bind-mount"),
])
async def test_candidate_adoption_with_nested_mount(tmp_path, monkeypatch, scenario, bind, hard_links):
    repo, candidate = _partial_publication(tmp_path)
    mount = repo / "mounted"
    mount.mkdir(exist_ok=True)
    target = mount / "record.txt"
    if scenario == "added":
        header = "new file mode 100644\n--- /dev/null\n+++ b/mounted/record.txt\n@@ -0,0 +1 @@\n"
        original = b""
    else:
        original = b"original mount\n"
        target.write_bytes(original)
        _git(repo, "add", "mounted/record.txt")
        _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "test: track mounted file")
        header = "--- a/mounted/record.txt\n+++ b/mounted/record.txt\n@@ -1 +1 @@\n-original mount\n"
    local_patch, blocked = candidate.diff.split("diff --git a/settings", 1)
    diff = local_patch + "diff --git a/mounted/record.txt b/mounted/record.txt\n" + header + "+candidate mount\n"
    if scenario != "success":
        diff += "diff --git a/settings" + blocked
    candidate = replace(candidate, diff=diff)
    (repo / "user-note.txt").write_text("staged by user\n")
    _git(repo, "add", "user-note.txt")
    index = repo / ".git/index"
    index_before = index.read_bytes()
    environment = LocalEnvironment(str(repo))
    execute = environment.exec_cmd
    original_fd = os.open(target, os.O_WRONLY | os.O_APPEND) if scenario == "modified" else None
    published_fd = None
    statuses = []

    async def observe(command, timeout=120, **kwargs):
        nonlocal published_fd
        if "filesystem()" in command or "device()" in command:
            command = _mount_commands(repo, mount, bind=bind, hard_links=hard_links) + command
        result = await execute(command, timeout, **kwargs)
        if " apply --binary " in command and ".candidate-adopt-" in command:
            statuses.append(result.returncode)
            if scenario != "success":
                assert target.read_bytes() == b"candidate mount\n"
                published_fd = os.open(target, os.O_WRONLY | os.O_APPEND)
        return result

    monkeypatch.setattr(environment, "exec_cmd", observe)

    async def flow(ctx, _):
        await ctx.adopt_candidate(candidate)

    try:
        result = await OpenCollab(repo, environment=environment, model="offline").workflow(flow, trace=False)
        assert index.read_bytes() == index_before
        assert (repo / "settings/notes.txt").read_bytes() == b"existing settings\n"
        if scenario == "success":
            assert result.ok, result.reason
            assert statuses == [0]
            assert target.read_bytes() == b"candidate mount\n"
            assert (repo / "record.txt").read_bytes() == b"candidate\n"
            assert not list((repo / ".git").glob("candidate-recovery-*"))
            assert not list(mount.glob(".candidate-recovery-*"))
            return

        assert type(result.error) is RuntimeError and "candidate adoption failed" in result.reason, result.reason
        assert statuses == [128]
        assert (repo / "record.txt").read_bytes() == b"original\n"
        if original:
            assert target.read_bytes() == original
        else:
            assert not target.exists()
        recovery, = (repo / ".git").glob("candidate-recovery-*")
        location, = (recovery / "volumes").iterdir()
        store = Path(location.read_text())
        assert store.parent == mount
        assert not (recovery / "replay").exists()
        assert not list(store.glob("restore-*"))
        assert published_fd is not None
        parked, = store.glob("observed-*/entry")
        assert parked.with_name("path").read_text() == str(target)
        retained = [(published_fd, parked, b"candidate mount\n")]
        if original_fd is not None:
            record, = store.glob("original-inodes/[0-9]*.path")
            assert record.read_text() == str(target)
            retained.append((original_fd, record.with_suffix(""), original))
        for fd, saved, initial in retained:
            os.write(fd, b"late write\n")
            os.fsync(fd)
            assert os.fstat(fd).st_ino == saved.stat().st_ino
            assert saved.read_bytes() == initial + b"late write\n"
    finally:
        if original_fd is not None:
            os.close(original_fd)
        if published_fd is not None:
            os.close(published_fd)
        await environment.cleanup()


@pytest.mark.parametrize(("source_edit", "linked", "readonly_common"), [
    (None, False, False),
    (None, True, False),
    pytest.param(None, True, True, id="linked-permissions-restored"),
    ("tracked", False, False),
    ("untracked", False, False),
])
async def test_followup_candidate_ignores_only_registered_mount_recovery(
    tmp_path, monkeypatch, source_edit, linked, readonly_common,
):
    source, candidate = _partial_publication(tmp_path)
    (source / "app.py").write_text("value = 1\n")
    mounted_file = "mounted/record.txt"
    (source / "mounted").mkdir()
    (source / mounted_file).write_text("original\n")
    (source / ".gitignore").write_text("settings/\n")
    if linked:
        (source / ".gitattributes").write_text("*.txt text eol=lf\n")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", "test: prepare mounted source")
    repo = source
    if linked:
        repo = tmp_path / "linked"
        _git(source, "worktree", "add", "--detach", str(repo))
        (repo / "settings").mkdir()
        (repo / "settings/notes.txt").write_text("existing settings\n")

    mount = repo / "mounted"
    target = repo / mounted_file
    candidate = replace(candidate, diff=candidate.diff.replace("record.txt", mounted_file))
    user_data = mount / ".candidate-recovery-userdata"
    user_data.mkdir()
    user_note = user_data / "note.txt"
    user_note.write_text("user data\n")
    index = Path(_git(repo, "rev-parse", "--absolute-git-dir").removesuffix("\n")) / "index"
    index_before = index.read_bytes()
    common = source / ".git"
    common_mode = stat.S_IMODE(common.stat().st_mode)
    environment = LocalEnvironment(str(repo))
    workspace = EnvCandidateWorkspace(environment)
    execute = environment.exec_cmd

    async def with_mount(command, timeout=120, **kwargs):
        if "filesystem()" in command:
            command = _mount_commands(repo, mount, bind=False, hard_links=True) + command
        return await execute(command, timeout, **kwargs)

    monkeypatch.setattr(environment, "exec_cmd", with_mount)
    store_path = None
    retained = None
    with target.open("ab", buffering=0) as old_writer:
        async def edit(child, _):
            worktree = Path(child.workspace_root)
            assert not (worktree / store_path).exists()
            assert (worktree / user_note.relative_to(repo)).read_text() == "user data\n"
            (worktree / "app.py").write_text("value = 2\n")
            old_writer.write(b"late original write\n")
            if source_edit == "tracked":
                (repo / "record.txt").write_text("tracked source edit\n")
            elif source_edit == "untracked":
                user_note.write_text("untracked source edit\n")

        async def flow(ctx, _):
            nonlocal store_path, retained
            try:
                if readonly_common:
                    common.chmod(0o555)
                    if os.access(common, os.W_OK):
                        pytest.skip("requires an account subject to directory permissions")
                with pytest.raises(RuntimeError, match="candidate adoption failed"):
                    await ctx.adopt_candidate(candidate)
            finally:
                if readonly_common:
                    common.chmod(common_mode)
            _git(repo, "restore", "--source=HEAD", "--worktree", mounted_file)
            primary = repo.parent if readonly_common else common
            recovery, = primary.glob("candidate-recovery-*")
            location, = (recovery / "volumes").iterdir()
            store = Path(location.read_text())
            store_path = store.relative_to(repo)
            retained, = (store / "original-inodes").glob("[0-9]")
            assert retained.stat().st_ino == os.fstat(old_writer.fileno()).st_ino

            before = await workspace.source_diff()
            assert str(store_path) not in before
            assert "+user data\n" in before
            if linked:
                lease = await workspace._acquire("team follow-up", strict_capture=True)
                try:
                    tree = Path(lease.candidate_workspace)
                    assert not (tree / store_path).exists()
                    assert (tree / user_note.relative_to(repo)).read_text() == "user data\n"
                    assert not await lease.source_contents_changed()
                finally:
                    await lease.cleanup()
            if source_edit is None:
                followup = await ctx.candidate_workflow(edit, {}, label="unrelated candidate")
                assert "+value = 2" in followup.diff
                await ctx.adopt_candidate(followup)
            else:
                try:
                    with pytest.raises(CandidateWorkspaceTrackingError, match="source worktree changed"):
                        await ctx.candidate_workflow(edit, {}, label="unrelated candidate")
                finally:
                    registered = _git(repo, "worktree", "list", "--porcelain").splitlines()
                    for entry in registered:
                        if entry.startswith("worktree ") and Path(entry[9:]) != repo:
                            _git(repo, "worktree", "remove", "--force", entry[9:])
            assert await workspace.source_diff() != before

        try:
            result = await OpenCollab(repo, environment=environment, model="offline").workflow(flow, trace=False)
            assert result.ok, result.reason
            assert target.read_text() == "original\n"
            assert retained.read_bytes() == b"original\nlate original write\n"
            assert (repo / "app.py").read_text() == ("value = 1\n" if source_edit else "value = 2\n")
            assert index.read_bytes() == index_before
        finally:
            await environment.cleanup()
