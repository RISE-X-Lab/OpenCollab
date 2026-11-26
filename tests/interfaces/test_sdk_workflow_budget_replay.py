from __future__ import annotations

import asyncio
import json
from functools import wraps

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.responses_provider_test_support import message_item

REASONING = "I will inspect the import and export paths, then design a test."


def _prompt(messages):
    content = next(message["content"] for message in reversed(messages) if message["role"] == "user")
    return content if isinstance(content, str) else "".join(part["text"] for part in content)


def _completion(request, body, protocol, content, reasoning, finish_reason, inputs, outputs):
    total = inputs + outputs
    if protocol == "anthropic":
        blocks = [{"type": "text", "text": content}] if content else []
        if reasoning:
            blocks.append({"type": "thinking", "thinking": reasoning, "signature": "fixture-signature"})
        return http_response(request, 200, json={
            "id": "msg_replay", "type": "message", "role": "assistant", "model": body["model"], "content": blocks,
            "stop_reason": {"length": "max_tokens", "stop": "end_turn"}.get(finish_reason, finish_reason),
            "stop_sequence": None,
            "usage": {"input_tokens": inputs, "output_tokens": outputs},
        })
    if protocol == "responses":
        assert body["stream"]
        items = [message_item(content)] if content else []
        if reasoning:
            items.append({"type": "reasoning", "id": "rs_replay", "summary": [
                {"type": "summary_text", "text": reasoning},
            ]})
        status = "incomplete" if finish_reason == "length" else "completed"
        terminal = {
            "id": "resp_replay", "object": "response", "created_at": 1, "model": body["model"],
            "instructions": body.get("instructions"), "status": status, "output": items, "error": None,
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
            "usage": {"input_tokens": inputs, "output_tokens": outputs, "total_tokens": total},
        }
        events = [{"type": "response.output_item.done", "output_index": index, "item": item}
                  for index, item in enumerate(items)]
        events.append({"type": f"response.{status}", "response": terminal})
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=payload)
    usage = {"prompt_tokens": inputs, "completion_tokens": outputs, "total_tokens": total}
    if body.get("stream"):
        fields = {"id": "chat_replay", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
        delta = {"role": "assistant", "content": content}
        if reasoning:
            delta["reasoning_content"] = reasoning
        events = [
            {**fields, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**fields, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
            {**fields, "choices": [], "usage": usage},
        ]
        payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=payload)
    response = completion_http_response(request, text=content, output_tokens=outputs).json()
    response["choices"][0]["finish_reason"] = finish_reason
    response["usage"] = usage
    return http_response(request, 200, json=response)


async def _wait(event):
    await asyncio.wait_for(event.wait(), 5)


@pytest.mark.parametrize(
    "protocol,outcome,sibling_tokens,review_limits,status,output,usage",
    [
        ("chat", "complete", 100, [10, 128], "completed", "Review complete", [6000, 6014]),
        ("chat", "shortfall", 14950, [10], "stopped", "Useful partial", [6000]),
        ("chat", "exhausted", 15050, [10], "stopped", "Useful partial", [6000]),
        ("chat", "busy", 100, [10, 128], "stopped", "Useful partial", [6000]),
        ("chat", "context", 100, [10, 128], "stopped", "Useful partial", [6000]),
        ("chat", "empty", 100, [10, 128], "stopped", "Useful partial", [6000, 6118]),
        ("chat", "invalid", 100, [10, 128], "failed", None, [6000]),
        ("chat", "expired", 100, [10, 128], "failed", None, [6000]),
        ("chat", "filtered", 100, [10, 128], "stopped", "", [6000, 5994]),
        ("chat_sse", "reasoning", 100, [32, 128], "stopped", "Useful partial", [6000, 6096]),
        ("responses", "reasoning", 100, [32, 128], "stopped", "Useful partial", [6000, 6096]),
        ("anthropic", "reasoning", 100, [32, 128], "stopped", "Useful partial", [6000, 6096]),
        ("anthropic", "decoded_context", 100, [32, 128], "stopped", "Useful partial", [6000, 5980]),
        ("anthropic", "decoded_context_extension", 100, [32, 128], "stopped", "Useful partial with counts",
         [6000, 5980]),
        ("anthropic", "decoded_context_after_empty", 100, [32, 128], "stopped", "Review findings", [6000, 5980]),
        ("anthropic", "decoded_context_after_reasoning", 100, [32, 128], "stopped", "Review findings", [6000, 5980]),
        ("anthropic", "clipped_prefix", 100, [32, 128], "stopped", "Useful partial", [6000, 6096]),
        ("anthropic", "completed_prefix", 100, [32, 128], "completed", "Useful", [6000, 5992]),
        ("chat_sse", "reasoning_complete", 100, [32, 128], "stopped", "Useful partial", [6000, 5992]),
        ("chat_sse", "partial", 100, [32, 128], "stopped", "More useful partial", [6000, 6096]),
    ],
)
async def test_budget_replay_preserves_first_partial_when_it_cannot_improve(
    tmp_path, monkeypatch, protocol, outcome, sibling_tokens, review_limits, status, output, usage,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(ledger))
    preparing, finish_prepare, collection_entered, start_review = (asyncio.Event() for _ in range(4))
    reviewing, sibling_admitted, finish_sibling, replay_pending, admit_replay = (asyncio.Event() for _ in range(5))
    events, review_payloads, assistant_history = [], [], []
    model = {"chat_sse": "deepseek-reasoner", "responses": "o3", "anthropic": "claude-opus-5-5"}.get(
        protocol, "gpt-4o",
    )
    provider = "anthropic" if protocol == "anthropic" else "openai"
    wire = "responses" if protocol == "responses" else "chat_completions"
    review_inputs = 5990 if protocol == "chat" else 5968

    class Client(LLMClient):
        def estimate_context_tokens(self, messages, **kwargs):
            return 10 * len(messages)

        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return {"prepare": 2990, "review": review_inputs, "sibling": 90}[_prompt(messages)]

        @wraps(LLMClient.complete)
        async def complete(self, messages, **kwargs):
            if _prompt(messages) == "review" and review_payloads:
                replay_pending.set()
                await _wait(admit_replay)
            return await super().complete(messages, **kwargs)

    async def handler(request):
        body = json.loads(request.content)
        label = _prompt(body.get("messages", body.get("input")))
        limit = body.get("max_tokens", body.get("max_output_tokens"))
        finish_reason = "stop"
        reasoning = None
        if label == "prepare":
            preparing.set()
            await _wait(finish_prepare)
            content, inputs, outputs = "prepared", 2990, 10
        elif label == "sibling":
            sibling_admitted.set()
            await _wait(finish_sibling)
            content, inputs, outputs = "sibling finished", sibling_tokens - 10, 10
        else:
            review_payloads.append(body)
            if len(review_payloads) == 1:
                reviewing.set()
                await _wait(sibling_admitted)
                finish_reason = "length"
                content, inputs, outputs = "Useful partial", review_inputs, limit
                if outcome in {"decoded_context_after_empty", "decoded_context_after_reasoning"}:
                    content = None
                if outcome == "decoded_context_after_reasoning":
                    reasoning = REASONING
            elif outcome in {"busy", "invalid"}:
                code = 503 if outcome == "busy" else 400
                return http_response(request, code, json={"error": {"message": f"Offline replay {outcome}"}})
            elif outcome == "expired":
                return http_response(request, 401, json={
                    "error": {"message": "Authentication session expired after its idle timeout."},
                })
            elif outcome == "context":
                return http_response(request, 400, json={
                    "error": {"message": "Maximum context length exceeded", "code": "context_length_exceeded"},
                })
            elif outcome.startswith("decoded_context"):
                finish_reason = "model_context_window_exceeded"
                content = {
                    "decoded_context": "Review introduction",
                    "decoded_context_extension": "Useful partial with counts",
                }.get(outcome, "Review findings")
                inputs, outputs = review_inputs, 12
            elif outcome in {"clipped_prefix", "completed_prefix"}:
                is_clipped = outcome == "clipped_prefix"
                finish_reason = "length" if is_clipped else "stop"
                content, inputs, outputs = "Useful", review_inputs, limit if is_clipped else 24
            elif outcome == "empty" or limit < 128:
                finish_reason = "length"
                content, inputs, outputs = "", 5990, limit
            elif outcome == "filtered":
                finish_reason = "content_filter"
                content, inputs, outputs = "", 5990, 4
            elif outcome.startswith("reasoning"):
                finish_reason = "length" if outcome == "reasoning" else "stop"
                content, inputs, outputs = None, review_inputs, limit if outcome == "reasoning" else 24
                reasoning = REASONING
            elif outcome == "partial":
                finish_reason = "length"
                content, inputs, outputs = "More useful partial", review_inputs, limit
                reasoning = content
            else:
                content, inputs, outputs = "Review complete", 5990, 24
        return _completion(request, body, protocol, content, reasoning, finish_reason, inputs, outputs)

    async def workflow(ctx, _inputs):
        async def review():
            collection_entered.set()
            await _wait(start_review)
            return await ctx.agent_run("review", tools=[], run_control=RunControl(on_event=events.append))

        async def record():
            return "recorded"

        preparation = asyncio.create_task(ctx.agent_run("prepare", budget=18000, tools=[]))
        group = sibling = None
        try:
            await _wait(preparing)
            group = asyncio.create_task(ctx.parallel([review, record]))
            await _wait(collection_entered)
            finish_prepare.set()
            assert (await preparation).output == "prepared"
            start_review.set()
            await _wait(reviewing)
            sibling = asyncio.create_task(ctx.agent_run("sibling", budget=10000, tools=[]))
            await _wait(replay_pending)
            finish_sibling.set()
            await sibling
            admit_replay.set()
            result, note = await group
            assert note == "recorded"
            assert ctx.tokens_remaining() == 30000 - ctx.budget.spent()
            review_session = ctx.budget._sessions[1]
            assistant_history.extend(
                message["content"] for message in review_session.messages if message["role"] == "assistant"
            )
            return result
        finally:
            for event in (finish_prepare, start_review, sibling_admitted, finish_sibling, admit_replay):
                event.set()
            await asyncio.gather(*(task for task in (preparation, group, sibling) if task), return_exceptions=True)

    install_sdk_transport(monkeypatch, provider, handler)
    pool = tmp_path / "pool"
    pool.mkdir()
    async with Client(
        model=model, provider=provider, wire_protocol=wire, api_key=tmp_path.name,
        base_url="https://offline.invalid/v1", request_pool=str(pool), request_capacity=2, max_retries=0,
        stream_chat=protocol == "chat_sse",
    ) as client:
        config = {"max_output_tokens": 128, "wire_protocol": wire}
        if protocol == "anthropic":
            config.update(thinking=True, thinking_params={"thinking": {"type": "adaptive"}})
        result = await OpenCollab(tmp_path, model=model, provider=provider, config=config).workflow(
            workflow, llm=client, budget=30000, concurrency=3, task_concurrency=2, limit_mode="explicit",
            artifacts=tmp_path / "artifacts", trace=True, timeout=15,
        )

    assert result.status == "completed", result.reason
    row = result.output
    assert (row.status, row.output, row.steps) == (status, output, 1)
    assert row.tokens == sum(usage)
    assert result.tokens == 3000 + sibling_tokens + row.tokens
    trace_path = tmp_path / "artifacts" / "orchestration.jsonl"
    rows = [json.loads(line) for line in trace_path.read_text().splitlines()]
    completions = [row for row in rows if row["type"] == "llm_call"]
    assert sum(row["metrics"]["tokens"] for row in completions) == result.tokens
    assert [event.data["total_tokens"] for event in events if event.type == "usage"] == usage
    assert [body.get("max_tokens", body.get("max_output_tokens")) for body in review_payloads] == review_limits
    if len(review_limits) > 1:
        input_key = "input" if wire == "responses" else "messages"
        assert review_payloads[0][input_key] == review_payloads[1][input_key]
    if outcome.startswith(("reasoning", "decoded_context")) or outcome in {"clipped_prefix", "completed_prefix"}:
        assert assistant_history == [output]
        ledger_usage = [json.loads(line)["usage"]["total_tokens"] for line in ledger.read_text().splitlines()]
        assert sorted(ledger_usage) == sorted([3000, sibling_tokens, *usage])
    if outcome.startswith("reasoning"):
        discarded = [row for row in completions if row["payload"].get("reasoning") == REASONING]
        assert len(discarded) == 1
        assert discarded[0]["payload"]["purpose"] == "budget_recovery"
    if outcome in {"decoded_context", "clipped_prefix"}:
        [discarded] = [row for row in completions if row["payload"].get("purpose") == "budget_recovery"]
        assert (discarded["payload"]["finish_reason"], discarded["payload"]["content"]) == {
            "decoded_context": ("model_context_window_exceeded", "Review introduction"),
            "clipped_prefix": ("max_tokens", "Useful"),
        }[outcome]
    if outcome == "filtered":
        assert row.reason == "provider filtered the output"
    if outcome == "expired":
        assert "AuthenticationError" in row.reason
