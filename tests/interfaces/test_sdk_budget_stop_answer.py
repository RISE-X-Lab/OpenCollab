"""A billed Responses retry preserves usable text when its budget stops the run."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from opencollab import BudgetDecision, OpenCollab, RunControl
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.base import Tool
from opencollab.bootstrap import agent_runtime
from tests.support.provider_sdk_http import http_response, install_sdk_transport
from tests.support.responses_provider_test_support import function_item, message_item

ANSWER = "The measured total is 42. The calculation is complete."
REQUEST_INPUT_ESTIMATE = 500
INITIAL_BUDGET = 1075
PROMPT = "Read these notes and return the measured total.\n" + (
    "The measured count was recorded as 42 and checked by the operator.\n" * 30
)

class MarkerTool(Tool):
    name = "touch_marker"
    description = "Record a local marker."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self):
        self.calls = 0

    async def execute_with_runtime(self, params, runtime):
        self.calls += 1
        return "recorded"

@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("case", [
    "soft_text", "soft_text_tool", "soft_tool", "growth",
    "hard_text", "hard_text_tool", "hard_tool", "single", "truncated",
])
async def test_budget_stop_preserves_answer_after_native_retry(tmp_path, monkeypatch, trace, case):
    requests, runtime_events, events, snapshots, sessions = [], [], [], [], []
    tool = MarkerTool()
    retry = case not in {"single", "truncated"}
    with_tool = case.endswith("_tool")
    has_text = case not in {"soft_tool", "hard_tool"}
    hard_budget = INITIAL_BUDGET if case.startswith("hard_") else 3000
    expected_tokens = 1115 if retry else 565
    stopped_for_budget = case.startswith(("soft_", "hard_"))
    original = agent_runtime.build_session

    def observed_session(**kwargs):
        session = original(**kwargs)
        session.event_bus.subscribe(runtime_events.append)
        sessions.append(session)
        return session

    def decide(snapshot):
        snapshots.append(snapshot)
        allowance = 2000 if case == "growth" and snapshot.reason == "after_response" else snapshot.soft_budget_tokens
        return BudgetDecision(allowance)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        empty = retry and len(requests) == 1
        items = [] if empty or not has_text else [message_item(ANSWER)]
        if with_tool and not empty:
            items.append(function_item("call_unused", tool.name, "{}"))
        output_tokens = 0 if empty else 15
        truncated = case == "truncated"
        response = {
            "id": f"resp_{len(requests)}", "object": "response", "created_at": 1, "model": body["model"],
            "instructions": body.get("instructions"), "status": "incomplete" if truncated else "completed",
            "error": None, "incomplete_details": {"reason": "max_output_tokens"} if truncated else None,
            "output": items,
            "usage": {
                "input_tokens": 550, "output_tokens": output_tokens, "total_tokens": 550 + output_tokens,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        stream_events = [
            {"type": "response.output_item.done", "output_index": index, "item": item}
            for index, item in enumerate(items)
        ]
        stream_events.append({
            "type": "response.incomplete" if truncated else "response.completed", "response": response,
        })
        stream = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in stream_events)
        return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=stream)

    usage_log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_log))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    monkeypatch.setattr(agent_runtime, "build_session", observed_session)
    monkeypatch.setattr(LLMClient, "estimate_request_tokens", lambda *_args, **_kwargs: REQUEST_INPUT_ESTIMATE)
    install_sdk_transport(monkeypatch, "openai", handler)
    artifacts = tmp_path / "artifacts"
    result = await OpenCollab(
        tmp_path, provider="openai", model="gpt-4o",
        api_key="controlled-test", base_url="https://controlled.invalid/v1",  # pragma: allowlist secret
        config={"wire_protocol": "responses", "llm_max_retries": 1, "max_output_tokens": 128},
    ).agent2(
        PROMPT, tools=(tool,) if with_tool else (), system_prompt="Answer the user using the supplied notes.",
        budget=hard_budget, max_steps=4, trace=trace, artifacts=artifacts,
        run_control=RunControl(initial_soft_budget_tokens=INITIAL_BUDGET, decide_budget=decide, on_event=events.append),
    )
    saved = OpenCollab.read_session_snapshot(artifacts / "agent.json")
    messages = saved["messages"]
    assistant = [message for message in messages if message["role"] == "assistant"]
    session = sessions[0]
    ledger = [json.loads(line) for line in usage_log.read_text(encoding="utf-8").splitlines()]
    text_events = [event.data["content"] for event in runtime_events if event.type == "text_delta"]
    receipt = {
        "case": case, "trace": trace, "status": result.status, "reason": result.reason,
        "output": result.output, "tokens": result.tokens, "requests": requests, "assistant": assistant,
        "text_events": text_events, "tool_executions": tool.calls,
        "snapshots": [asdict(snapshot) for snapshot in snapshots], "ledger": ledger,
        "saved_record": str(artifacts / "agent.json"),
    }
    (tmp_path / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    assert result.status == ("stopped" if stopped_for_budget or case == "truncated" else "completed")
    assert result.error is None
    assert result.tokens == session.used_tokens == expected_tokens
    assert session.step_count == 1
    assert len(requests) == (2 if retry else 1)
    assert all((0 if retry and index == 0 else 15) <= body["max_output_tokens"]
               for index, body in enumerate(requests))
    request_snapshot = next(snapshot for snapshot in snapshots if snapshot.reason == "request")
    assert request_snapshot.reserved_input_tokens == REQUEST_INPUT_ESTIMATE
    if retry:
        assert requests[1]["max_output_tokens"] == INITIAL_BUDGET - 550 - REQUEST_INPUT_ESTIMATE == 25
    assert [event.data["total_tokens"] for event in events if event.type == "usage"] == [expected_tokens]
    assert len(ledger) == 1 and ledger[0]["usage"]["total_tokens"] == expected_tokens
    assert ledger[0]["usage"]["input_tokens"] == (1100 if retry else 550)
    assert ledger[0]["usage"]["output_tokens"] == 15
    assert (artifacts / "trajectory.jsonl").exists() is trace
    assert tool.calls == 0
    assert all(not message.get("tool_calls") for message in messages)
    assert session._open_tool_call_ids() == [] and session.runner._pending is None
    if stopped_for_budget:
        assert result.reason == f"budget exceeded after model call: {expected_tokens} tokens used"
        assert all(not message.get("response_items") and not message.get("provider_state") for message in messages)
        if case.startswith("soft_"):
            assert snapshots[-1].reason == "after_response"
            assert snapshots[-1].used_tokens == expected_tokens and snapshots[-1].soft_budget_tokens == INITIAL_BUDGET
        else:
            assert all(snapshot.used_tokens < hard_budget for snapshot in snapshots)
    elif case == "growth":
        assert session.max_budget_tokens == 2000 and snapshots[-1].reason == "after_response"
    elif case == "truncated":
        assert result.reason == "output truncated: provider reached its generation limit"
    assert result.output == (ANSWER if has_text else "")
    assert [message.get("content") for message in assistant] == ([ANSWER] if has_text else [])
    assert text_events == ([ANSWER] if has_text else [])
