"""Public SDK failure delivery and recovery through real Git and provider SDKs."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters import worktree_pool
from opencollab.adapters.llm import retry
from opencollab.application._scheduler_constants import MAX_TEAMMATE_MESSAGE_BYTES
from opencollab.bootstrap import programmatic
from opencollab.domain.pending import PendingEventTable, RowStatus
from opencollab.domain.session import SessionPhase
from opencollab.environments import attach_container, local_environment
from tests.support.provider_sdk_http import http_response, install_sdk_transport
from tests.support.worktree_delivery_support import CONTAINER_ID, apply_and_execute, git, make_repo
from tests.support.worktree_delivery_support import worktree_transport as worktree_transport


def _tool_call(step: int, name: str, arguments: dict) -> dict:
    return {
        "id": f"call-{step}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def _run_team(
    tmp_path, monkeypatch, *, backend, ending, trace, capture_failure=None,
    prebuilt=False, payload=None,
):
    repo = make_repo(tmp_path / "repo")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        f"  lead:\n    prompt: ROLE_LEAD\n    tools: [{'message_agent' if prebuilt else 'spawn_agent'}]\n"
        "  coder:\n    prompt: ROLE_CODER\n    tools: [file_write, file_read]\n"
        "topology:\n  lead: [coder]\n", encoding="utf-8",
    )
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.setattr(worktree_pool, "CONTAINER_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setattr(retry, "RETRY_JITTER_MAX_SECONDS", 0)
    observed = {"steps": {}, "outages": 0, "captures": 0}
    last_tool_step = 3 if payload is not None else 2

    def handler(request):
        messages = json.loads(request.content)["messages"]
        role = "lead" if "ROLE_LEAD" in str(messages[0].get("content")) else "coder"
        if role == "lead":
            observed.setdefault("lead_inputs", []).append(messages)
        step = observed["steps"].get(role, 0) + 1
        observed["steps"][role] = step
        if role == "coder" and step > last_tool_step and ending == "api":
            observed["outages"] += 1
            return http_response(
                request, 503, headers={"retry-after": "0"},
                json={"error": {"message": "Temporary provider outage", "type": "server_error"}},
            )
        calls = []
        if role == "lead" and step == 1:
            calls = [_tool_call(step, "message_agent", {
                "to_aid": 1, "summary": "write", "content": "Write and read solution.py.",
            })] if prebuilt else [_tool_call(
                step, "spawn_agent", {"role": "coder", "task": "Write and read solution.py."},
            )]
        elif role == "coder" and step == 1:
            calls = [_tool_call(step, "file_write", {
                "path": "solution.py", "mode": "create", "content": "print(42)\n",
            })]
        elif role == "coder" and step == 2 and payload is not None:
            calls = [_tool_call(step, "file_write", {
                "path": "payload.txt", "mode": "create", "content": payload,
            })]
        elif role == "coder" and step == last_tool_step:
            workspace = Path(observed["env"].workspace)
            assert (workspace / "solution.py").read_text() == "print(42)\n"
            executed = subprocess.run(
                [sys.executable, str(workspace / "solution.py")],
                check=True, capture_output=True, text=True,
            )
            assert executed.stdout == "42\n"
            if capture_failure == "ignored":
                cache = workspace / "__pycache__"
                cache.mkdir()
                (cache / "solution.pyc").write_bytes(b"cache")
            calls = [_tool_call(step, "file_read", {"path": "solution.py"})]
        tool_results = [str(message.get("content") or "") for message in messages if message.get("role") == "tool"]
        if role == "lead" and step == 2:
            observed["parent_result"] = tool_results[-1]
        return http_response(request, 200, json={
            "id": f"{role}-{step}", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": None if calls else "\n".join(tool_results),
                **({"tool_calls": calls} if calls else {}),
            }, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    install_sdk_transport(monkeypatch, "openai", handler)
    original_acquire = worktree_pool.WorktreePool.acquire

    async def acquire(pool, role):
        env = await original_acquire(pool, role)
        observed["env"] = env
        observed["pool"] = pool
        original_diff = env.get_diff

        async def capture():
            observed["captures"] += 1
            return await original_diff()

        monkeypatch.setattr(env, "get_diff", capture)
        if capture_failure == "transient":
            executor = env if backend == "container" else env._local_env
            original_exec = executor.exec_cmd

            async def truncate_once(*args, **kwargs):
                result = await original_exec(*args, **kwargs)
                if "GIT_INDEX_FILE" in args[0] and observed["captures"] == 1:
                    result.stdout_truncated = True
                return result

            monkeypatch.setattr(executor, "exec_cmd", truncate_once)
        return env

    monkeypatch.setattr(worktree_pool.WorktreePool, "acquire", acquire)
    original_build = programmatic.build_scheduler

    def build(*args, **kwargs):
        scheduler = original_build(*args, **kwargs)
        observed["scheduler"] = scheduler
        return scheduler

    monkeypatch.setattr(programmatic, "build_scheduler", build)
    original_fill = PendingEventTable.fill

    def fill(table, tool_call_id, *args, **kwargs):
        original_fill(table, tool_call_id, *args, **kwargs)
        if tool_call_id == "call-1":
            observed["parent_row"] = table.rows[tool_call_id]

    monkeypatch.setattr(PendingEventTable, "fill", fill)
    if backend == "container":
        environment = attach_container(container_id=CONTAINER_ID, workspace=str(repo))
    else:
        environment = local_environment(repo)
    result = await OpenCollab(
        repo, model="offline", api_key="local-fixture", base_url="https://offline.example.invalid/v1",  # pragma: allowlist secret
        environment=environment,
    ).team(
        "Write and read the script.", config=team, budget=1_000_000, timeout=15,
        max_steps=last_tool_step if ending == "stopped" else 100, use_worktrees=True,
        trace=trace, artifacts=tmp_path / "artifacts", prebuild_team=prebuilt, serialize_turns=prebuilt,
    )
    observed.update({"repo": repo, "result": result})
    return observed


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize("ending", ["normal", "api", "stopped"])
@pytest.mark.parametrize("trace", [False, True])
async def test_sdk_delivers_partial_changes_and_preserves_failure(
    worktree_transport, tmp_path, monkeypatch, backend, ending, trace,
):
    observed = await _run_team(tmp_path, monkeypatch, backend=backend, ending=ending, trace=trace)
    result = observed["result"]
    scheduler = observed["scheduler"]
    child = scheduler.table.get(1)
    parent_row = observed["parent_row"]
    assert result.status == "completed"
    assert "[Changes made in worktree]" in observed["parent_result"]
    assert observed["parent_result"] == child.result == parent_row.result
    assert observed["captures"] == 1
    assert parent_row.status is (RowStatus.DONE if ending == "normal" else RowStatus.FAILED)
    expected_phase = {"normal": SessionPhase.DONE, "api": SessionPhase.ERROR, "stopped": SessionPhase.STOPPED}[ending]
    assert child.state.phase is expected_phase
    if ending != "normal":
        assert result.agent_failures
        assert observed["parent_result"].startswith("Error:")
        assert parent_row.error in observed["parent_result"]
    else:
        assert not result.agent_failures
    if ending == "api":
        assert observed["outages"] == 4
        assert result.agent_failures[0]["exception_type"] == "InternalServerError"
    if trace:
        records = [json.loads(line) for line in Path(scheduler._tracer.path).read_text().splitlines()]
        changes = [record["payload"] for record in records if record["type"] == "worktree_changes"]
        assert len(changes) == 1
        assert changes[0]["aid"] == 1
        assert [entry["path"] for entry in changes[0]["files"]] == ["solution.py"]
    assert not Path(observed["env"].workspace).exists()
    assert git(observed["repo"], "worktree", "list", "--porcelain").count("worktree ") == 1
    patch = observed["parent_result"].split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
    apply_and_execute(observed["repo"], patch)
    await observed["pool"].release()
    await observed["env"].cleanup()


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize("ending", ["normal", "api", "stopped"])
@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("recovery", ["get_diff", "revoked_git"])
async def test_sdk_capture_failure_survives_shutdown_and_git_recovery(
    worktree_transport, tmp_path, monkeypatch, backend, ending, trace, recovery,
):
    observed = await _run_team(
        tmp_path, monkeypatch, backend=backend, ending=ending, trace=trace, capture_failure="transient",
    )
    result = observed["result"]
    env = observed["env"]
    workspace = Path(env.workspace)
    try:
        assert result.status == "failed"
        assert result.agent_failures
        assert "diff extraction failed" in observed["parent_result"].lower()
        assert "```diff" not in observed["parent_result"]
        assert observed["captures"] == 1
        scheduler = observed["scheduler"]
        assert scheduler._shutting_down
        assert not scheduler._tasks
        for session in scheduler._sessions.values():
            assert session._llm._closed
            assert session._llm._openai.is_closed()
        assert scheduler._tracer is None or scheduler._tracer._closed
        for _ in range(2):
            with pytest.raises(OSError, match="retry state retained"):
                await observed["pool"].release()
            assert (workspace / "solution.py").read_text() == "print(42)\n"
            assert f"worktree {workspace.resolve()}" in git(observed["repo"], "worktree", "list", "--porcelain", "-z")
        if recovery == "get_diff":
            patch = await env.get_diff()
        else:
            env.revoke()
            with pytest.raises(RuntimeError, match="aborted"):
                await env.get_diff()
            git(workspace, "add", "solution.py")
            patch = git(workspace, "diff", "--cached", env._base_commit)
        apply_and_execute(observed["repo"], patch)
        if recovery == "get_diff":
            await observed["pool"].release()
            await observed["pool"].release()
            assert not workspace.exists()
    finally:
        if workspace.exists():
            git(observed["repo"], "worktree", "remove", "--force", str(workspace))
            git(observed["repo"], "update-ref", "-d", f"refs/heads/{env._branch}", env._base_commit)


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize("trace", [False, True])
async def test_sdk_ignored_cache_capture_failure_retains_the_real_files(
    worktree_transport, tmp_path, monkeypatch, backend, trace,
):
    observed = await _run_team(
        tmp_path, monkeypatch, backend=backend, ending="normal", trace=trace, capture_failure="ignored",
    )
    env = observed["env"]
    workspace = Path(env.workspace)
    try:
        assert observed["result"].status == "failed"
        assert "ignored untracked files" in observed["parent_result"]
        assert (workspace / "solution.py").read_text() == "print(42)\n"
        assert (workspace / "__pycache__" / "solution.pyc").read_bytes() == b"cache"
    finally:
        shutil.rmtree(workspace / "__pycache__")
        git(observed["repo"], "worktree", "remove", "--force", str(workspace))
        git(observed["repo"], "update-ref", "-d", f"refs/heads/{env._branch}", env._base_commit)


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize("ending", ["api", "stopped"])
@pytest.mark.parametrize("trace", [False, True])
async def test_prebuilt_sender_receives_small_failed_patch(
    worktree_transport, tmp_path, monkeypatch, backend, ending, trace,
):
    observed = await _run_team(
        tmp_path, monkeypatch, backend=backend, ending=ending, trace=trace, prebuilt=True,
    )
    scheduler = observed["scheduler"]
    child = scheduler.table.get(1)
    notices = [message["content"].split("</team-notice>", 1)[0] + "</team-notice>"
               for message in scheduler._sessions[0].messages
               if "<team-notice " in str(message.get("content"))]
    assert len(notices) == 1
    assert len(notices[0].encode()) <= MAX_TEAMMATE_MESSAGE_BYTES
    content = ET.fromstring(notices[0]).text
    assert "has stopped" in content
    assert "[Changes made in worktree]" in content and "+print(42)" in content
    if ending == "api":
        assert "+print(42)" in json.dumps(observed["lead_inputs"])
    assert child.state.phase in {SessionPhase.ERROR, SessionPhase.STOPPED}
    assert observed["result"].agent_failures
    assert not Path(observed["env"].workspace).exists()
    patch = content.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
    apply_and_execute(observed["repo"], patch)


@pytest.mark.parametrize("backend", ["local", "container"])
@pytest.mark.parametrize("ending", ["api", "stopped"])
@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("prebuilt", [False, True])
async def test_truncated_failed_patch_retains_complete_files_after_sdk_shutdown(
    worktree_transport, tmp_path, monkeypatch, backend, ending, trace, prebuilt,
):
    payload = "".join(f"line {index} \u4e2d\u6587 & <tag> value\n" for index in range(1800))
    observed = await _run_team(
        tmp_path, monkeypatch, backend=backend, ending=ending, trace=trace,
        prebuilt=prebuilt, payload=payload,
    )
    env = observed["env"]
    workspace = Path(env.workspace)
    try:
        assert observed["result"].agent_failures
        assert observed["captures"] == 1
        assert (workspace / "payload.txt").read_text() == payload
        if prebuilt:
            notices = [message["content"].split("</team-notice>", 1)[0] + "</team-notice>"
                       for message in observed["scheduler"]._sessions[0].messages
                       if "<team-notice " in str(message.get("content"))]
            assert len(notices) == 1
            assert len(notices[0].encode()) <= MAX_TEAMMATE_MESSAGE_BYTES
            delivered = ET.fromstring(notices[0]).text
        else:
            delivered = observed["parent_result"]
            assert "chars truncated" in delivered
        assert str(workspace) in delivered and "retained" in delivered
        for session in observed["scheduler"]._sessions.values():
            assert session._llm._closed
        for _ in range(2):
            with pytest.raises(OSError, match="retry state retained"):
                await observed["pool"].release()
            assert (workspace / "payload.txt").read_text() == payload
        patch = await env.get_diff()
        assert env.recovery_location is None
        apply_and_execute(observed["repo"], patch)
        assert (observed["repo"] / "payload.txt").read_bytes() == payload.encode()
        await observed["pool"].release()
        await observed["pool"].release()
        assert not workspace.exists()
    finally:
        if workspace.exists():
            git(observed["repo"], "worktree", "remove", "--force", str(workspace))
            git(observed["repo"], "update-ref", "-d", f"refs/heads/{env._branch}", env._base_commit)
