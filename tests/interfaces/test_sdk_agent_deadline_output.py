from __future__ import annotations

import asyncio
import json
import threading

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.client import LLMClient
from opencollab.application.session import Session
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("entry", ["agent", "workflow-agent"])
@pytest.mark.parametrize("scene", [
    "completed", "checkpoint", "truncated_checkpoint", "reasoning_checkpoint", "unfinished", "late",
])
async def test_agent_retains_only_accepted_output_when_the_checkpoint_crosses_deadline(
    tmp_path, monkeypatch, entry, scene,
):
    answer = "Verified intake recorded as 42."
    reasoning = "I should check the remaining intake rows."
    truncated = scene in {"truncated_checkpoint", "reasoning_checkpoint"}
    held_checkpoint = scene == "checkpoint" or truncated
    checkpoint, released = threading.Event(), threading.Event()
    original_save = Session.save
    calls = []

    def hold_checkpoint(session, path):
        phase = "stopped" if truncated else "done"
        if held_checkpoint and not checkpoint.is_set() and session.phase.value == phase:
            [recorded] = [row["content"] for row in session.messages if row["role"] == "assistant"]
            assert recorded == (reasoning if scene == "reasoning_checkpoint" else answer)
            checkpoint.set()
            assert released.wait(6)
        return original_save(session, path)

    async def respond(request):
        calls.append(json.loads(request.content)["model"])
        if scene in {"unfinished", "late"}:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if scene == "unfinished":
                    raise
                return completion_http_response(request, output_tokens=5, text="Late response.")
        response = completion_http_response(request, output_tokens=5, text=answer)
        if not truncated:
            return response
        body = response.json()
        body["choices"][0]["finish_reason"] = "length"
        if scene == "reasoning_checkpoint":
            body["choices"][0]["message"].update(content=None, reasoning_content=reasoning)
        return http_response(request, 200, json=body)

    async def release_after_deadline():
        assert await asyncio.to_thread(checkpoint.wait, 5)
        await asyncio.sleep(1.1)
        released.set()

    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setattr(Session, "save", hold_checkpoint)
    install_sdk_transport(monkeypatch, "openai", respond)
    timeout = 4 if scene == "completed" else 1
    artifacts = tmp_path / "artifacts"
    app = OpenCollab(tmp_path, model="gpt-4o", config={"max_output_tokens": 128})

    async def workflow(ctx, _):
        stage = await ctx.agent_run("Record the intake.", tools=[], timeout=timeout, cleanup_timeout=3)
        assert stage.cleanup_complete and stage.workspace_ready
        return {"output": stage.output, "status": stage.status, "reason": stage.reason, "tokens": stage.tokens}

    release_task = asyncio.create_task(release_after_deadline()) if held_checkpoint else None
    try:
        async with LLMClient(
            model="gpt-4o", api_key=tmp_path.name, base_url="https://offline.invalid/v1",
            max_retries=0, stream_chat=False,
        ) as model:
            if entry == "agent":
                result = await app.agent(
                    "Record the intake.", llm=model, tools=[], budget=20_000,
                    artifacts=artifacts, trace=False, timeout=timeout, cleanup_timeout=3,
                )
                assert result.metrics["execution_quiesced"] is True
                public = {"output": result.output, "status": result.status, "reason": result.reason,
                          "tokens": result.tokens}
            else:
                result = await app.workflow(
                    workflow, llm=model, budget=20_000, concurrency=1, limit_mode="explicit",
                    artifacts=artifacts, trace=False, timeout=14,
                )
                assert result.ok
                public = result.output
        if release_task is not None:
            await release_task
    finally:
        released.set()
        if release_task is not None and not release_task.done():
            release_task.cancel()
            await asyncio.gather(release_task, return_exceptions=True)

    saved, = (path for path in artifacts.glob("*.json") if path.name != "workflow.json")
    snapshot = app.read_session_snapshot(saved)
    recorded = [row["content"] for row in snapshot["messages"] if row["role"] == "assistant"]
    accepted = scene in {"completed", "checkpoint", "truncated_checkpoint"}
    assert calls == ["gpt-4o"]
    assert result.tokens == public["tokens"] == snapshot["session_state"]["used_tokens"]
    assert public["tokens"] == (0 if scene == "unfinished" else 7)
    expected_transcript = [answer] if accepted else []
    if scene == "reasoning_checkpoint":
        expected_transcript = [reasoning]
    assert recorded == expected_transcript
    assert public["output"] == (answer if accepted else None)
    assert (public["status"], public["reason"]) == (
        ("completed", None) if scene == "completed" else ("stopped", "timeout")
    )
