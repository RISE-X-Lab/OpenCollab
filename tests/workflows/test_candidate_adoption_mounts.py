from __future__ import annotations

import os
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
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
