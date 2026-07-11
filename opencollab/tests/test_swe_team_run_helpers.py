from __future__ import annotations

import importlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from opencollab.adapters import retirement_registry

team_owner = importlib.import_module("scripts.swe_team_owner")
team_run_io = importlib.import_module("scripts.swe_team_run_io")


def _git_patch_context(repo: Path) -> tuple[str, Path]:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    git_dir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return base, Path(git_dir) / "objects"


def _configure_retirement_auth(tmp_path, workspace, monkeypatch):
    key_path = tmp_path / "retirement.key"
    team_run_io.create_retirement_key(key_path)
    key = key_path.read_bytes()
    monkeypatch.setattr(retirement_registry, "_persistent_signing_key", key)
    monkeypatch.setenv(
        retirement_registry.INTERNAL_RETIREMENT_WORKSPACE_ENV,
        str(workspace),
    )
    # Production resolves the already-open parent via Linux /proc.  These
    # host-side unit tests register only workspace-root artifacts on macOS.
    monkeypatch.setattr(
        retirement_registry,
        "_persistent_relative_path",
        lambda _parent_fd, name: name,
    )
    return key_path, key


def test_team_runner_shell_contains_no_embedded_python_and_stays_focused():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "start_team_run.sh"
    ).read_text(encoding="utf-8")

    assert len(source.splitlines()) < 800
    assert "<<'PY'" not in source
    assert "swe_team_run_io.py" in source
    assert "swe_team_owner.py" in source


def _snapshot_tar(*members: tuple[tarfile.TarInfo, bytes]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for member, content in members:
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if content else None)
    return payload.getvalue()


def _run_snapshot_helper(destination: Path, payload: bytes):
    helper = Path(__file__).resolve().parents[2] / "scripts" / "safe_workspace_snapshot.py"
    return subprocess.run(
        [sys.executable, str(helper), str(destination)],
        input=payload,
        capture_output=True,
        check=False,
    )


def test_workspace_snapshot_rejects_parent_traversal(tmp_path):
    destination = tmp_path / "snapshot"
    destination.mkdir()
    member = tarfile.TarInfo("../escaped")

    result = _run_snapshot_helper(destination, _snapshot_tar((member, b"owned")))

    assert result.returncode != 0
    assert not (tmp_path / "escaped").exists()


def test_workspace_snapshot_never_follows_archive_symlink_parent(tmp_path):
    destination = tmp_path / "snapshot"
    destination.mkdir()
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../outside"
    nested = tarfile.TarInfo("link/payload")

    result = _run_snapshot_helper(
        destination,
        _snapshot_tar((link, b""), (nested, b"owned")),
    )

    assert result.returncode != 0
    assert not (tmp_path / "outside" / "payload").exists()


def test_task_in_place_write_is_bound_to_original_target(tmp_path):
    task = tmp_path / "task.md"
    task.write_bytes(b"")
    identity = (task.stat().st_dev, task.stat().st_ino)

    team_run_io._write_task(task, b"prompt")

    current = task.stat()
    assert (current.st_dev, current.st_ino) == identity
    assert task.read_bytes() == b"prompt"
    assert not list(tmp_path.glob(".opencollab-retired-*"))


def test_pending_prediction_adopts_extracted_patch_without_tombstone(tmp_path):
    source = tmp_path / "extracted-patch"
    source.write_bytes(b"diff --git a/a.py b/a.py\n")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    record = tmp_path / "pending.record.json"
    pending = tmp_path / "pending.patch"

    team_run_io.create_pending_prediction(
        record,
        pending,
        source,
        tmp_path / "predictions.jsonl",
        "demo__task-1",
        "opencollab-team",
        0,
    )

    assert not source.exists()
    current = pending.stat()
    assert (current.st_dev, current.st_ino) == source_identity
    assert pending.read_bytes() == b"diff --git a/a.py b/a.py\n"
    assert record.is_file()
    assert not list(tmp_path.glob(".opencollab-retired-*"))


def test_owner_remove_is_bound_to_validated_marker_inode(tmp_path, monkeypatch):
    marker = tmp_path / "team_container.owner"
    session_key = "a" * 64
    nonce = "b" * 32
    team_owner.create_marker(marker, "oc-team-demo", session_key, nonce)
    identity = (marker.stat().st_dev, marker.stat().st_ino)
    observed: dict[str, object] = {}

    def capture(path, **kwargs):
        observed.update(path=path, kwargs=kwargs)
        return True

    monkeypatch.setattr(team_owner, "unlink_regular_file_durable", capture)

    assert team_owner.remove_marker(
        marker,
        session_key,
        "oc-team-demo",
        "",
        nonce,
        require_match=True,
    )
    assert observed["path"] == marker
    assert observed["kwargs"]["expected_target_identity"] == identity


def test_owner_bind_is_bound_to_validated_marker_inode(tmp_path, monkeypatch):
    marker = tmp_path / "team_container.owner"
    session_key = "a" * 64
    nonce = "b" * 32
    container_id = "c" * 64
    team_owner.create_marker(marker, "oc-team-demo", session_key, nonce)
    identity = (marker.stat().st_dev, marker.stat().st_ino)
    observed: dict[str, object] = {}

    def capture(path, payload, **kwargs):
        observed.update(path=path, payload=payload, kwargs=kwargs)

    monkeypatch.setattr(team_owner, "write_regular_bytes_atomic", capture)

    team_owner.bind_container_id(
        marker,
        session_key,
        "oc-team-demo",
        nonce,
        container_id,
    )

    payload = json.loads(observed["payload"])
    assert payload["container_id"] == container_id
    assert observed["kwargs"]["expected_target_identity"] == identity


def test_team_staged_diff_uses_only_unchanged_registered_tombstones(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    retirement_log = tmp_path / "retirements.jsonl"
    team_run_io.create_retirement_log(retirement_log)
    retirement_key, _key = _configure_retirement_auth(tmp_path, repo, monkeypatch)
    monkeypatch.setenv(
        retirement_registry.INTERNAL_RETIREMENT_LOG_ENV,
        str(retirement_log),
    )
    tombstone = repo / ".opencollab-retired-framework"
    tombstone.write_text("old\n", encoding="utf-8")
    parent_fd = os.open(repo, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        retirement_registry.register_verified_retirement(parent_fd, tombstone.name)
    finally:
        os.close(parent_fd)
    with retirement_registry._records_lock:
        retirement_registry._records.clear()

    (repo / "source.py").write_text("new\n", encoding="utf-8")
    base, objects = _git_patch_context(repo)
    command = team_run_io.team_staged_diff_command(
        repo,
        retirement_log,
        retirement_key,
        base_revision=base,
        object_directory=objects,
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "source.py" in result.stdout
    assert tombstone.name not in result.stdout

    (repo / ".opencollab-retired-model").write_text("hidden\n", encoding="utf-8")
    rejected = subprocess.run(
        ["bash", "-lc", command],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 125
    assert "unregistered or modified .opencollab-retired-*" in rejected.stderr


@pytest.mark.parametrize("mutation", ["in_place", "replacement"])
def test_team_staged_diff_rejects_modified_registered_tombstone(
    tmp_path,
    monkeypatch,
    mutation,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "source.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=OpenCollab",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    retirement_log = tmp_path / "retirements.jsonl"
    team_run_io.create_retirement_log(retirement_log)
    retirement_key, key = _configure_retirement_auth(tmp_path, repo, monkeypatch)
    monkeypatch.setenv(
        retirement_registry.INTERNAL_RETIREMENT_LOG_ENV,
        str(retirement_log),
    )
    tombstone = repo / ".opencollab-retired-framework"
    tombstone.write_text("old\n", encoding="utf-8")
    parent_fd = os.open(repo, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        retirement_registry.register_verified_retirement(parent_fd, tombstone.name)
    finally:
        os.close(parent_fd)
    with retirement_registry._records_lock:
        retirement_registry._records.clear()
    base, objects = _git_patch_context(repo)
    if mutation == "in_place":
        original_inode = tombstone.stat().st_ino
        tombstone.write_text("model changed the registered artifact\n", encoding="utf-8")
        assert tombstone.stat().st_ino == original_inode
    else:
        replacement = repo / "replacement"
        replacement.write_text("model replaced the registered artifact\n", encoding="utf-8")
        os.replace(replacement, tombstone)

    with pytest.raises(OSError, match="unregistered or modified"):
        team_run_io.team_staged_diff_command(
            repo,
            retirement_log,
            retirement_key,
            base_revision=base,
            object_directory=objects,
        )


def test_failed_persistent_registration_never_enters_memory_allowlist(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    tombstone = workspace / ".opencollab-retired-framework"
    tombstone.write_text("retained\n", encoding="utf-8")
    with retirement_registry._records_lock:
        retirement_registry._records.clear()
    _key_path, _key = _configure_retirement_auth(tmp_path, workspace, monkeypatch)
    monkeypatch.setenv(
        retirement_registry.INTERNAL_RETIREMENT_LOG_ENV,
        str(tmp_path / "missing" / "retirements.jsonl"),
    )
    parent_fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(OSError):
            retirement_registry.register_verified_retirement(
                parent_fd,
                tombstone.name,
            )
    finally:
        os.close(parent_fd)

    with retirement_registry._records_lock:
        assert retirement_registry._records == []
    with pytest.raises(OSError, match="unregistered or modified"):
        retirement_registry.registered_retirement_paths(workspace)


def test_team_sidecar_rejects_forged_v1_record_with_real_stat(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@y", "commit", "-qm", "base"],
        cwd=workspace,
        check=True,
    )
    log = tmp_path / "retirements.jsonl"
    team_run_io.create_retirement_log(log)
    key_path, _key = _configure_retirement_auth(tmp_path, workspace, monkeypatch)
    forged = workspace / ".opencollab-retired-forged"
    forged.write_text("hidden\n", encoding="utf-8")
    info = forged.stat()
    payload = {
        "schema": "opencollab.retirement.v1",
        "parent_dev": workspace.stat().st_dev,
        "parent_ino": workspace.stat().st_ino,
        "name": forged.name,
        "file_dev": info.st_dev,
        "file_ino": info.st_ino,
        "mode": stat.S_IFMT(info.st_mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "nlink": info.st_nlink,
    }
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")

    base, objects = _git_patch_context(workspace)
    with pytest.raises(ValueError, match="schema|authentication|fields"):
        team_run_io.team_staged_diff_command(
            workspace,
            log,
            key_path,
            base_revision=base,
            object_directory=objects,
        )


def test_team_sidecar_rejects_tampered_authenticated_record(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    tracked = workspace / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", tracked.name], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@y", "commit", "-qm", "base"],
        cwd=workspace,
        check=True,
    )
    log = tmp_path / "retirements.jsonl"
    team_run_io.create_retirement_log(log)
    key_path, _key = _configure_retirement_auth(tmp_path, workspace, monkeypatch)
    monkeypatch.setenv(retirement_registry.INTERNAL_RETIREMENT_LOG_ENV, str(log))
    tombstone = workspace / ".opencollab-retired-framework"
    tombstone.write_text("trusted\n", encoding="utf-8")
    parent_fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        retirement_registry.register_verified_retirement(parent_fd, tombstone.name)
    finally:
        os.close(parent_fd)
    payload = json.loads(log.read_text(encoding="utf-8"))
    payload["size"] += 1
    log.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    base, objects = _git_patch_context(workspace)

    with pytest.raises(ValueError, match="authentication failed"):
        team_run_io.team_staged_diff_command(
            workspace,
            log,
            key_path,
            base_revision=base,
            object_directory=objects,
        )


def test_retirement_key_is_consumed_before_task_subprocess(tmp_path):
    key_path = tmp_path / "task.key"
    key_path.write_bytes(b"k" * retirement_registry.RETIREMENT_SIGNING_KEY_BYTES)
    env = os.environ.copy()
    env[retirement_registry.INTERNAL_RETIREMENT_KEY_FILE_ENV] = str(key_path)
    code = """
import json, os, subprocess, sys
path = os.environ['OPENCOLLAB_INTERNAL_RETIREMENT_KEY_FILE']
import opencollab.adapters.retirement_registry as registry
child = subprocess.run(
    [sys.executable, '-c',
     'import json,os; import opencollab.adapters.retirement_registry as r; '
     'print(json.dumps({"has_env":r.INTERNAL_RETIREMENT_KEY_FILE_ENV in os.environ,'
     '"has_key":r._persistent_signing_key is not None}))'],
    text=True, capture_output=True, check=True,
)
print(json.dumps({"path":path,"has_env":registry.INTERNAL_RETIREMENT_KEY_FILE_ENV in os.environ,
"exists":os.path.exists(path),"child":json.loads(child.stdout)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    evidence = json.loads(result.stdout)

    assert evidence["path"] == str(key_path)
    assert evidence["has_env"] is False
    assert evidence["exists"] is False
    assert evidence["child"] == {
        "has_env": False,
        "has_key": False,
    }


def test_retirement_scan_rejects_child_directory_swap_after_traversal(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    child = workspace / "child"
    child.mkdir(parents=True)
    tombstone = child / ".opencollab-retired-framework"
    tombstone.write_text("retained\n", encoding="utf-8")
    child_fd = os.open(child, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        retirement_registry.register_verified_retirement(child_fd, tombstone.name)
    finally:
        os.close(child_fd)
    real_stat = retirement_registry.os.stat
    child_stats = 0

    def swap_child_on_postcheck(path, *args, **kwargs):
        nonlocal child_stats
        if path == "child" and kwargs.get("dir_fd") is not None:
            child_stats += 1
            if child_stats == 2:
                child.rename(workspace / "detached-child")
                child.mkdir()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(retirement_registry.os, "stat", swap_child_on_postcheck)

    with pytest.raises(OSError, match="directory changed after traversal"):
        retirement_registry.registered_retirement_paths(workspace)


@pytest.mark.parametrize("tamper", ["unsigned", "wrong_mac", "duplicate", "reordered"])
def test_persistent_retirement_log_rejects_unauthenticated_or_replayed_rows(
    tmp_path,
    monkeypatch,
    tamper,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    log = tmp_path / "retirements.jsonl"
    team_run_io.create_retirement_log(log)
    _key_path, key = _configure_retirement_auth(tmp_path, workspace, monkeypatch)
    monkeypatch.setenv(retirement_registry.INTERNAL_RETIREMENT_LOG_ENV, str(log))
    for suffix in ("first", "second"):
        tombstone = workspace / f".opencollab-retired-{suffix}"
        tombstone.write_text(suffix, encoding="utf-8")
        parent_fd = os.open(workspace, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            retirement_registry.register_verified_retirement(parent_fd, tombstone.name)
        finally:
            os.close(parent_fd)
    lines = log.read_text(encoding="utf-8").splitlines()
    if tamper == "unsigned":
        payload = json.loads(lines[0])
        payload.pop("mac")
        lines[0] = json.dumps(payload, sort_keys=True)
    elif tamper == "wrong_mac":
        payload = json.loads(lines[0])
        payload["mac"] = "0" * 64
        lines[0] = json.dumps(payload, sort_keys=True)
    elif tamper == "duplicate":
        lines.append(lines[0])
    else:
        lines.reverse()
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with retirement_registry._records_lock:
        retirement_registry._records.clear()

    with pytest.raises(ValueError, match="fields|authentication|sequence|chain"):
        retirement_registry.registered_retirement_snapshot(
            workspace,
            persistent_log=log,
            persistent_key=key,
        )


def test_persistent_retirement_log_bounds_fail_closed_with_technical_status(
    tmp_path,
    capsys,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    (workspace / "tracked").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "tracked"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "-c", "user.name=x", "-c", "user.email=x@y", "commit", "-qm", "base"],
        cwd=workspace,
        check=True,
    )
    log = tmp_path / "retirements.jsonl"
    log.write_bytes(b"x" * (retirement_registry.MAX_RETIREMENT_LOG_BYTES + 1))
    key_path = tmp_path / "verification.key"
    key_path.write_bytes(b"k" * retirement_registry.RETIREMENT_SIGNING_KEY_BYTES)
    base, objects = _git_patch_context(workspace)

    status = team_run_io.main(
        [
            "bounded-diff-command",
            str(Path(__file__).resolve().parents[2] / "swebench"),
            "--workspace",
            str(workspace),
            "--retirement-log",
            str(log),
            "--retirement-key",
            str(key_path),
            "--base-revision",
            base,
            "--object-directory",
            str(objects),
        ]
    )

    assert status == 125
    assert "bounded regular file" in capsys.readouterr().err
