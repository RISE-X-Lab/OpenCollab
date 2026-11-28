from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.worktree_delivery_support import make_repo

PREPARE = "Load the local inventory."
REVIEW = "Review the inventory."
PARTIAL = "Checked items 1 through 42."
COMPLETE = "Checked all 87 items. Inventory reconciled."


@pytest.mark.parametrize(("entry", "scene"), [
    ("agent", "replay_timeout"),
    ("agent", "replay_complete"),
    ("agent", "single_complete"),
    ("agent", "initial_timeout"),
    ("agent", "initial_cancel"),
    ("agent", "workflow_cancel"),
    ("enforced", "replay_timeout"),
    ("candidate", "replay_timeout"),
])
async def test_workflow_agent_retains_settled_text_when_budget_replay_times_out(tmp_path, monkeypatch, entry, scene):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    ledger_path = tmp_path / "provider.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(ledger_path))
    prep_started, prep_release, prep_finished = (asyncio.Event() for _ in range(3))
    initial_started, replay_started, request_cancelled = (asyncio.Event() for _ in range(3))
    requests = []
    review_messages = []

    def prompt_for(messages):
        return next((row["content"] for row in reversed(messages) if row["role"] == "user"), "")

    def estimate(_client, messages, tools=None, **kwargs):
        return {PREPARE: 2990, REVIEW: 5990}.get(prompt_for(messages), 100)

    async def handler(request):
        body = json.loads(request.content)
        prompt = prompt_for(body["messages"])
        limit = body["max_tokens"]
        requests.append((prompt, limit))
        finish = "stop"
        if prompt == PREPARE:
            prep_started.set()
            await prep_release.wait()
            text, inputs, outputs = "Inventory loaded.", 2990, 10
        else:
            assert prompt == REVIEW
            review_messages.append(body["messages"])
            if len(review_messages) == 1:
                prep_release.set()
                await prep_finished.wait()
                if scene in {"initial_timeout", "initial_cancel"}:
                    initial_started.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        request_cancelled.set()
                        raise
                text = COMPLETE if scene == "single_complete" else PARTIAL
                finish = "stop" if scene == "single_complete" else "length"
                inputs, outputs = 5990, limit
            else:
                replay_started.set()
                if scene in {"replay_timeout", "workflow_cancel"}:
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        request_cancelled.set()
                        raise
                text, inputs, outputs = COMPLETE, 5990, 24
        response = completion_http_response(request, text=text, output_tokens=outputs).json()
        response["choices"][0]["finish_reason"] = finish
        response["usage"].update(prompt_tokens=inputs, total_tokens=inputs + outputs)
        return http_response(request, 200, json=response)

    monkeypatch.setattr(LLMClient, "estimate_request_tokens", estimate)
    install_sdk_transport(monkeypatch, "openai", handler)

    async def workflow(ctx, _inputs):
        async def prepare():
            result = await ctx.agent(PREPARE, tools=[], budget=33_000)
            assert result == "Inventory loaded."
            prep_finished.set()

        async def review():
            if entry == "candidate":
                candidate = await ctx.candidate_agent(REVIEW, label="review", tools=[], timeout=2)
                assert not candidate.diff
                return candidate.output
            if entry == "enforced":
                return await ctx.agent(
                    REVIEW, label="review", tools=[], timeout=2,
                    enforcement_strength="needs-enforcement", commit_reserve=100,
                )
            return await ctx.agent(REVIEW, label="review", tools=[], timeout=2)

        async def record():
            return "Inventory date recorded."

        pending = asyncio.create_task(prepare())
        try:
            await asyncio.wait_for(prep_started.wait(), 5)
            result, note = await ctx.parallel([review, record])
            assert note == "Inventory date recorded."
            return result
        finally:
            prep_release.set()
            await pending

    artifacts = tmp_path / "artifacts"
    workspace = make_repo(tmp_path / "workspace") if entry == "candidate" else tmp_path
    client = OpenCollab(
        workspace, model="gpt-4o", provider="openai", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": False,
                "llm_max_retries": 0, "max_output_tokens": 128},
    )
    operation = asyncio.create_task(client.workflow(
        workflow, budget=45_000, concurrency=2, task_concurrency=2, limit_mode="explicit",
        artifacts=artifacts, trace=True, timeout=12, cleanup_timeout=2,
    ))
    cancelled = scene in {"initial_cancel", "workflow_cancel"}
    if cancelled:
        try:
            event = initial_started if scene == "initial_cancel" else replay_started
            await asyncio.wait_for(event.wait(), 5)
            operation.cancel("caller cancelled inventory workflow")
            with pytest.raises(asyncio.CancelledError, match="caller cancelled inventory workflow") as error:
                await operation
            result = error.value.runtime_result
            assert result.evidence_complete and not result.environment_cleanup_deferred
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
    else:
        result = await operation
        assert result.ok and result.metrics["execution_quiesced"]
    ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    billed_tokens = [row["usage"]["total_tokens"] for row in ledger if row.get("usage")]
    manifest = json.loads((artifacts / "workflow.json").read_text())
    limits, expected_tokens, expected_output = {
        "replay_timeout": ([10, 128], 9000, PARTIAL),
        "replay_complete": ([10, 128], 15_014, COMPLETE),
        "single_complete": ([10], 9000, COMPLETE),
        "initial_timeout": ([10], 3000, None),
        "initial_cancel": ([10], 3000, None),
        "workflow_cancel": ([10, 128], 9000, None),
    }[scene]
    assert requests == [(PREPARE, 128), *((REVIEW, limit) for limit in limits)]
    assert manifest["status"] == ("stopped" if cancelled else "completed")
    assert result.tokens == manifest["tokens_spent"] == sum(billed_tokens) == expected_tokens
    assert request_cancelled.is_set() is (cancelled or scene in {"initial_timeout", "replay_timeout"})
    if len(review_messages) == 2:
        assert review_messages[0] == review_messages[1]
    assert result.output == expected_output
