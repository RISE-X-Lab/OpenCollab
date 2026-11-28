import asyncio
import json
import threading

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.application.session import Session
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


async def test_team_deadline_preserves_final_answer_during_diff_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    team = tmp_path / "team.yaml"
    team.write_text("entry: lead\nroles:\n  lead:\n    prompt: State the total.\n    tools: []\n", encoding="utf-8")
    answer = "The total is twelve."
    diff_started = asyncio.Event()
    diff_cancelled = asyncio.Event()
    requests = []

    class SlowDiffEnvironment(LocalEnvironment):
        async def get_diff(self):
            diff_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                diff_cancelled.set()
            return ""

    def respond(request):
        requests.append(request)
        return completion_http_response(request, text=answer)

    install_sdk_transport(monkeypatch, "openai", respond)
    environment = SlowDiffEnvironment(str(tmp_path))
    client = OpenCollab(
        tmp_path, provider="openai", model="fixture-model", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1", environment=environment,
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    try:
        result = await client.team(
            "Read the total.", config=team, artifacts=artifacts, budget=20000,
            trace=False, use_worktrees=False, timeout=2.0, cleanup_timeout=1.0,
        )
        assert len(requests) == 1
        assert diff_started.is_set() and diff_cancelled.is_set()
        assert (result.status, result.reason, result.tokens) == ("stopped", "timeout", 4)
        assert result.metrics["session_quiesced"] is True
        snapshot = client.read_session_snapshot(next(artifacts.glob("agent_0_*.json")))
        assert snapshot["messages"][-1]["content"] == answer
        assert result.output == answer
    finally:
        await environment.cleanup()


@pytest.mark.parametrize(("finish_reason", "visible"), [
    ("stop", True), ("length", True), ("content_filter", True), ("length", False),
], ids=["complete", "truncated", "filtered", "reasoning-only"])
async def test_team_deadline_preserves_visible_reply_during_checkpoint(tmp_path, monkeypatch, finish_reason, visible):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    team = tmp_path / "team.yaml"
    team.write_text("entry: lead\nroles:\n  lead:\n    prompt: State the total.\n    tools: []\n", encoding="utf-8")
    timeout = 1.5
    answer = "The verified inventory contains 42 units."
    checkpoint_started = threading.Event()
    release_checkpoint = threading.Event()
    terminal_phase = "done" if finish_reason == "stop" else "stopped"
    save = Session.save
    requests = []

    def hold_checkpoint(session, path):
        if (session.phase.value == terminal_phase and threading.current_thread() is not threading.main_thread()
                and not checkpoint_started.is_set()):
            checkpoint_started.set()
            assert release_checkpoint.wait(5)
        return save(session, path)

    def respond(request):
        requests.append(request)
        reply = completion_http_response(request, text=answer, output_tokens=5).json()
        choice, = reply["choices"]
        choice["finish_reason"] = finish_reason
        if not visible:
            choice["message"].update(content=None, reasoning_content="I should check the inventory.")
        return http_response(request, 200, json=reply)

    async def release_after_deadline():
        assert await asyncio.to_thread(checkpoint_started.wait, 4)
        await asyncio.sleep(timeout + 0.1)
        release_checkpoint.set()

    monkeypatch.setattr(Session, "save", hold_checkpoint)
    install_sdk_transport(monkeypatch, "openai", respond)
    client = OpenCollab(
        tmp_path, provider="openai", model="fixture-model", api_key=tmp_path.name,
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    release = asyncio.create_task(release_after_deadline())
    try:
        result = await client.team(
            "Report the verified inventory.", config=team, artifacts=artifacts, trace=False,
            use_worktrees=False, budget=30_000, timeout=timeout, cleanup_timeout=3,
        )
        await release
    finally:
        release_checkpoint.set()
        if not release.done():
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)

    assert len(requests) == 1
    assert checkpoint_started.is_set()
    assert (result.status, result.reason, result.tokens) == ("stopped", "timeout", 7)
    assert result.output == (answer if visible else None)
    saved, = artifacts.glob("agent_0_*.json")
    if visible:
        snapshot = client.read_session_snapshot(saved)
        assert [row.get("content") for row in snapshot["messages"] if row["role"] == "assistant"] == [answer]
