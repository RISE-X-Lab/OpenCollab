"""Observed writes resolve paths read-only and publish in one payload call."""
from __future__ import annotations

import errno
import os
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from opencollab.adapters import _env_local, safe_anchored_files
from opencollab.adapters._env_base import ENV_FILE_WRITE_LIMIT_BYTES, ExecResult, _is_write_rejected
from opencollab.adapters.env import DockerEnvironment, LocalEnvironment, VMEnvironment
from opencollab.adapters.vm_config import VMConnection
from opencollab.domain.tool_facts import ToolFactsCollector
from opencollab.tools import evidence_tools


def _file_state(target):
    if not target.exists():
        return None
    info = target.stat()
    return target.read_bytes(), info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns

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

@pytest.mark.parametrize("create_parent", [False, True])
async def test_nonregular_target_rejection_accounts_for_parent_creation(tmp_path, monkeypatch, create_parent):
    parent = tmp_path / "parent"
    target = parent / "target"
    if create_parent:
        mkdir = os.mkdir

        def create_directory_with_target(path, mode=0o777, *, dir_fd=None):
            mkdir(path, mode, dir_fd=dir_fd)
            if path == "parent" and dir_fd is not None:
                mkdir(target)

        monkeypatch.setattr(safe_anchored_files.os, "mkdir", create_directory_with_target)
    else:
        target.mkdir(parents=True)
    env = LocalEnvironment(str(tmp_path))
    try:
        with pytest.raises(OSError, match="target is not a regular file") as error:
            await env.write_file_with_change("parent/target", "payload")
        assert type(error.value) is OSError
        assert _is_write_rejected(error.value) is not create_parent
        assert target.is_dir() and not list(target.iterdir())
    finally:
        await env.cleanup()

@pytest.mark.parametrize("operation", ["str_replace", "patch_rejected", "patch_published"])
async def test_local_rejected_and_late_failed_writes_keep_distinct_evidence(tmp_path, monkeypatch, operation):
    target = tmp_path / "input.txt"
    target.write_text("before\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_input.py").write_text(
        "from pathlib import Path\ndef test_input():\n    assert Path('input.txt').read_text() == 'before\\n'\n"
    )
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setattr(_env_local, "LOCAL_FILE_WRITE_LIMIT_BYTES", 64)
    bash, write, patch = evidence_tools("bash", "file_write", "apply_patch", headless=False)
    environment = LocalEnvironment(str(tmp_path))
    observations = ToolFactsCollector()
    runtime = SimpleNamespace(environment=environment, observations=observations, safety_policy=None)
    test = "test_input.py::test_input"
    command = f"{shlex.quote(sys.executable)} -m pytest -c pytest.ini -q -rA -p no:cacheprovider {test}"
    published = operation == "patch_published"
    try:
        assert "1 passed" in await bash.execute_with_runtime({"command": command}, runtime)
        assert bash.verified_targets == {test}

        if published:
            real_fsync = os.fsync
            directory = tmp_path.stat()

            def fail_directory_fsync(fd):
                entry = os.fstat(fd)
                if (entry.st_dev, entry.st_ino) == (directory.st_dev, directory.st_ino):
                    raise OSError(errno.EIO, "directory sync failed")
                real_fsync(fd)

            monkeypatch.setattr(safe_anchored_files.os, "fsync", fail_directory_fsync)
        updated = "after" if published else "\u754c" * 23
        params = {"path": "input.txt", "new_str": updated}
        if operation == "str_replace":
            tool, params = write, params | {"mode": "str_replace", "old_str": "before"}
        else:
            tool, params = patch, params | {"mode": "line_replace", "start_line": 1, "end_line": 1}

        with pytest.raises(OSError, match="directory sync failed" if published else "write limit") as error:
            await tool.execute_with_runtime(params, runtime)
        assert type(error.value) is OSError
        assert error.value.__cause__ is None
        assert error.value.errno == (errno.EIO if published else None)
        assert target.read_text() == ("after\n" if published else "before\n")
        record, = bash.verification_records
        assert record["applicability"] == ("unknown" if published else "current")
        assert record["post_test_edits"] == (["input.txt"] if published else [])
        assert bash.verified_targets == (set() if published else {test})
        assert observations.close().content_changed is (None if published else False)
    finally:
        await environment.cleanup()

@pytest.mark.parametrize(("before", "content", "changed"), [
    (None, "", True), (b"", "", False), (b"same", "same", False), (b"old", "new", True),
    (b"longer", "short", True), (b"\xff\xfe", "text", True),
])
async def test_docker_comparison_uses_one_real_shell_write(monkeypatch, tmp_path, before, content, changed):
    target = tmp_path / "f.txt"
    if before is not None:
        target.write_bytes(before)
    original = _file_state(target)
    executions = []

    async def run_locally(command, *, timeout, input_bytes=None, **_kwargs):
        executions.append((command, input_bytes))
        if input_bytes is None:
            assert "opencollab-file-parent" in command
            assert _file_state(target) == original
        result = subprocess.run(["bash", "-c", command], input=input_bytes, capture_output=True, check=False)
        if input_bytes is None:
            assert _file_state(target) == original
        return ExecResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    env = DockerEnvironment(workspace=str(tmp_path), container_id="a" * 64)
    env._attached_bound = True
    monkeypatch.setattr(env, "_exec", run_locally)
    assert await env.write_file_with_change(str(target), content) is changed
    assert target.read_text() == content
    assert len(executions) == 2
    assert [payload for _command, payload in executions] == [None, content.encode("utf-8")]
    assert "opencollab-file-parent" in executions[0][0]
    assert "opencollab-write " in executions[1][0]
    assert not list(tmp_path.glob(".*.opencollab-write-*.tmp"))

@pytest.mark.parametrize("backend", ["docker", "vm"])
@pytest.mark.parametrize("operation", ["overwrite", "patch", "remote-error"])
async def test_remote_write_evidence_for_size_limits_and_publication_errors(tmp_path, monkeypatch, backend, operation):
    path = tmp_path / "input.txt"
    source = "VALUE\n" + "x" * (ENV_FILE_WRITE_LIMIT_BYTES - len("VALUE\n"))
    path.write_text(source)
    (tmp_path / "test_input.py").write_text(
        "from pathlib import Path\ndef test_input():\n    assert Path('input.txt').read_text().startswith('VALUE')\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    writes = []

    def run_shell(command, payload=None):
        child = subprocess.run(
            ["bash", "-c", command], input=payload, capture_output=True, check=False, cwd=tmp_path,
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        )
        return ExecResult(child.returncode, child.stdout.decode(), child.stderr.decode())

    if backend == "docker":
        environment = DockerEnvironment(workspace=str(tmp_path), container_id="a" * 64)
        environment._attached_bound = True
        error_type = OSError

        async def execute(command, *, input_bytes=None, **_kwargs):
            result = run_shell(command, input_bytes)
            if input_bytes is None:
                return result
            writes.append(input_bytes)
            assert result.returncode == 0
            return ExecResult(1, "", "publication receipt unavailable")

        monkeypatch.setattr(environment, "_exec", execute)
    else:
        environment = VMEnvironment(VMConnection("guest", "worker"), str(tmp_path),
                                    state_directory="/var/tmp/oc-test", run_id="evidence-test")
        environment._identity = {"workspace": environment.workspace}
        error_type = ValueError

        async def rpc(payload):
            if payload["method"] == "cancel":
                return {"status": "cancelled"}
            assert payload["method"] == "start"
            request = payload["request"]
            action, args = request["action"], request["args"]
            if action == "lock":
                return {"status": "running", "lock_acquired": True}
            if action == "exec":
                result = run_shell(args["cmd"])
                value = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                         "stdout_bytes": None}
            elif action == "read":
                value = path.read_text()
            else:
                assert action == "write"
                writes.append(args["content"])
                path.write_text(args["content"])
                return {"status": "failed", "error": "ValueError", "message": "VM file exceeds write limit"}
            return {"status": "completed", "value": value}

        monkeypatch.setattr(environment, "_rpc", rpc)

    bash, file_write, apply_patch = evidence_tools("bash", "file_write", "apply_patch", headless=False)
    observations = ToolFactsCollector()
    runtime = SimpleNamespace(environment=environment, observations=observations, safety_policy=None)
    command = (f"{shlex.quote(sys.executable)} -m pytest -c pytest.ini -q -rA "
               "-p no:cacheprovider test_input.py::test_input")
    assert "1 passed" in await bash.execute_with_runtime({"command": command}, runtime)
    targets = {"test_input.py::test_input"}
    assert bash.verified_targets == targets
    if operation == "patch":
        tool, params = apply_patch, {"path": "input.txt", "mode": "line_replace", "start_line": 1,
                                     "end_line": 1, "new_str": "LONGER_VALUE"}
    else:
        content = "different\n" if operation == "remote-error" else source + "a"
        tool, params = file_write, {"path": "input.txt", "mode": "create", "content": content, "overwrite": True}

    published = operation == "remote-error"
    message = "write verification failed" if published and backend == "docker" else "exceeds write limit"
    with pytest.raises(error_type, match=message) as error:
        await tool.execute_with_runtime(params, runtime)
    assert type(error.value) is error_type
    assert len(writes) == int(published)
    assert path.read_text() == ("different\n" if published else source)
    record, = bash.verification_records
    assert record["applicability"] == ("unknown" if published else "current")
    assert record["post_test_edits"] == (["input.txt"] if published else [])
    assert bash.verified_targets == (set() if published else targets)
    assert observations.close().content_changed is (None if published else False)


@pytest.mark.parametrize("comparison", [
    "exit 2",
    'printf external > "$4.swap"; mv -f "$4.swap" "$4"; exit 0',
])
async def test_docker_comparison_error_or_drift_still_publishes(monkeypatch, tmp_path, comparison):
    target = tmp_path / "f.txt"
    target.write_text("old")
    original = _file_state(target)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    cmp = fake_bin / "cmp"
    cmp.write_text(f"#!/bin/sh\n{comparison}\n")
    cmp.chmod(0o755)
    executions = []

    async def run_locally(command, *, timeout, input_bytes=None, **_kwargs):
        executions.append((command, input_bytes))
        if input_bytes is None:
            assert "opencollab-file-parent" in command
            assert _file_state(target) == original
        result = subprocess.run(
            ["bash", "-c", command], input=input_bytes, capture_output=True, check=False,
            env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        )
        if input_bytes is None:
            assert _file_state(target) == original
        return ExecResult(result.returncode, result.stdout.decode(), result.stderr.decode())

    env = DockerEnvironment(workspace=str(tmp_path), container_id="a" * 64)
    env._attached_bound = True
    monkeypatch.setattr(env, "_exec", run_locally)
    assert await env.write_file_with_change(str(target), "new") is None
    assert target.read_text() == "new"
    assert len(executions) == 2
    assert [payload for _command, payload in executions] == [None, b"new"]
    assert "opencollab-file-parent" in executions[0][0]
    assert "opencollab-write " in executions[1][0]
