from __future__ import annotations

import importlib
import json
import os
import subprocess
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


def _configure_retirement_log(workspace, monkeypatch):
    monkeypatch.setenv(
        retirement_registry.INTERNAL_RETIREMENT_WORKSPACE_ENV,
        str(workspace),
    )
    # Production resolves the already-open parent via Linux /proc.  These
    # host-side unit tests register only workspace-root artifacts on macOS.
    monkeypatch.setattr(
        retirement_registry,
        "_relative_path",
        lambda _parent_fd, name: name,
    )


def test_team_runner_shell_contains_no_embedded_python_and_stays_focused():
    source = (
        Path(__file__).resolve().parents[2] / "scripts" / "start_team_run.sh"
    ).read_text(encoding="utf-8")

    assert len(source.splitlines()) < 800
    assert "<<'PY'" not in source
    assert "swe_team_run_io.py" in source
    assert "swe_team_owner.py" in source


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
    _configure_retirement_log(repo, monkeypatch)
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
    with retirement_registry._lock:
        retirement_registry._records.clear()

    (repo / "source.py").write_text("new\n", encoding="utf-8")
    command = team_run_io.team_staged_diff_command(repo, retirement_log)
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
    _configure_retirement_log(repo, monkeypatch)
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
    with retirement_registry._lock:
        retirement_registry._records.clear()
    if mutation == "in_place":
        original_inode = tombstone.stat().st_ino
        tombstone.write_text("model changed the registered artifact\n", encoding="utf-8")
        assert tombstone.stat().st_ino == original_inode
    else:
        replacement = repo / "replacement"
        replacement.write_text("model replaced the registered artifact\n", encoding="utf-8")
        os.replace(replacement, tombstone)

    with pytest.raises(OSError, match="unregistered or modified"):
        team_run_io.team_staged_diff_command(repo, retirement_log)


def test_failed_persistent_registration_never_enters_memory_allowlist(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    tombstone = workspace / ".opencollab-retired-framework"
    tombstone.write_text("retained\n", encoding="utf-8")
    with retirement_registry._lock:
        retirement_registry._records.clear()
    _configure_retirement_log(workspace, monkeypatch)
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

    with retirement_registry._lock:
        assert retirement_registry._records == []
    with pytest.raises(OSError, match="unregistered or modified"):
        retirement_registry.registered_retirement_paths(workspace)


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


def test_retirement_scan_rejects_child_directory_swap_before_traversal(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    child = workspace / "child"
    child.mkdir(parents=True)
    real_open = retirement_registry.os.open
    swapped = False

    def swap_child_before_open(path, *args, **kwargs):
        nonlocal swapped
        if path == "child" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            child.rename(workspace / "detached-child")
            child.mkdir()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(retirement_registry.os, "open", swap_child_before_open)

    with pytest.raises(OSError, match="directory changed before traversal"):
        retirement_registry.registered_retirement_paths(workspace)


def test_retirement_scan_rejects_workspace_swap_after_traversal(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    real_stat = retirement_registry.os.stat
    root_stats = 0

    def swap_workspace_on_postcheck(path, *args, **kwargs):
        nonlocal root_stats
        if path == str(workspace) and not kwargs.get("follow_symlinks", True):
            root_stats += 1
            if root_stats == 2:
                workspace.rename(tmp_path / "detached-repo")
                workspace.mkdir()
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(retirement_registry.os, "stat", swap_workspace_on_postcheck)

    with pytest.raises(OSError, match="workspace changed after scan"):
        retirement_registry.registered_retirement_paths(workspace)


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
    status = team_run_io.main(
        [
            "bounded-diff-command",
            str(Path(__file__).resolve().parents[2] / "swebench"),
            "--workspace",
            str(workspace),
            "--retirement-log",
            str(log),
        ]
    )

    assert status == 125
    assert "internal retirement log is invalid" in capsys.readouterr().err
