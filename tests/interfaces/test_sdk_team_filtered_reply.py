from __future__ import annotations

import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.bootstrap import load_session
from opencollab.domain.agent import Agent
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.responses_provider_test_support import function_item, message_item
from tests.support.session_characterization_test_support import FakeLLMClient

_INTERIM = "I will inspect the inventory before reporting a subtotal."
_VISIBLE = "Verified inventory subtotal is 42."
_PARTIAL = _VISIBLE + " The next section"
_REFUSAL = "I can share the verified subtotal of 42."
_REASONING = "I should check whether the inventory is complete."
_FILTER_REASON = "provider filtered the output"
_TRUNCATED_REASON = "output truncated: provider reached its generation limit"


def _chat_reply(request, model, step, reason, message):
    return http_response(request, 200, json={
        "id": f"inventory-{step}", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    })


def _inventory_read():
    return {"content": _INTERIM, "tool_calls": [{
        "id": "read-inventory", "type": "function", "function": {"name": "file_read",
            "arguments": json.dumps({"path": "inventory.txt"})},
    }]}


@pytest.mark.parametrize(("finish_reason", "terminal_reply", "visible"), [
    pytest.param("stop", {"content": _VISIBLE}, _VISIBLE, id="completed"),
    pytest.param("content_filter", {"content": _VISIBLE}, _VISIBLE, id="partial"),
    pytest.param("content_filter", {"content": None, "refusal": _REFUSAL}, _REFUSAL, id="refusal"),
    pytest.param("content_filter", {"content": None}, "", id="empty-after-tool"),
    pytest.param("length", {"content": _PARTIAL}, _PARTIAL, id="truncated"),
    pytest.param("length", {"content": None}, "", id="truncated-empty-after-tool"),
    pytest.param("length", {"content": None, "reasoning_content": _REASONING}, "", id="truncated-reasoning-only"),
    pytest.param("model_context_window_exceeded", {"content": _PARTIAL}, _PARTIAL, id="context-limited"),
])
async def test_team_delivers_member_reply_text_to_the_delegating_agent(
    tmp_path, monkeypatch, finish_reason, terminal_reply, visible,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: fixture-lead\n    prompt: Delegate the inventory review.\n    tools: [spawn_agent]\n"
        "  reviewer:\n    model: fixture-reviewer\n    prompt: Read the inventory.\n    tools: [file_read]\n"
        "topology:\n  lead: [reviewer]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        requests.append(body)
        tool_results = [row["content"] for row in body["messages"] if row["role"] == "tool"]
        if model == "fixture-lead":
            if tool_results:
                return completion_http_response(request, text=f"Lead received: {tool_results[-1]}")
            reason = "tool_calls"
            message = {"content": None, "tool_calls": [{
                "id": "delegate-review", "type": "function", "function": {"name": "spawn_agent",
                    "arguments": json.dumps({"role": "reviewer", "task": "Report the verified inventory subtotal."})},
            }]}
        elif not tool_results:
            reason = "tool_calls"
            message = _inventory_read()
        else:
            assert "verified inventory: 42" in tool_results[-1]
            reason = finish_reason
            message = terminal_reply
        return _chat_reply(request, model, len(requests), reason, message)

    install_sdk_transport(monkeypatch, "openai", handler)
    artifacts = tmp_path / "artifacts"
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, trace=False,
        artifacts=artifacts, use_worktrees=False, budget=100_000, timeout=5,
    )

    stop_reason = {
        "content_filter": _FILTER_REASON,
        "length": _TRUNCATED_REASON,
        "model_context_window_exceeded": _TRUNCATED_REASON,
    }.get(finish_reason)
    expected_delivery = f"Error: agent stopped: {stop_reason}" if stop_reason else visible
    if stop_reason and visible:
        expected_delivery += f"\n\n[Partial response]\n{visible}"
    assert [row["model"] for row in requests] == [
        "fixture-lead", "fixture-reviewer", "fixture-reviewer", "fixture-lead",
    ]
    assert [row["content"] for row in requests[-1]["messages"] if row["role"] == "tool"] == [expected_delivery]
    assert result.status == "completed"
    assert result.output == f"Lead received: {expected_delivery}"
    assert result.tokens == 16
    assert [row["exception_type"] for row in result.agent_failures] == (["AgentStopped"] if stop_reason else [])

    path = next(artifacts.glob("agent_1_*.json"))
    snapshot = app.read_session_snapshot(path)
    assert snapshot["session_state"]["phase"] == ("stopped" if stop_reason else "done")
    assert snapshot["session_state"]["terminal_reason"] == (stop_reason or "completed")
    recorded_text = terminal_reply.get("reasoning_content", visible)
    assert [row["content"] for row in snapshot["messages"] if row["role"] == "assistant"] == (
        [_INTERIM, recorded_text] if recorded_text else [_INTERIM]
    )
    model = FakeLLMClient()
    env = LocalEnvironment(str(tmp_path))
    restored = load_session(str(path), agent=Agent(name="reviewer", system_prompt="Read the inventory."),
                            llm=model, env=env)
    try:
        if stop_reason == _TRUNCATED_REASON:
            cursor = restored.state.completed_turn_start_message_index
            terminal_text = snapshot["messages"][cursor]["content"] if cursor is not None else ""
            assert terminal_text == visible
        else:
            assert await restored.run_loop() == visible
        assert model.calls == []
    finally:
        await restored.aclose()
        await env.cleanup()


@pytest.mark.parametrize(("finish_reason", "content", "output", "status", "reason"), [
    pytest.param("content_filter", None, None, "stopped", _FILTER_REASON, id="empty-filter"),
    pytest.param("content_filter", _VISIBLE, _VISIBLE, "stopped", _FILTER_REASON, id="partial-filter"),
    pytest.param("length", None, _INTERIM, "stopped", _TRUNCATED_REASON, id="length-stop"),
    pytest.param("stop", _VISIBLE, _VISIBLE, "completed", None, id="completed"),
])
async def test_team_lead_returns_only_visible_filtered_text(
    tmp_path, monkeypatch, finish_reason, content, output, status, reason,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n  lead:\n    prompt: Read the inventory.\n    tools: [file_read]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        requests.append(body)
        if len(requests) == 1:
            return _chat_reply(request, model, 1, "tool_calls", _inventory_read())
        assert len(requests) == 2
        read_result = next(row["content"] for row in body["messages"] if row["role"] == "tool")
        assert "verified inventory: 42" in read_result
        return _chat_reply(request, model, 2, finish_reason, {"content": content})

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, trace=False,
        artifacts=tmp_path / "artifacts", use_worktrees=False, budget=100_000, timeout=5,
    )

    assert (result.output, result.status, result.reason, result.tokens) == (output, status, reason, 8)
    assert len(requests) == 2


@pytest.mark.parametrize(("initial_answer", "finish_reason", "content", "output"), [
    pytest.param(_VISIBLE, "stop", "Updated subtotal.", "Updated subtotal.", id="completed-followup"),
    pytest.param(_VISIBLE, "content_filter", None, _VISIBLE, id="empty-filter"),
    pytest.param(_VISIBLE, "content_filter", _REFUSAL, _REFUSAL, id="visible-filter"),
    pytest.param(None, "content_filter", None, None, id="interim-only"),
])
async def test_team_keeps_completed_lead_answer_after_empty_filtered_followup(
    tmp_path, monkeypatch, initial_answer, finish_reason, content, output,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: fixture-lead\n    prompt: Report the inventory.\n    tools: [message_agent]\n"
        "  worker:\n    model: fixture-worker\n    prompt: Send delivery updates.\n    tools: [message_agent]\n"
        "topology:\n  lead: [worker]\n  worker: [lead]\n",
        encoding="utf-8",
    )
    notice = "The delivery audit is pending."
    lead_requests = []
    worker_requests = []

    def message_call(call_id, to_role, text, interim=None):
        return {"content": interim, "tool_calls": [{
            "id": call_id, "type": "function", "function": {"name": "message_agent",
                "arguments": json.dumps({"to_role": to_role, "summary": "Delivery status", "content": text}),
            },
        }]}

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        messages = body["messages"]
        if model == "fixture-worker":
            worker_requests.append(messages)
            if len(worker_requests) == 1:
                return _chat_reply(request, model, 1, "tool_calls", message_call("notify-lead", "lead", notice))
            assert any("Message queued to aid 0" in row["content"] for row in messages if row["role"] == "tool")
            return completion_http_response(request, text="Notification sent.")
        lead_requests.append(messages)
        if any(notice in row.get("content", "") for row in messages if row["role"] == "user"):
            return _chat_reply(request, model, len(lead_requests), finish_reason, {"content": content})
        if len(lead_requests) == 1:
            return _chat_reply(
                request, model, 1, "tool_calls",
                message_call("ask-worker", "worker", "Send a separate delivery update.", interim=_INTERIM),
            )
        return _chat_reply(request, model, len(lead_requests), "stop", {"content": initial_answer})

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, artifacts=artifacts, trace=True,
        use_worktrees=False, prebuild_team=True, serialize_turns=True, budget=100_000, timeout=5,
    )
    assert len(lead_requests) == (4 if initial_answer is None else 3)
    assert len(worker_requests) == 2
    rows = [json.loads(line) for line in (artifacts / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()]
    lead_completed = next(
        row["step"] for row in rows if row["type"] == "agent_completed" and row["payload"]["aid"] == 0
    )
    worker_sent = next(
        row["step"] for row in rows if row["type"] == "message_sent" and row["payload"]["from_aid"] == 1
    )
    assert lead_completed < worker_sent
    filtered = finish_reason == "content_filter"
    assert (result.output, result.status, result.reason) == (
        output, "stopped" if filtered else "completed", _FILTER_REASON if filtered else None,
    )


@pytest.mark.parametrize(
    ("code", "exception_type", "status_code"),
    [
        ("content_filter", "ResponsesTerminalEventError", None),
        ("invalid_prompt", "ResponsesTerminalEventError", None),
        ("rate_limit_exceeded", "ResponsesTransientEventError", 429),
        ("rate_limit_error", "RateLimitError", 429),
    ],
)
async def test_team_keeps_responses_failure_metadata(tmp_path, monkeypatch, code, exception_type, status_code):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: gpt-4o\n    prompt: Delegate the inventory review.\n    tools: [spawn_agent]\n"
        "  reviewer:\n    model: gpt-4o-mini\n    prompt: Review the inventory.\n    tools: []\n"
        "topology:\n  lead: [reviewer]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/responses")
        requests.append(body)
        model = body["model"]
        if len(requests) == 3:
            assert model == "gpt-4o"
            assert any(row.get("type") == "function_call_output" for row in body["input"])
            return completion_http_response(request, text="Recorded the reviewer's outcome.")
        status = "completed"
        error = None
        incomplete = None
        if len(requests) == 1:
            assert model == "gpt-4o"
            items = [function_item("delegate", "spawn_agent", '{"role":"reviewer","task":"Review the subtotal."}')]
        else:
            assert len(requests) == 2 and model == "gpt-4o-mini"
            if code == "rate_limit_error":
                return http_response(request, 429, json={
                    "error": {"type": code, "message": "The inventory provider reported an error."},
                })
            if code == "content_filter":
                status = "incomplete"
                incomplete = {"reason": code}
                items = [message_item(_VISIBLE)]
            else:
                status = "failed"
                error = {"code": code, "message": "The inventory provider reported an error."}
                items = []
        response = {
            "id": f"inventory-{len(requests)}", "object": "response", "model": model,
            "status": status, "error": error, "incomplete_details": incomplete, "output": items,
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        }
        events = [{"type": "response.output_item.done", "output_index": index, "item": item}
                  for index, item in enumerate(items)]
        events.append({"type": f"response.{status}", "response": response})
        frames = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return http_response(request, 200, headers={"Content-Type": "text/event-stream"}, text=frames)

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, model="gpt-4o", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "responses"},
    )
    result = await app.team(
        "Report the inventory subtotal.", config=team, trace=False, use_worktrees=False,
        budget=100_000, timeout=5,
    )

    assert len(requests) == 3
    expected_tokens = 8 if code == "rate_limit_error" else 12
    assert (result.status, result.output, result.tokens) == (
        "completed", "Recorded the reviewer's outcome.", expected_tokens,
    )
    assert result.agent_failures == ({
        "label": "reviewer",
        "exception_type": exception_type,
        "status_code": status_code,
        "provider_error_type": code,
    },)
