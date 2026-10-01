"""Observed writes preserve atomic publication and avoid extra remote round trips."""
from __future__ import annotations

import os
import subprocess

import pytest

from opencollab.adapters import safe_anchored_files
from opencollab.adapters._env_base import ExecResult
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment


@pytest.mark.parametrize(("before", "content", "changed"), [
    (None, "", True), (b"", "", False), (b"same", "same", False), (b"old", "new", True),
    (b"\xff\xfe", "text", True), (b"x" * (5 * 1024 * 1024), "small", True),
])
async def test_local_observed_write_preserves_content_and_existing_mode(tmp_path, before, content, changed):
    target = tmp_path / "f.txt"
    if before is not None:
        target.write_bytes(before)
        target.chmod(0o751)
    inode = target.stat().st_ino if target.exists() else None
    env = LocalEnvironment(str(tmp_path))
    try:
        assert await env.write_file_with_change("f.txt", content) is changed
        assert target.read_text() == content
        if before is not None:
            assert target.stat().st_ino != inode
            assert target.stat().st_mode & 0o777 == 0o751
        assert await env.write_file("f.txt", content) is None
    finally:
        await env.cleanup()


async def test_local_comparison_permission_error_keeps_allowed_atomic_write(monkeypatch, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("old")
    original_open = os.open

    def deny_comparison(path, flags, *args, **kwargs):
        if path == "f.txt" and flags & os.O_ACCMODE == os.O_RDONLY:
            raise PermissionError("comparison read denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_anchored_files.os, "open", deny_comparison)
    env = LocalEnvironment(str(tmp_path))
    try:
        assert await env.write_file_with_change("f.txt", "new") is None
        assert target.read_text() == "new"
    finally:
        await env.cleanup()


async def test_local_identity_drift_discards_comparison_but_publishes(monkeypatch, tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("same")
    original_observe = safe_anchored_files._observe_content_change

    def replace_after_comparison(parent_fd, name, temporary, current, payload=None):
        result = original_observe(parent_fd, name, temporary, current, payload)
        other = tmp_path / "other"
        other.write_text("external")
        other.replace(target)
        return result

    monkeypatch.setattr(safe_anchored_files, "_observe_content_change", replace_after_comparison)
    env = LocalEnvironment(str(tmp_path))
    try:
        assert await env.write_file_with_change("f.txt", "same") is None
        assert target.read_text() == "same"
    finally:
        await env.cleanup()


async def test_local_bytes_writer_compares_existing_payload_without_reading_temporary(monkeypatch, tmp_path):
    (tmp_path / "f.txt").write_text("same")
    original_observe = safe_anchored_files._observe_content_change

    def observe_without_temporary_read(parent_fd, name, temporary, current, payload=None):
        class NoTemporaryRead:
            fileno = temporary.fileno

            def seek(self, *args):
                raise AssertionError("materialized payload must avoid a temporary read")

            read = seek

        return original_observe(parent_fd, name, NoTemporaryRead(), current, payload)

    monkeypatch.setattr(safe_anchored_files, "_observe_content_change", observe_without_temporary_read)
    env = LocalEnvironment(str(tmp_path))
    try:
        assert await env.write_file_with_change("f.txt", "same") is False
    finally:
        await env.cleanup()


def test_streaming_atomic_writer_keeps_temporary_comparison_fallback(tmp_path):
    target = tmp_path / "f.txt"
    target.write_bytes(b"same")
    root_fd = safe_anchored_files.open_directory_anchor(tmp_path)
    try:
        changed = safe_anchored_files.write_regular_file_atomic_at(
            root_fd, tmp_path, "f.txt", lambda handle: handle.write(b"same"), max_bytes=4, observe_change=True,
        )
        assert changed is False
        assert target.read_bytes() == b"same"
    finally:
        os.close(root_fd)


@pytest.mark.parametrize("same_size", [False, True])
async def test_local_observation_reads_only_the_target_size(monkeypatch, tmp_path, same_size):
    payload = "x" * (4 * 1024 * 1024)
    target = tmp_path / "f.txt"
    target.write_text(payload if same_size else "old")
    original_open, original_read = os.open, os.read
    observed_fds = set()
    observed_bytes = 0

    def record_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == "f.txt" and flags & os.O_ACCMODE == os.O_RDONLY:
            observed_fds.add(fd)
        return fd

    def record_read(fd, length):
        nonlocal observed_bytes
        data = original_read(fd, length)
        if fd in observed_fds:
            observed_bytes += len(data)
        return data

    monkeypatch.setattr(safe_anchored_files.os, "open", record_open)
    monkeypatch.setattr(safe_anchored_files.os, "read", record_read)
    env = LocalEnvironment(str(tmp_path))
    try:
        assert await env.write_file_with_change("f.txt", payload) is (not same_size)
        assert observed_bytes == (len(payload) if same_size else 0)
    finally:
        await env.cleanup()


async def test_observed_local_write_keeps_path_and_size_restrictions(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    try:
        with pytest.raises(PermissionError):
            await env.write_file_with_change("../outside", "x")
        with pytest.raises(OSError, match="write limit"):
            await env.write_file_with_change("too-large", "x" * (4 * 1024 * 1024 + 1))
        (tmp_path / "link").symlink_to(tmp_path / "missing")
        with pytest.raises(OSError):
            await env.write_file_with_change("link", "x")
        assert not (tmp_path / "too-large").exists()
        assert not (tmp_path / "missing").exists()
    finally:
        await env.cleanup()


@pytest.mark.parametrize(("before", "content", "changed"), [
    (None, "", True), (b"", "", False), (b"same", "same", False), (b"old", "new", True),
    (b"longer", "short", True), (b"\xff\xfe", "text", True),
])
async def test_docker_comparison_uses_one_real_shell_write(monkeypatch, tmp_path, before, content, changed):
    target = tmp_path / "f.txt"
    if before is not None:
        target.write_bytes(before)
    executions = []

    async def run_locally(command, *, timeout, input_bytes=None):
        executions.append(command)
        result = subprocess.run(["bash", "-c", command], input=input_bytes, capture_output=True, check=False)
        return ExecResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    env = DockerEnvironment(workspace=str(tmp_path), container_id="a" * 64)
    env._attached_bound = True
    monkeypatch.setattr(env, "_exec", run_locally)
    assert await env.write_file_with_change(str(target), content) is changed
    assert target.read_text() == content
    assert len(executions) == 1
    assert not list(tmp_path.glob(".*.opencollab-write-*.tmp"))


@pytest.mark.parametrize("comparison", [
    "exit 2",
    'printf external > "$4.swap"; mv -f "$4.swap" "$4"; exit 0',
])
async def test_docker_comparison_error_or_drift_still_publishes(monkeypatch, tmp_path, comparison):
    target = tmp_path / "f.txt"
    target.write_text("old")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    cmp = fake_bin / "cmp"
    cmp.write_text(f"#!/bin/sh\n{comparison}\n")
    cmp.chmod(0o755)
    calls = 0

    async def run_locally(command, *, timeout, input_bytes=None):
        nonlocal calls
        calls += 1
        result = subprocess.run(
            ["bash", "-c", command], input=input_bytes, capture_output=True, check=False,
            env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        )
        return ExecResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    env = DockerEnvironment(workspace=str(tmp_path), container_id="a" * 64)
    env._attached_bound = True
    monkeypatch.setattr(env, "_exec", run_locally)
    assert await env.write_file_with_change(str(target), "new") is None
    assert target.read_text() == "new"
    assert calls == 1
