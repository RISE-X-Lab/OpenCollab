"""Preserve an accepted structured result across its terminal checkpoint deadline."""

from __future__ import annotations

import json
import threading

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.application.session import Session
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("capture_stage", ["initial", "correction", "neither"])
async def test_validated_capture_survives_the_caller_deadline_during_checkpoint(tmp_path, monkeypatch, capture_stage):
    requests = []
    checkpoint_entered = threading.Event()
    release_checkpoint = threading.Event()
    original_save = Session.save
    expected = {"measurement": 42}
    held_stage = "correction" if capture_stage == "neither" else capture_stage

    def stage(messages):
        user_texts = [row.get("content") for row in messages if row.get("role") == "user"]
        if any(isinstance(text, str) and "You MUST call" in text for text in user_texts):
            return "correction"
        return "initial"

    def save_after_release(session, target):
        if not checkpoint_entered.is_set() and stage(session.messages) == held_stage:
            if capture_stage != "neither":
                assert any(row.get("tool_call_id") == "verified-meter" for row in session.messages)
            checkpoint_entered.set()
            assert release_checkpoint.wait(10)
        return original_save(session, target)

    async def handler(request):
        action = stage(json.loads(request.content)["messages"])
        requests.append(action)
        if action != capture_stage:
            response = completion_http_response(request, text="The observed measurement was 42 milliseconds.").json()
        else:
            response = completion_http_response(request, text="").json()
            choice = response["choices"][0]
            choice["finish_reason"] = "tool_calls"
            choice["message"] = {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "verified-meter", "type": "function",
                    "function": {"name": "structured_output", "arguments": json.dumps(expected)},
                }],
            }
        response["usage"] = {"prompt_tokens": 801, "completion_tokens": 19, "total_tokens": 820}
        return http_response(request, 200, json=response)

    schema = {"type": "object", "properties": {"measurement": {"type": "integer"}}, "required": ["measurement"]}
    monkeypatch.setattr(Session, "save", save_after_release)
    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")

    async def workflow(ctx, _inputs):
        try:
            result = await ctx.agent("Record the measurement.", schema=schema, tools=[], timeout=2)
            assert checkpoint_entered.is_set()
            return result
        finally:
            release_checkpoint.set()

    artifacts = tmp_path / "artifacts"
    async with LLMClient(
        model="gpt-4o", api_key=tmp_path.name, base_url="https://offline.invalid/v1", max_retries=0,
    ) as model:
        result = await OpenCollab(tmp_path, model="gpt-4o", config={"max_output_tokens": 128}).workflow(
            workflow, llm=model, budget=20_000, concurrency=1, limit_mode="explicit",
            artifacts=artifacts, trace=True, timeout=15,
        )
    assert result.status == "completed", result.reason
    assert requests == (["initial"] if capture_stage == "initial" else ["initial", "correction"])
    assert result.tokens == 820 * len(requests)
    assert result.output == (None if capture_stage == "neither" else expected)
    manifest = json.loads((artifacts / "workflow.json").read_text())
    assert manifest["tokens_spent"] == result.tokens
    assert manifest["evidence_complete"] is True
