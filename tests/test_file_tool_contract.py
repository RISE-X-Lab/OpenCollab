"""Contract checks for the file-touching tools: read, write and apply_patch.

Split out of ``test_tool_runtime_contract.py``. These cover the read and write
paths through ``ToolRuntime`` plus the host write lock, which must only be
taken when the I/O really lands on the host filesystem.
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from tool_runtime_test_support import FakeRemoteEnv, SpyLock, run

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.safety import SandboxInterceptor
from opencollab.adapters.tools import base
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.application.tool_execution import ToolRuntime

# ---------------------------------------------------------------------------
# FileReadTool
# ---------------------------------------------------------------------------


def test_file_read_reads_through_runtime(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    result = run(
        FileReadTool().execute_with_runtime(
            {"path": "note.txt", "offset": 2, "limit": 1},
            runtime,
        )
    )

    assert "File: note.txt (3 lines total, showing 2-2)" in result
    assert "2\tbeta" in result
    assert "1\talpha" not in result


def test_file_read_small_range_bypasses_full_file_limit(tmp_path):
    target = tmp_path / "large.txt"
    target.write_bytes(b"first\nsecond\n" + b"x" * (5 * 1024 * 1024))
    runtime = ToolRuntime(
        environment=LocalEnvironment(str(tmp_path)),
        safety_policy=None,
        permission_policy=None,
    )

    result = run(
        FileReadTool().execute_with_runtime(
            {"path": "large.txt", "offset": 1, "limit": 2},
            runtime,
        )
    )

    assert "1\tfirst" in result
    assert "2\tsecond" in result
    assert "more lines below" in result


def test_file_read_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(FileReadTool().execute_with_runtime({"path": "/etc/passwd"}, runtime))


def test_file_read_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "missing.txt"}, runtime))

    assert result == "Error: no execution environment available."


def test_file_read_preserves_file_not_found(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "missing.txt"}, runtime))

    assert result == "Error: file not found: missing.txt"


def test_file_read_preserves_permission_error_string(monkeypatch, tmp_path):
    async def raise_permission_error(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(LocalEnvironment, "read_text_range", raise_permission_error)
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(FileReadTool().execute_with_runtime({"path": "secret.txt"}, runtime))

    assert result == "Error: denied"


def test_file_read_description_teaches_distill_and_forbids_reread():
    # The file_read description is the UNIVERSAL carrier of the anti-thrash rule:
    # every workflow/team that has the tool sees it, regardless of its prompt. Pins
    # the rule (distill into notes; trust the cleared stub; don't re-read to
    # reconfirm) motivated by a 90-read/0-write/step-cap stall.
    desc = FileReadTool.description
    assert "Distill as you read" in desc
    assert "empty reply" in desc  # forbids bare tool-call turns
    assert "re-read" in desc      # trust the stub instead



# ---------------------------------------------------------------------------
# FileWriteTool
# ---------------------------------------------------------------------------


def test_file_write_create_and_str_replace(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    create_result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "alpha\n"},
            runtime,
        )
    )
    replace_result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert "Created/wrote" in create_result
    assert "Replaced in" in replace_result
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "beta\n"


@pytest.mark.parametrize("existing", [False, True])
def test_file_write_create_requires_explicit_content(tmp_path, existing):
    target = tmp_path / "note.txt"
    if existing:
        target.write_text("KEEP", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create"},
            runtime,
        )
    )

    assert result == "Error: content is required for create mode."
    assert target.exists() is existing
    if existing:
        assert target.read_text(encoding="utf-8") == "KEEP"


def test_file_write_create_allows_explicit_empty_content(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "empty.txt", "mode": "create", "content": ""},
            runtime,
        )
    )

    assert "Created/wrote" in result
    assert (tmp_path / "empty.txt").read_text(encoding="utf-8") == ""


def test_file_write_preserves_workspace_path_jail(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = LocalEnvironment(str(workspace))
    sandbox = SandboxInterceptor(str(workspace))
    runtime = ToolRuntime(environment=env, safety_policy=sandbox, permission_policy=None)

    with pytest.raises(PermissionError):
        run(
            FileWriteTool().execute_with_runtime(
                {"path": "/tmp/outside.txt", "mode": "create", "content": "nope"},
                runtime,
            )
        )


def test_file_write_requires_environment():
    runtime = ToolRuntime(environment=None, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "x"},
            runtime,
        )
    )

    assert result == "Error: no execution environment available."


def test_file_write_preserves_missing_old_str(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("alpha\n", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "new_str": "beta"},
            runtime,
        )
    )

    assert result == "Error: old_str is required for str_replace mode."


def test_file_write_preserves_duplicate_old_str_error(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("alpha\nalpha\n", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert (
        result
        == "Error: old_str found 2 times in note.txt. Provide more context to make it unique."
    )


def test_file_write_rejects_overlapping_old_str_matches(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("aaa", encoding="utf-8")
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "aa", "new_str": "b"},
            runtime,
        )
    )

    assert (
        result
        == "Error: old_str found 2 times in note.txt. Provide more context to make it unique."
    )
    assert target.read_text(encoding="utf-8") == "aaa"


# ---------------------------------------------------------------------------
# Host write lock — only taken when I/O hits the host filesystem
# ---------------------------------------------------------------------------


def test_file_write_remote_env_takes_no_host_lock(monkeypatch):
    SpyLock.instances = []
    monkeypatch.setattr(base, "FileLock", SpyLock)
    env = FakeRemoteEnv({"note.txt": "alpha\n"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "str_replace", "old_str": "alpha", "new_str": "beta"},
            runtime,
        )
    )

    assert "Replaced in" in result
    assert env.files["note.txt"] == "beta\n"
    assert SpyLock.instances == []


def test_apply_patch_remote_env_takes_no_host_lock(monkeypatch):
    SpyLock.instances = []
    monkeypatch.setattr(base, "FileLock", SpyLock)
    env = FakeRemoteEnv({"f.py": "a\nb\nc\nd\n"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        ApplyPatchTool().execute_with_runtime(
            {
                "path": "f.py",
                "mode": "line_replace",
                "start_line": 2,
                "end_line": 2,
                "new_str": "B",
            },
            runtime,
        )
    )

    assert "Applied line_replace" in result
    assert env.files["f.py"] == "a\nB\nc\nd\n"
    assert SpyLock.instances == []


def test_host_write_lock_files_live_outside_target_workspace(tmp_path):
    env = LocalEnvironment(str(tmp_path))
    lock_path = Path(base._host_lock_path("x.txt", env))

    async def exercise():
        async with base.host_write_lock("x.txt", env) as lock:
            assert isinstance(lock, base.FileLock)
            assert lock_path.exists()

    run(exercise())

    assert tmp_path not in lock_path.parents
    assert list(tmp_path.rglob("*.lock")) == []


class SlowLocalWriteEnv(FakeRemoteEnv):
    local_filesystem = True
    workspace = "/virtual/workspace"

    async def read_file(self, path: str) -> str:
        await asyncio.sleep(0.05)
        return await super().read_file(path)

    async def write_file(self, path: str, content: str) -> None:
        await asyncio.sleep(0.05)
        await super().write_file(path, content)


def test_concurrent_host_writes_do_not_block_event_loop_on_lock():
    env = SlowLocalWriteEnv({"note.txt": "initial"})
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)
    tool = FileWriteTool()

    async def exercise():
        await asyncio.wait_for(
            asyncio.gather(
                tool.execute_with_runtime(
                    {
                        "path": "note.txt",
                        "mode": "create",
                        "content": "first",
                        "overwrite": True,
                    },
                    runtime,
                ),
                tool.execute_with_runtime(
                    {
                        "path": "note.txt",
                        "mode": "create",
                        "content": "second",
                        "overwrite": True,
                    },
                    runtime,
                ),
            ),
            timeout=1.0,
        )

    started = time.monotonic()
    run(exercise())

    assert time.monotonic() - started < 1.0
    assert env.files["note.txt"] in {"first", "second"}


def test_host_write_lock_never_pollutes_git_diff(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    env = LocalEnvironment(str(tmp_path))
    runtime = ToolRuntime(environment=env, safety_policy=None, permission_policy=None)

    result = run(
        FileWriteTool().execute_with_runtime(
            {"path": "note.txt", "mode": "create", "content": "hello\n"},
            runtime,
        )
    )
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert result.startswith("Created/wrote")
    assert ".lock" not in status
    assert list(tmp_path.rglob("*.lock")) == []
