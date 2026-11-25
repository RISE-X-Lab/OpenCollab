from __future__ import annotations

import asyncio
import json
from functools import wraps

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.llm.client import LLMClient
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _prompt(messages):
    return next(message["content"] for message in reversed(messages) if message["role"] == "user")


async def _wait(event):
    await asyncio.wait_for(event.wait(), 5)


@pytest.mark.parametrize(
    "outcome,sibling_tokens,review_limits,status,output,usage",
    [
        ("complete", 100, [10, 128], "completed", "Review complete", [6000, 6014]),
        ("shortfall", 14950, [10], "stopped", "Useful partial", [6000]),
        ("exhausted", 15050, [10], "stopped", "Useful partial", [6000]),
        ("busy", 100, [10, 128], "stopped", "Useful partial", [6000]),
        ("context", 100, [10, 128], "stopped", "Useful partial", [6000]),
        ("empty", 100, [10, 128], "stopped", "Useful partial", [6000, 6118]),
        ("invalid", 100, [10, 128], "failed", None, [6000]),
        ("expired", 100, [10, 128], "failed", None, [6000]),
        ("filtered", 100, [10, 128], "stopped", "", [6000, 5994]),
    ],
)
async def test_budget_replay_preserves_first_partial_when_it_cannot_improve(
    tmp_path, monkeypatch, outcome, sibling_tokens, review_limits, status, output, usage,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    preparing, finish_prepare, collection_entered, start_review = (asyncio.Event() for _ in range(4))
    reviewing, sibling_admitted, finish_sibling, replay_pending, admit_replay = (asyncio.Event() for _ in range(5))
    events, review_payloads = [], []

    class Client(LLMClient):
        def estimate_context_tokens(self, messages, **kwargs):
            return 10 * len(messages)

        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            return {"prepare": 2990, "review": 5990, "sibling": 90}[_prompt(messages)]

        @wraps(LLMClient.complete)
        async def complete(self, messages, **kwargs):
            if _prompt(messages) == "review" and review_payloads:
                replay_pending.set()
                await _wait(admit_replay)
            return await super().complete(messages, **kwargs)

    async def handler(request):
        body = json.loads(request.content)
        label = _prompt(body["messages"])
        limit = body["max_tokens"]
        finish_reason = "stop"
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
                content, inputs, outputs = "Useful partial", 5990, limit
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
            elif outcome == "empty" or limit < 128:
                finish_reason = "length"
                content, inputs, outputs = "", 5990, limit
            elif outcome == "filtered":
                finish_reason = "content_filter"
                content, inputs, outputs = "", 5990, 4
            else:
                content, inputs, outputs = "Review complete", 5990, 24
        response = completion_http_response(request, text=content, output_tokens=outputs).json()
        response["choices"][0]["finish_reason"] = finish_reason
        response["usage"] = {"prompt_tokens": inputs, "completion_tokens": outputs, "total_tokens": inputs + outputs}
        return http_response(request, 200, json=response)

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
            return result
        finally:
            for event in (finish_prepare, start_review, sibling_admitted, finish_sibling, admit_replay):
                event.set()
            await asyncio.gather(*(task for task in (preparation, group, sibling) if task), return_exceptions=True)

    install_sdk_transport(monkeypatch, "openai", handler)
    pool = tmp_path / "pool"
    pool.mkdir()
    async with Client(
        model="gpt-4o", api_key=tmp_path.name, base_url="https://offline.invalid/v1",
        request_pool=str(pool), request_capacity=2, max_retries=0,
    ) as client:
        result = await OpenCollab(tmp_path, model="gpt-4o", config={"max_output_tokens": 128}).workflow(
            workflow, llm=client, budget=30000, concurrency=3, task_concurrency=2, limit_mode="explicit",
            trace=False, timeout=15,
        )

    assert result.status == "completed", result.reason
    row = result.output
    assert (row.status, row.output, row.steps) == (status, output, 1)
    assert row.tokens == sum(usage)
    assert result.tokens == 3000 + sibling_tokens + row.tokens
    assert [event.data["total_tokens"] for event in events if event.type == "usage"] == usage
    assert [body["max_tokens"] for body in review_payloads] == review_limits
    if len(review_limits) > 1:
        assert review_payloads[0]["messages"] == review_payloads[1]["messages"]
    if outcome == "filtered":
        assert row.reason == "provider filtered the output"
    if outcome == "expired":
        assert "AuthenticationError" in row.reason
