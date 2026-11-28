"""Terminal session exports require settled native usage after cancellation."""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab import OpenCollab
from opencollab.application.session import SessionBusyError
from opencollab.bootstrap import build_session, load_session
from opencollab.bootstrap.agent_profiles import resolve_agent_profile
from opencollab.domain.agent import Agent
from opencollab.environments import local_environment
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.responses_provider_test_support import function_item


def _tool_response(request, provider, call_id, output_tokens):
    body = json.loads(request.content)
    arguments = {"path": "notes.txt"}
    usage = {"input_tokens": 2, "output_tokens": output_tokens}
    if provider == "anthropic":
        return http_response(request, 200, json={
            "id": f"msg_{call_id}", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "tool_use", "id": call_id, "name": "file_read", "input": arguments}],
            "stop_reason": "tool_use", "stop_sequence": None, "usage": usage,
        })

    item = function_item(call_id, "file_read", json.dumps(arguments))
    response = {
        "id": f"resp_{call_id}", "object": "response", "status": "completed", "model": body["model"],
        "output": [item], "error": None, "incomplete_details": None,
        "usage": {**usage, "total_tokens": 2 + output_tokens},
    }
    events = [
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": response},
    ]
    stream = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
    return http_response(request, 200, headers={"content-type": "text/event-stream"}, text=stream)


def _tool_results(request_body, provider):
    if provider == "anthropic":
        return {
            item["tool_use_id"]: item["content"]
            for message in request_body["messages"] if isinstance(message["content"], list)
            for item in message["content"] if item["type"] == "tool_result"
        }
    return {
        item["call_id"]: item["output"]
        for item in request_body["input"] if item.get("type") == "function_call_output"
    }


async def _drain(session):
    while tasks := session.pending_cleanup_tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("llm_timeout", [None, 600])
async def test_terminal_export_requires_settled_native_usage(tmp_path, monkeypatch, provider, llm_timeout):
    usage_log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_log))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "notes.txt").write_text("The event is on April 18.\n", encoding="utf-8")
    automatic, exported = tmp_path / "automatic.json", tmp_path / "exported.json"
    admitted, release = asyncio.Event(), asyncio.Event()
    requests = []
    environment = local_environment(tmp_path)
    options = {
        "env": environment,
        "auto_save_path": str(automatic),
        "max_budget_tokens": 60_000,
        "llm_timeout": llm_timeout,
    }
    profile = resolve_agent_profile("base")

    def agent():
        return Agent(
            name="reader", system_prompt="Read and remember the date.", tools=list(profile.resolve_tools("read")),
            model="gpt-4o" if provider == "openai" else "claude-sonnet-4-6",
            provider=provider, wire_protocol="responses" if provider == "openai" else "chat_completions",
            api_key=tmp_path.name, base_url="https://provider.invalid/v1",
            llm_max_retries=0,
        )

    async def peer(request):
        requests.append(json.loads(request.content))
        match len(requests):
            case 1:
                return _tool_response(request, provider, "call_first", 9)
            case 2:
                return completion_http_response(request, output_tokens=11, text="Recorded April 18.")
            case 3:
                return _tool_response(request, provider, "call_second", 15)
            case 4:
                admitted.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                return completion_http_response(request, output_tokens=21, text="Late unused answer.")
            case 5:
                return completion_http_response(request, output_tokens=27, text="I still remember April 18.")
            case _:
                pytest.fail("unexpected extra provider request")

    install_sdk_transport(monkeypatch, provider, peer)
    original = build_session(agent=agent(), agent_profile=profile, **options)
    turn = resumed = None
    try:
        await original.add_user_message("Read notes.txt and remember the date.")
        assert await original.run_loop() == "Recorded April 18."
        await original.add_user_message("Read the file again to verify the date.")
        turn = asyncio.create_task(original.run_loop())
        await asyncio.wait_for(admitted.wait(), 2)
        turn.cancel()
        done, _ = await asyncio.wait({turn}, timeout=2)
        assert turn in done
        with pytest.raises(asyncio.CancelledError):
            await turn

        stopped = OpenCollab.read_session_snapshot(automatic)
        assert stopped["session_state"]["phase"] == "stopped"
        assert stopped["session_state"]["used_tokens"] == 41
        with pytest.raises(SessionBusyError, match="provider requests are still draining"):
            original.save(str(exported))
        assert not exported.exists()

        release.set()
        await asyncio.wait_for(_drain(original), 2)
        original.save(str(exported))
        await original.aclose()
        resumed = load_session(str(exported), agent=agent(), agent_profile=profile, **options)
        assert resumed.used_tokens == 64
        await resumed.add_user_message("Tell me the date you remember.")
        assert await resumed.run_loop() == "I still remember April 18."
        assert len(requests) == 5
        results = _tool_results(requests[-1], provider)
        assert set(results) == {"call_first", "call_second"}
        assert all("April 18" in json.dumps(value) for value in results.values())
        assert "Late unused answer." not in json.dumps(requests[-1])
        assert OpenCollab.read_session_snapshot(automatic)["session_state"]["used_tokens"] == 93
        usage = [json.loads(line)["usage"]["total_tokens"] for line in usage_log.read_text().splitlines()]
        assert usage == [11, 13, 17, 23, 29]
    finally:
        release.set()
        if turn is not None:
            await asyncio.gather(turn, return_exceptions=True)
        await _drain(original)
        await original.aclose()
        if resumed is not None:
            await resumed.aclose()
        await environment.cleanup()
