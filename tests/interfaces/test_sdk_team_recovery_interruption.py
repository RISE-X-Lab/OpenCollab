from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli import main as cli_main
from opencollab.bootstrap import build_scheduler
from opencollab.bootstrap.config import build_config
from opencollab.bootstrap.runtime_context import build_runtime_context
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _tool_reply(request, *calls):
    return http_response(request, 200, json={
        "id": "chat-team-recovery", "object": "chat.completion", "created": 1,
        "model": json.loads(request.content)["model"],
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args),
                }}
                for call_id, name, args in calls
            ],
        }}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    })


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_team_recovery_keeps_accepted_submission(tmp_path, monkeypatch, interruption):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: lead-fixture\n    prompt: Report the verified table count.\n"
        "    tools: [spawn_agent, submit]\n"
        "  reviewer:\n    model: reviewer-fixture\n    prompt: Review the table.\n"
        "    tools: []\ntopology:\n  lead: [reviewer]\n  reviewer: []\n",
        encoding="utf-8",
    )

    original_prompt = "Review the table."
    queued_prompt = "Follow up on the table review."
    final_prompt = "Recall the previously submitted count."
    summary = "The verified table contains 17 rows."
    restored_reply = "The previous submission reported 17 rows."
    original_review_started = asyncio.Event()
    original_review_release = asyncio.Event()
    recovery_review_started = asyncio.Event()
    recovery_review_cancelled = asyncio.Event()
    final_requests = []
    stage = "original"

    async def handler(request):
        body = json.loads(request.content)
        messages = body["messages"]
        if stage == "final":
            final_requests.append((body["model"], messages))
            return completion_http_response(request, text=restored_reply)
        if body["model"] == "reviewer-fixture":
            if stage == "original":
                original_review_started.set()
                await original_review_release.wait()
                return completion_http_response(request, text="Original table reviewed.")
            recovery_review_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                recovery_review_cancelled.set()
        tool_rows = [row for row in messages if row["role"] == "tool"]
        if stage == "original":
            if tool_rows:
                return completion_http_response(request, text="Original answer.")
            return _tool_reply(request, ("original-review", "spawn_agent", {
                "role": "reviewer", "task": "Review the original table.",
            }))
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "original-review"
        assert messages[-1]["content"] == "deferred child interrupted by session restore"
        return _tool_reply(
            request,
            ("recovery-review", "spawn_agent", {"role": "reviewer", "task": "Check the next table."}),
            ("submit-count", "submit", {"summary": summary}),
        )

    install_sdk_transport(monkeypatch, "openai", handler)
    provider = {
        "provider": "openai", "model": "lead-fixture", "api_key": "team-test-token",  # pragma: allowlist secret
        "base_url": "https://fixture.invalid/v1",
    }
    transport = {"wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0}
    app = OpenCollab(tmp_path, config=transport, **provider)

    async def await_suspension(directory, *, submitted_summary=None):
        while True:
            for path in directory.glob("agent_0_*.json"):
                snapshot = app.read_session_snapshot(path)
                state = snapshot["session_state"]
                if state["phase"] == "awaiting_events" and state["submitted_summary"] == submitted_summary:
                    return path, snapshot
            await asyncio.sleep(0.005)

    original_dir = tmp_path / "original"
    original = asyncio.create_task(app.team(
        original_prompt, config=team, artifacts=original_dir,
        use_worktrees=False, trace=False, budget=100_000, timeout=10,
    ))
    try:
        await asyncio.wait_for(original_review_started.wait(), 5)
        _, original_snapshot = await asyncio.wait_for(await_suspension(original_dir), 5)
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps(original_snapshot), encoding="utf-8")
    finally:
        original_review_release.set()
        first_result = await asyncio.wait_for(original, 5)
    assert first_result.status == "completed"

    stage = "recovery"
    cfg = build_config(str(tmp_path), overrides={**provider, **transport, "budget": 100_000})
    context = build_runtime_context(str(tmp_path), cfg.model_dump(), trace=False)
    recovery_dir = tmp_path / "recovery"
    scheduler = build_scheduler(
        context, use_worktrees=False, interactive=True, team_config_path=team,
        session_file=str(checkpoint), save_dir=recovery_dir,
    )
    active = asyncio.create_task(scheduler.run(queued_prompt))
    try:
        await asyncio.wait_for(recovery_review_started.wait(), 5)
        recovery_path, accepted = await asyncio.wait_for(
            await_suspension(recovery_dir, submitted_summary=summary), 5,
        )
        assert [(row["tool_call_id"], row["status"]) for row in accepted["session_state"]["pending_events"]] == [
            ("recovery-review", "pending"), ("submit-count", "done"),
        ]
        if interruption == "cancel":
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        else:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(active, 0)
        assert recovery_review_cancelled.is_set()
    finally:
        if not active.done():
            active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        await scheduler.cleanup()

    settled = app.read_session_snapshot(recovery_path)
    stage = "final"
    resumed = await asyncio.to_thread(CliRunner().invoke, cli_main.app, [
        "--workspace", str(tmp_path), "--team-config", str(team), "--no-worktrees",
        "--provider", provider["provider"], "--model", provider["model"], "--api-key", provider["api_key"],
        "--base-url", provider["base_url"], "--budget", "100000", "--session", str(recovery_path),
        "--prompt", final_prompt,
    ])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    assert restored_reply in resumed.output
    assert [model for model, _ in final_requests] == ["lead-fixture"]
    messages = final_requests[0][1]
    assert messages[-1] == {"role": "user", "content": final_prompt}
    assert messages[-2] == {"role": "assistant", "content": summary}
    assert sum(message == messages[-2] for message in messages) == 1
    assert [row["content"] for row in messages if row["role"] == "user"] == [original_prompt, final_prompt]
    assert [(row["tool_call_id"], row["content"]) for row in messages if row["role"] == "tool"] == [
        ("original-review", "deferred child interrupted by session restore"),
        ("recovery-review", "Error: scheduler cleanup cancelled delegated work"),
        ("submit-count", "Submitted. Your turn ends here."),
    ]
    assert settled["session_state"]["phase"] == "stopped"
    assert settled["session_state"]["pending_events"] == []
    assert settled["session_state"]["submitted_summary"] is None
    assert settled["messages"][-1]["content"] == summary
