"""SDK workflow evidence retains its original outcome when owned cleanup fails."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path

import pytest

from opencollab import OpenCollab, RunError
from opencollab.adapters import _env_local
from opencollab.bootstrap.workflow_runtime import WorkflowDeadlineExceeded
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _causes(error):
    found = []
    pending = [error]
    while pending:
        candidate = pending.pop()
        if candidate is None or any(candidate is seen for seen in found):
            continue
        found.append(candidate)
        pending.extend((candidate.__cause__, candidate.__context__))
    return found


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "workflow_exception", "deadline"])
async def test_workflow_cleanup_preserves_outcome_and_errors(tmp_path, monkeypatch, outcome, cleanup_fails):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inventory.txt").write_text("Inventory subtotal 42\n", encoding="utf-8")
    invalid = "missing-column-value"
    (workspace / "calibration.txt").write_text(
        invalid if outcome == "workflow_exception" else "3", encoding="utf-8",
    )

    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        tool_results = [row["content"] for row in body["messages"] if row["role"] == "tool"]
        if tool_results:
            assert "Inventory subtotal 42" in tool_results[-1]
            return completion_http_response(request, text="Verified subtotal 42.", output_tokens=3)
        return http_response(request, 200, json={
            "id": "inventory-completion", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "inventory-read", "type": "function", "function": {
                        "name": "file_read", "arguments": json.dumps({"path": "inventory.txt"}),
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        })

    install_sdk_transport(monkeypatch, "openai", respond)
    artifacts = tmp_path / "artifacts"
    path = artifacts / "workflow.json"
    owned_files = []
    before_cleanup = []
    cleanup_error = OSError(errno.EIO, "verifier scratch cleanup I/O error")
    fault_armed = cleanup_fails
    unlink_file = _env_local.unlink_regular_file_durable_at

    def unlink(root_fd, base, relative_path, **kwargs):
        if fault_armed and str(relative_path).startswith("verification-"):
            before_cleanup.append(json.loads(path.read_text()))
            raise cleanup_error
        return unlink_file(root_fd, base, relative_path, **kwargs)

    monkeypatch.setattr(_env_local, "unlink_regular_file_durable_at", unlink)

    async def flow(ctx, _inputs):
        report = await ctx.agent("Read the inventory subtotal.", tools="read", label="verifier")
        assert report == "Verified subtotal 42."
        environment = ctx.sessions[0].env
        scratch = Path(await environment.write_temp_file(report, prefix="verification-"))
        owned_files.append((environment, scratch))
        assert scratch.read_text() == report
        if outcome == "deadline":
            await asyncio.Future()
        return {"report": report, "calibration": int((workspace / "calibration.txt").read_text())}

    app = OpenCollab(
        workspace, model="offline-verifier", provider="openai",
        api_key="fixture-unused",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0},
    )
    expected = {
        "workflow_exception": ("failed", "workflow_exception", "ValueError"),
        "deadline": ("stopped", "cancelled", "CancelledError"),
        "completed": ("completed", None, None),
    }[outcome]
    operation = app.workflow(
        flow, artifacts=artifacts, trace=False, budget=80_000, cleanup_timeout=1,
        timeout=2 if outcome == "deadline" else None, run_id="inventory-audit",
    )
    try:
        if cleanup_fails:
            with pytest.raises(RunError) as caught:
                await operation
            causes = _causes(caught.value)
            assert cleanup_error in causes
            assert all(not item["evidence_complete"] for item in before_cleanup)
            assert before_cleanup
            if outcome == "workflow_exception":
                assert any(isinstance(exc, ValueError) and invalid in str(exc) for exc in causes)
            elif outcome == "deadline":
                deadline, = [exc for exc in causes if isinstance(exc, WorkflowDeadlineExceeded)]
                assert str(deadline) == "workflow wall-clock deadline expired"
                assert deadline.result is not None and deadline.result.tokens == 10
            else:
                expected = ("failed", "environment_cleanup_failed", "ProgrammaticLifecycleError")
            assert owned_files[0][1].is_file()
        else:
            result = await operation
            assert result.tokens == 10 and result.metrics["execution_quiesced"] is True
            if outcome == "workflow_exception":
                assert result.status == "failed" and invalid in result.reason
                assert isinstance(result.error, ValueError)
            elif outcome == "deadline":
                assert result.status == "stopped" and result.reason == "timeout"
                assert isinstance(result.error, WorkflowDeadlineExceeded)
            else:
                assert result.ok and result.output == {"report": "Verified subtotal 42.", "calibration": 3}
            assert not owned_files[0][1].exists()

        assert len(requests) == 2
        manifest = json.loads(path.read_text())
        assert manifest["run_id"] == "inventory-audit"
        assert manifest["tokens_spent"] == 10
        assert (manifest["status"], manifest["reason"], manifest["failure_type"]) == expected
        assert manifest["evidence_complete"] is (not cleanup_fails)
    finally:
        fault_armed = False
        for environment, scratch in owned_files:
            if scratch.exists():
                await environment.cleanup()
