import asyncio
import json

import pytest

from opencollab import OpenCollab
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("outcome", ["submit", "working", "complete"])
@pytest.mark.parametrize("persist", [False, True], ids=["live", "artifacts"])
async def test_team_deadline_returns_only_an_accepted_answer(tmp_path, monkeypatch, outcome, persist):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: lead-fixture\n    prompt: Report the figures.\n    tools: [spawn_agent, submit]\n"
        "  reader:\n    model: reader-fixture\n    prompt: Check the figures.\n    tools: []\n"
        "topology:\n  lead: [reader]\n  reader: []\n",
        encoding="utf-8",
    )
    requests = []
    reader_cancelled = asyncio.Event()
    answer = "The first table has eight observations."

    async def model_request(request):
        body = json.loads(request.content)
        model = body["model"]
        requests.append(model)
        if model == "reader-fixture":
            if outcome != "complete":
                try:
                    await asyncio.Event().wait()
                finally:
                    reader_cancelled.set()
            return completion_http_response(request, text="Checked. The first table has eight observations.")
        assert model == "lead-fixture"
        tool_calls = [{
            "id": "reader-call", "type": "function", "function": {
                "name": "spawn_agent", "arguments": json.dumps({"role": "reader", "task": "Check the table."}),
            },
        }]
        if outcome != "working":
            tool_calls.append({
                "id": "submit-call", "type": "function", "function": {
                    "name": "submit", "arguments": json.dumps({"summary": answer}),
                },
            })
        return http_response(request, 200, json={
            "id": "team-submission", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "I am checking the first table.", "tool_calls": tool_calls,
            }}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        })

    install_sdk_transport(monkeypatch, "openai", model_request)
    client = OpenCollab(
        tmp_path, provider="openai", model="lead-fixture", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts" if persist else None
    result = await client.team(
        "Report the table count.", config=team, artifacts=artifacts, trace=False, use_worktrees=False,
        budget=100_000, timeout=2.0, cleanup_timeout=1.0,
    )
    assert requests == ["lead-fixture", "reader-fixture"]
    assert result.metrics["execution_quiesced"] is True
    if outcome == "complete":
        assert (result.status, result.reason, result.tokens) == ("completed", None, 8)
    else:
        assert (result.status, result.reason, result.tokens) == ("stopped", "timeout", 4)
        assert isinstance(result.error, asyncio.TimeoutError)
        assert isinstance(result.error.__cause__, asyncio.CancelledError)
        assert reader_cancelled.is_set()

    assert result.output == (None if outcome == "working" else answer)
