"""End-to-end tests for OpenCollab tools over the remote environment."""

from __future__ import annotations

import os

import pytest

from opencollab.adapters.execution.remote import RemoteEnvironment
from opencollab.application.tool_execution import ToolRuntime
from opencollab.tools import builtin_tools

pytestmark = [
    pytest.mark.external_server,
    pytest.mark.skipif(
        os.environ.get("OPENCOLLAB_TEST_EXECUTION_SERVER") != "1",
        reason="set OPENCOLLAB_TEST_EXECUTION_SERVER=1 with a server on port 8080",
    ),
]

BASE = "http://127.0.0.1:8080"
# built immutable base (create() rejects unknown bases); Docker default unchanged.
IMAGE = os.environ.get("EXECSERVER_TEST_IMAGE", "python:3.11-slim")


@pytest.fixture
async def env():
    e = RemoteEnvironment(BASE, image=IMAGE)
    await e.setup()
    try:
        yield e
    finally:
        await e.cleanup()


@pytest.fixture
def tools():
    got = builtin_tools("bash", "file_read", "file_write", "apply_patch", "git_diff", "run_tests")
    return {t.name: t for t in got}


def _runtime(env) -> ToolRuntime:
    # No safety/permission policy needed for the harness; the sandbox is the
    # boundary. This mirrors how a headless OpenCollab run wires a tool.
    return ToolRuntime(environment=env, safety_policy=None, permission_policy=None)


async def _has(env, binary_check: str) -> bool:
    r = await env.exec_cmd(binary_check)
    return r.returncode == 0


# --------------------------------------------------------------------------

async def test_bash_runs_remotely(env, tools):
    out = await tools["bash"].execute_with_runtime(
        {"command": "echo hello-from-remote"}, _runtime(env))
    assert "hello-from-remote" in out


async def test_file_write_then_read_roundtrip(env, tools):
    rt = _runtime(env)
    await tools["file_write"].execute_with_runtime(
        {"path": "notes.txt", "mode": "create", "content": "line-one\nline-two\n"}, rt)
    out = await tools["file_read"].execute_with_runtime({"path": "notes.txt"}, rt)
    assert "line-one" in out
    assert "line-two" in out


async def test_file_write_str_replace(env, tools):
    rt = _runtime(env)
    await tools["file_write"].execute_with_runtime(
        {"path": "cfg.txt", "mode": "create", "content": "host = old\n"}, rt)
    await tools["file_write"].execute_with_runtime(
        {"path": "cfg.txt", "mode": "str_replace",
         "old_str": "host = old", "new_str": "host = new"}, rt)
    out = await tools["file_read"].execute_with_runtime({"path": "cfg.txt"}, rt)
    assert "host = new" in out
    assert "old" not in out.split("host = new")[-1]


async def test_git_diff_when_git_available(env, tools):
    if not await _has(env, "command -v git"):
        pytest.skip("image has no git (python:3.11-slim); use a git-provisioned image")
    rt = _runtime(env)
    setup = (
        "git init -q && git config user.email a@b.c && git config user.name t && "
        "printf 'v1\\n' > f.txt && git add -A && git commit -qm init && "
        "printf 'v2\\n' >> f.txt"
    )
    r = await env.exec_cmd(setup)
    assert r.returncode == 0, r.stderr
    out = await tools["git_diff"].execute_with_runtime({}, rt)
    assert "f.txt" in out


async def test_apply_patch_edits_remote_file_and_rejects_stale_content(env, tools):
    rt = _runtime(env)
    await tools["file_write"].execute_with_runtime(
        {"path": "patch.txt", "mode": "create", "content": "one\ntwo\nthree\n"}, rt)
    params = {"path": "patch.txt", "mode": "line_replace", "start_line": 2,
              "end_line": 2, "expected_str": "two", "new_str": "updated"}
    out = await tools["apply_patch"].execute_with_runtime(params, rt)
    assert "Applied line_replace" in out
    assert await env.read_file("patch.txt") == "one\nupdated\nthree\n"
    out = await tools["apply_patch"].execute_with_runtime(params, rt)
    assert "expected_str does not match" in out
    assert await env.read_file("patch.txt") == "one\nupdated\nthree\n"


async def test_run_tests_when_pytest_available(env, tools):
    if not await _has(env, "python -m pytest --version"):
        pytest.skip("image has no pytest (python:3.11-slim); use a pytest-provisioned image")
    rt = _runtime(env)
    await tools["file_write"].execute_with_runtime(
        {"path": "test_ok.py", "mode": "create",
         "content": "def test_ok():\n    assert 1 + 1 == 2\n"}, rt)
    out = await tools["run_tests"].execute_with_runtime({"target": "test_ok.py"}, rt)
    assert "passed" in out.lower() or "1 passed" in out.lower()


async def test_tools_see_process_isolation(env, tools):
    # bash refuses to run against an environment that is not process-isolated.
    # RemoteEnvironment advertises process_isolated=True, so bash proceeds
    # rather than returning its "bash is disabled" guard string.
    out = await tools["bash"].execute_with_runtime({"command": "true"}, _runtime(env))
    assert "bash is disabled" not in out
