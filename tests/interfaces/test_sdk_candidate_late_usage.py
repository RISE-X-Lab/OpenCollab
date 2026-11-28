from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.storage import SessionStore
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.workflows.test_workflow_candidate_workspace import _git, _repository


async def test_cancelled_candidate_traces_late_provider_usage(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    worktrees = _git(repo, "worktree", "list", "--porcelain")
    artifacts = tmp_path / "artifacts"
    usage_log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_log))
    correcting, cancelled, release, sibling_started = (asyncio.Event() for _ in range(4))

    async def handler(request):
        body = json.loads(request.content)
        prompt = next(row["content"] for row in reversed(body["messages"]) if row["role"] == "user")
        if "read sibling" in prompt:
            sibling_started.set()
            return completion_http_response(request, text="sibling answer", output_tokens=5)
        if "You MUST call" not in prompt:
            return completion_http_response(request, text="Measured 42", output_tokens=6)

        assert any(row.get("content") == "Measured 42" for row in body["messages"])
        correcting.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        payload = completion_http_response(request, text=None, output_tokens=17).json()
        payload["choices"][0]["finish_reason"] = "tool_calls"
        payload["choices"][0]["message"]["tool_calls"] = [{
            "id": "late-measurement", "type": "function",
            "function": {"name": "structured_output", "arguments": '{"measurement":42}'},
        }]
        return http_response(request, 200, json=payload)

    async def candidate_flow(ctx, _args):
        return await ctx.agent("Record the measurement.", tools=[], schema={
            "type": "object", "properties": {"measurement": {"type": "integer"}},
            "required": ["measurement"],
        })

    async def flow(ctx, args):
        candidate = asyncio.create_task(ctx.candidate_workflow(candidate_flow, args, label="measure", budget=10000))
        sibling = None
        try:
            await asyncio.wait_for(correcting.wait(), 5)
            sibling = asyncio.create_task(ctx.agent_run("read sibling", tools=[], budget=5000))
            candidate.cancel()
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not candidate.done()
            assert not sibling_started.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await candidate
            result = await sibling
            return result.output
        finally:
            release.set()
            tasks = [candidate] if sibling is None else [candidate, sibling]
            await asyncio.gather(*tasks, return_exceptions=True)

    install_sdk_transport(monkeypatch, "openai", handler)
    async with LLMClient(
        model="gpt-4o", api_key=tmp_path.name, base_url="https://offline.invalid/v1", max_retries=0,
    ) as llm:
        result = await OpenCollab(repo, model="gpt-4o", config={"max_output_tokens": 128}).workflow(
            flow, llm=llm, artifacts=artifacts, trace=True, budget=20000, concurrency=1,
            timeout=25, limit_mode="explicit",
        )

    assert result.status == "completed", result.reason
    assert result.output == "sibling answer"
    assert result.metrics["execution_quiesced"] is True
    assert _git(repo, "worktree", "list", "--porcelain") == worktrees
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["evidence_complete"] is True
    assert result.tokens == manifest["tokens_spent"] == 34
    provider_records = [json.loads(line) for line in usage_log.read_text().splitlines()]
    assert [row["usage"]["total_tokens"] for row in provider_records] == [8, 19, 7]

    trace = [json.loads(line) for line in (artifacts / "orchestration.jsonl").read_text().splitlines()]
    completions = [row for row in trace if row["type"] == "llm_call"]
    assert [row["metrics"]["tokens"] for row in completions] == [8, 19, 7]
    late, = (row for row in completions if row["payload"].get("late"))
    stopped, = (row for row in trace if row["type"] == "llm_call_cancelled")
    for key in ("response_session_id", "session_step"):
        assert late["payload"][key] == stopped["payload"][key]
    assert stopped["step"] < late["step"]
    assert late["payload"]["usage"]["total_tokens"] == 19
    assert late["payload"]["request_tool_names"] == ["structured_output"]
    assert all(row["type"] != "tool_exec" for row in trace)

    correction_path, = artifacts.glob("001*.json")
    correction = SessionStore().load_snapshot(str(correction_path), "")
    assert correction["session_state"]["used_tokens"] == 19
    assert correction["session_state"]["phase"] == "stopped"
    assert all(not row.get("tool_calls") for row in correction["messages"])
