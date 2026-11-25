"""Restore a settled Team snapshot through the public CLI."""

import asyncio
import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli import main as cli_main
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("outcome", ["completed", "timeout"])
@pytest.mark.parametrize("submit_summary", [False, True])
async def test_team_nested_turn_does_not_restart_after_settlement(tmp_path, monkeypatch, outcome, submit_summary):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    monkeypatch.chdir(tmp_path)
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: lead-fixture\n    prompt: Assemble both results.\n    tools: [spawn_agent, submit]\n"
        "  reviewer:\n    model: reviewer-fixture\n    prompt: Check the figures.\n    tools: [spawn_agent]\n"
        "  reader:\n    model: reader-fixture\n    prompt: Return the measured value.\n    tools: []\n"
        "topology:\n  lead: [reviewer]\n  reviewer: [reader]\n  reader: []\n",
        encoding="utf-8",
    )
    requests = []
    leaf_cancelled = asyncio.Event()
    restoring = False
    summary = "The first table contains eight observations."

    async def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        messages = body["messages"]
        requests.append((model, messages))
        if restoring:
            return completion_http_response(request, text="Restored answer.")
        if model == "reader-fixture":
            if outcome == "timeout":
                try:
                    await asyncio.Event().wait()
                finally:
                    leaf_cancelled.set()
            return completion_http_response(request, text="Measured value is 42.")
        tool_result = next((row["content"] for row in messages if row["role"] == "tool"), None)
        if tool_result is not None:
            return completion_http_response(request, text=tool_result)
        role = "reviewer" if model == "lead-fixture" else "reader"
        tool_calls = [{
            "id": f"{role}-call", "type": "function", "function": {
                "name": "spawn_agent", "arguments": json.dumps({"role": role, "task": f"Run {role}."}),
            },
        }]
        if submit_summary and model == "lead-fixture":
            tool_calls.append({
                "id": "submit-call", "type": "function", "function": {
                    "name": "submit", "arguments": json.dumps({"summary": summary}),
                },
            })
        return http_response(request, 200, json={
            "id": f"delegate-{role}", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": tool_calls,
            }}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
        })

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, provider="openai", model="lead-fixture",
        api_key="team-test-token",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    result = await app.team(
        "Check the figures.", config=team, artifacts=artifacts, trace=False, use_worktrees=False,
        budget=100_000, timeout=2 if outcome == "timeout" else 5, cleanup_timeout=1,
    )
    assert result.metrics["execution_quiesced"] is True
    assert [model for model, _ in requests][:3] == ["lead-fixture", "reviewer-fixture", "reader-fixture"]
    if outcome == "timeout":
        assert (result.status, result.reason, result.tokens) == ("stopped", "timeout", 8)
        assert leaf_cancelled.is_set()
        assert len(requests) == 3
    else:
        expected_answer = summary if submit_summary else "Measured value is 42."
        expected_tokens = 16 if submit_summary else 20
        assert (result.status, result.output, result.tokens) == ("completed", expected_answer, expected_tokens)
        assert len(requests) == (4 if submit_summary else 5)

    snapshots = [app.read_session_snapshot(next(artifacts.glob(f"agent_{aid}_*.json"))) for aid in range(3)]
    phases = [snapshot["session_state"]["phase"] for snapshot in snapshots]
    rows = [[(r["tool_call_id"], r["status"]) for r in snapshot["session_state"]["pending_events"]]
            for snapshot in snapshots]
    manifest = json.loads((artifacts / "team.json").read_text())
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(json.dumps(snapshots[0]), encoding="utf-8")
    before_resume = len(requests)
    restoring = True
    resumed = await asyncio.to_thread(CliRunner().invoke, cli_main.app, [
        "--workspace", str(tmp_path), "--team-config", str(team), "--no-worktrees",
        "--provider", "openai", "--model", "lead-fixture", "--api-key", "team-test-token",
        "--base-url", "https://fixture.invalid/v1", "--budget", "100000",
        "--session", str(checkpoint), "--prompt", "Review the follow-up figures.",
    ])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    restored_requests = requests[before_resume:]
    assert [model for model, _ in restored_requests] == ["lead-fixture"]
    restored_messages = restored_requests[0][1]
    assert restored_messages[-1]["content"] == "Review the follow-up figures."
    tool_rows = [row for row in restored_messages if row["role"] == "tool"]
    expected_result = (
        "Error: scheduler cleanup cancelled delegated work" if outcome == "timeout" else "Measured value is 42."
    )
    expected_tools = [("reviewer-call", expected_result)]
    if submit_summary:
        expected_tools.append(("submit-call", "Submitted. Your turn ends here."))
        assert snapshots[0]["messages"][-1]["content"] == summary
        assert restored_messages[-2] == {"role": "assistant", "content": summary}
    assert [(row["tool_call_id"], row["content"]) for row in tool_rows] == expected_tools
    expected_phase = "stopped" if outcome == "timeout" else "done"
    assert phases == [expected_phase] * 3
    assert [agent["phase"] for agent in manifest["agents"]] == phases
    assert all(agent["busy"] is False for agent in manifest["agents"])
    expected_rows = [[] if submit_summary else [("reviewer-call", "failed")], [("reader-call", "failed")], []]
    assert rows == (expected_rows if outcome == "timeout" else [[], [], []])
