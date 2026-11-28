from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.application.workflow import WorkflowBudgetExceeded
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport


@pytest.mark.parametrize("scene", ["normal", "during-close", "during-body", "return-on-cancel", "budget-stop"])
async def test_workflow_deadline_keeps_only_output_returned_before_timeout(tmp_path, monkeypatch, scene):
    closing = asyncio.Event()
    closed = asyncio.Event()
    release = asyncio.Event()
    body_returns = []
    original_close = LLMClient.close
    timeout = 1.0

    async def close(client):
        closing.set()
        await release.wait()
        await original_close(client)
        closed.set()

    async def measure(ctx, _inputs):
        answer = await ctx.agent("Measure the subtotal.", tools=[])
        assert answer == "Verified subtotal"
        if scene == "budget-stop":
            raise WorkflowBudgetExceeded("No budget for another measurement.")
        if scene in {"during-body", "return-on-cancel"}:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if scene == "during-body":
                    raise
                answer = "Only returned on cancellation"
        body_returns.append(answer)
        return {"measurement": answer}

    monkeypatch.setattr(LLMClient, "close", close)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    install_sdk_transport(monkeypatch, "openai", lambda request: completion_http_response(
        request, text="Verified subtotal", output_tokens=5,
    ))
    client = OpenCollab(
        tmp_path, model="offline-checker", provider="openai", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1", config={"wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    task = asyncio.create_task(client.workflow(
        measure, artifacts=artifacts, trace=False, budget=80_000, timeout=timeout, cleanup_timeout=2,
    ))
    try:
        if scene in {"normal", "during-close", "budget-stop"}:
            await asyncio.wait_for(closing.wait(), timeout=2)
            assert body_returns == ([] if scene == "budget-stop" else ["Verified subtotal"])
        if scene in {"during-close", "budget-stop"}:
            await asyncio.sleep(timeout + 0.05)
        release.set()
        result = await asyncio.wait_for(task, timeout=3)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert closed.is_set()
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["evidence_complete"] is True
    assert result.tokens == manifest["tokens_spent"] == 7
    assert result.status == ("completed" if scene == "normal" else "stopped")
    assert result.reason == (None if scene == "normal" else "timeout")
    if scene in {"normal", "during-close"}:
        assert result.output == {"measurement": "Verified subtotal"}
    else:
        assert body_returns == (["Only returned on cancellation"] if scene == "return-on-cancel" else [])
        assert result.output is None
