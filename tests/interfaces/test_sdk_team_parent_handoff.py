"""Public SDK teams resume after child delivery during a real saved checkpoint."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.application.scheduler import Scheduler
from opencollab.application.session import Session
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("child_failure", [False, True])
@pytest.mark.parametrize("children,checkpoint_tail", [(1, True), (2, True), (1, False)])
async def test_public_saved_parent_delivers_each_child_once(
    tmp_path, monkeypatch, child_failure, children, checkpoint_tail,
):
    config = tmp_path / "team.yaml"
    config.write_text(
        "entry: lead\nroles:\n  lead:\n    prompt: Delegate and summarize.\n"
        "    model: lead-demo\n    tools: [spawn_agent]\n  reviewer:\n"
        "    prompt: Review and return a conclusion.\n    model: reviewer-demo\n"
        "    tools: []\ntopology:\n  lead: [reviewer]\n  reviewer: []\n",
        encoding="utf-8",
    )
    for variable in (
        "OPENCOLLAB_API_USAGE_LOG", "OPENCOLLAB_WORKFLOWS_DIR", "OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []
    schedulers = []
    checkpoint_entered = asyncio.Event()
    children_delivered = asyncio.Event()
    original_init = Scheduler.__init__
    original_checkpoint = Session._checkpoint_terminal_snapshot
    original_wake = Scheduler._wake
    lead_calls = 0
    deliveries = 0

    def capture_scheduler(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        schedulers.append(self)

    async def save_after_child_delivery(self, **kwargs):
        if checkpoint_tail and self.agent.name == "lead" and self.state.phase is SessionPhase.AWAITING_EVENTS:
            checkpoint_entered.set()
            await children_delivered.wait()
        await original_checkpoint(self, **kwargs)

    async def capture_delivery(self, *args, **kwargs):
        nonlocal deliveries
        await original_wake(self, *args, **kwargs)
        deliveries += 1
        if deliveries == children:
            children_delivered.set()

    async def handler(request):
        nonlocal lead_calls
        body = json.loads(request.content)
        requests.append(body)
        if body["model"] == "reviewer-demo":
            if checkpoint_tail:
                await checkpoint_entered.wait()
            if child_failure:
                return http_response(request, 400, json={"error": {
                    "message": "Invalid child request", "type": "invalid_request_error",
                }})
            response = completion_http_response(request, text="Child review complete.")
        else:
            lead_calls += 1
            if lead_calls == 1:
                response = http_response(request, 200, json={
                    "id": "chat-spawn", "object": "chat.completion", "created": 1, "model": body["model"],
                    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [{
                            "id": f"child-{index}", "type": "function", "function": {
                                "name": "spawn_agent", "arguments": json.dumps({
                                    "role": "reviewer", "task": f"Review section {index}.",
                                }),
                            },
                        } for index in range(children)],
                    }}],
                })
            else:
                tool_results = [message for message in body["messages"] if message["role"] == "tool"]
                assert [message["tool_call_id"] for message in tool_results] == [
                    f"child-{index}" for index in range(children)
                ]
                assert all(
                    ("Invalid child request" if child_failure else "Child review complete.") in message["content"]
                    for message in tool_results
                )
                response = completion_http_response(request, text="Final reviewed conclusion.")
        wire = response.json()
        estimate = estimate_request_tokens(body["messages"], body.get("tools"))
        assert estimate + 8192 < 1_048_576
        wire["usage"] = {"prompt_tokens": estimate, "completion_tokens": 10, "total_tokens": estimate + 10}
        return http_response(request, 200, json=wire)

    monkeypatch.setattr(Scheduler, "__init__", capture_scheduler)
    monkeypatch.setattr(Scheduler, "_wake", capture_delivery)
    monkeypatch.setattr(Session, "_checkpoint_terminal_snapshot", save_after_child_delivery)
    install_sdk_transport(monkeypatch, "openai", handler)
    prompt = "Produce a reviewed conclusion.\n"
    if checkpoint_tail:
        prompt += "Review the supplied settings documentation.\n" * 2400
    result = await OpenCollab(
        tmp_path, provider="openai", model="lead-demo",
        api_key="controlled-test", base_url="https://controlled.invalid/v1",  # pragma: allowlist secret
        config={"wire_protocol": "chat_completions", "llm_max_retries": 0,
                "context_window": 1_048_576, "max_output_tokens": 8192},
    ).team(prompt, config=config, use_worktrees=False, trace=True,
           artifacts=tmp_path / "artifacts", timeout=5, budget=1_000_000)

    assert result.status == "completed", (result.reason, result.error)
    assert result.output == "Final reviewed conclusion."
    assert lead_calls == 2
    assert deliveries == children
    assert [request["model"] for request in requests] == [
        "lead-demo", *["reviewer-demo"] * children, "lead-demo",
    ]
    assert 0 < result.tokens < 1_000_000
    scheduler = schedulers[0]
    lead = scheduler._sessions[0]
    assert scheduler._tasks == {}
    assert lead.state.phase is SessionPhase.DONE
    assert lead.state.pending_events.is_empty()
    snapshot = json.loads(Path(lead.auto_save_path).read_text(encoding="utf-8"))
    assert snapshot["session_state"]["phase"] == "done"
    assert [message["tool_call_id"] for message in snapshot["messages"] if message["role"] == "tool"] == [
        f"child-{index}" for index in range(children)
    ]
