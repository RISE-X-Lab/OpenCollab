from __future__ import annotations

import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.bootstrap import load_session
from opencollab.domain.agent import Agent
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.session_characterization_test_support import FakeLLMClient

_INTERIM = "I will inspect the inventory before reporting a subtotal."
_VISIBLE = "Verified inventory subtotal is 42."
_REFUSAL = "I can share the verified subtotal of 42."
_FILTER_ERROR = "Error: agent stopped: provider filtered the output"


@pytest.mark.parametrize(("finish_reason", "content", "refusal", "visible"), [
    pytest.param("stop", _VISIBLE, None, _VISIBLE, id="completed"),
    pytest.param("content_filter", _VISIBLE, None, _VISIBLE, id="partial"),
    pytest.param("content_filter", None, _REFUSAL, _REFUSAL, id="refusal"),
    pytest.param("content_filter", None, None, "", id="empty-after-tool"),
])
async def test_team_passes_filtered_reply_text_to_the_delegating_agent(
    tmp_path, monkeypatch, finish_reason, content, refusal, visible,
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
            message = {"content": _INTERIM, "tool_calls": [{
                "id": "read-inventory", "type": "function", "function": {"name": "file_read",
                    "arguments": json.dumps({"path": "inventory.txt"})},
            }]}
        else:
            assert "verified inventory: 42" in tool_results[-1]
            reason = finish_reason
            message = {"content": content, "refusal": refusal}
        return http_response(request, 200, json={
            "id": f"inventory-{len(requests)}", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", **message}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        })

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

    filtered = finish_reason == "content_filter"
    expected_delivery = _FILTER_ERROR if filtered else visible
    if filtered and visible:
        expected_delivery += f"\n\n[Partial response]\n{visible}"
    assert [row["model"] for row in requests] == [
        "fixture-lead", "fixture-reviewer", "fixture-reviewer", "fixture-lead",
    ]
    assert [row["content"] for row in requests[-1]["messages"] if row["role"] == "tool"] == [expected_delivery]
    assert result.status == "completed"
    assert result.output == f"Lead received: {expected_delivery}"
    assert result.tokens == 16
    assert [row["exception_type"] for row in result.agent_failures] == (["AgentStopped"] if filtered else [])

    path = next(artifacts.glob("agent_1_*.json"))
    snapshot = app.read_session_snapshot(path)
    assert snapshot["session_state"]["phase"] == ("stopped" if filtered else "done")
    assert snapshot["session_state"]["terminal_reason"] == (
        "provider filtered the output" if filtered else "completed"
    )
    assert [row["content"] for row in snapshot["messages"] if row["role"] == "assistant"] == (
        [_INTERIM, visible] if visible else [_INTERIM]
    )
    model = FakeLLMClient()
    env = LocalEnvironment(str(tmp_path))
    restored = load_session(str(path), agent=Agent(name="reviewer", system_prompt="Read the inventory."),
                            llm=model, env=env)
    try:
        assert await restored.run_loop() == visible
        assert model.calls == []
    finally:
        await restored.aclose()
        await env.cleanup()
