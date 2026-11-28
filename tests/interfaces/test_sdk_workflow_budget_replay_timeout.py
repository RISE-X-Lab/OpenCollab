from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("scene", ["role_timeout", "reasoning_timeout", "workflow_cancel", "completed_replay"])
async def test_optional_budget_replay_at_a_workflow_role_deadline(tmp_path, monkeypatch, scene):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    ledger_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(ledger_path))
    preparation_started, release_preparation, preparation_finished = (asyncio.Event() for _ in range(3))
    replay_started, replay_cancelled = asyncio.Event(), asyncio.Event()
    requests = []
    calls = []
    partial = "Checked items 1 through 42."
    complete = "Checked all 87 items. Inventory reconciled."
    hidden = "I should check the remaining rows."
    prepare_prompt = "Load the local inventory."
    review_prompt = "Review the inventory."

    def prompt_for(messages):
        return next((row["content"] for row in reversed(messages) if row["role"] == "user"), "")

    def estimate(_client, messages, tools=None, **kwargs):
        return {prepare_prompt: 2990, review_prompt: 5990}.get(prompt_for(messages), 100)

    async def reply(request):
        payload = json.loads(request.content)
        prompt = prompt_for(payload["messages"])
        requests.append((prompt, payload["max_tokens"]))
        if prompt == prepare_prompt:
            preparation_started.set()
            await release_preparation.wait()
            answer, input_tokens, output_tokens, finish = "Inventory loaded.", 2990, 10, "stop"
        else:
            assert prompt == review_prompt
            calls.append(payload["messages"])
            if len(calls) == 1:
                release_preparation.set()
                await preparation_finished.wait()
                answer, input_tokens, output_tokens, finish = partial, 5990, 10, "length"
            else:
                replay_started.set()
                if scene != "completed_replay":
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        replay_cancelled.set()
                        raise
                answer, input_tokens, output_tokens, finish = complete, 5990, 24, "stop"
        response = completion_http_response(request, text=answer, output_tokens=output_tokens).json()
        choice = response["choices"][0]
        choice["finish_reason"] = finish
        if scene == "reasoning_timeout" and finish == "length":
            choice["message"].update(content=None, reasoning_content=hidden)
        response["usage"].update(prompt_tokens=input_tokens, total_tokens=input_tokens + output_tokens)
        return http_response(request, 200, json=response)

    monkeypatch.setattr(LLMClient, "estimate_request_tokens", estimate)
    install_sdk_transport(monkeypatch, "openai", reply)

    async def flow(ctx, _inputs):
        async def prepare():
            result = await ctx.agent_run(prepare_prompt, tools=[], budget=33_000)
            assert result.output == "Inventory loaded."
            preparation_finished.set()

        async def review():
            return await ctx.agent_run(review_prompt, label="review", tools=[], timeout=2)

        async def record():
            return "Inventory date recorded."

        pending = asyncio.create_task(prepare())
        try:
            await asyncio.wait_for(preparation_started.wait(), 5)
            result, note = await ctx.parallel([review, record])
            assert note == "Inventory date recorded."
            return result
        finally:
            release_preparation.set()
            await pending

    artifacts = tmp_path / "artifacts"
    client = OpenCollab(
        tmp_path, model="gpt-4o", provider="openai", api_key=tmp_path.name, base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": False,
                "llm_max_retries": 0, "max_output_tokens": 128},
    )
    cancellations = []

    async def run():
        try:
            return await client.workflow(
                flow, budget=45_000, concurrency=2, task_concurrency=2, limit_mode="explicit",
                artifacts=artifacts, trace=True, timeout=12, cleanup_timeout=2,
            )
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    operation = asyncio.create_task(run())
    if scene == "workflow_cancel":
        try:
            await asyncio.wait_for(replay_started.wait(), 5)
            operation.cancel("caller cancelled the workflow")
            with pytest.raises(asyncio.CancelledError):
                await operation
            [cancelled] = cancellations
            assert str(cancelled) == "caller cancelled the workflow"
            details = cancelled.runtime_result
            assert details.output is None and details.tokens == 9000
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
    else:
        result = await operation
        assert result.ok and result.metrics["execution_quiesced"]
        row = result.output
        assert row.cleanup_complete and row.workspace_ready
        if scene == "completed_replay":
            assert (row.status, row.reason, row.output, row.tokens) == ("completed", None, complete, 12_014)
        else:
            expected = partial if scene == "role_timeout" else None
            assert (row.status, row.reason, row.output, row.tokens) == ("stopped", "timeout", expected, 6000)

    assert requests == [(prepare_prompt, 128), (review_prompt, 10), (review_prompt, 128)]
    assert calls[0] == calls[1]
    assert replay_cancelled.is_set() is (scene != "completed_replay")
    manifest = json.loads((artifacts / "workflow.json").read_text())
    ledger = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    tokens = 15_014 if scene == "completed_replay" else 9000
    assert manifest["tokens_spent"] == sum(row["usage"]["total_tokens"] for row in ledger) == tokens
    assert manifest["evidence_complete"]
    assert manifest["status"] == ("stopped" if scene == "workflow_cancel" else "completed")
    if scene != "workflow_cancel":
        assert result.tokens == tokens and not result.agent_failures

    transcript, = artifacts.glob("*_review.json")
    snapshot = client.read_session_snapshot(transcript)
    assistant = [row["content"] for row in snapshot["messages"] if row["role"] == "assistant"]
    assert assistant == ([complete] if scene == "completed_replay" else [])
