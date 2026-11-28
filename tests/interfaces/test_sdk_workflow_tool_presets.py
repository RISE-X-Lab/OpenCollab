"""Combine a public read preset with each Workflow agent's output mode."""

from __future__ import annotations

import json
import subprocess

import pytest

from opencollab import OpenCollab
from opencollab.application.session_run import ENFORCEMENT_ON
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport

READ_TOOLS = ["file_read", "grep", "git_diff"]
INVENTORY = "Inventory subtotal 42"
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"subtotal": {"type": "integer"}},
    "required": ["subtotal"],
}


@pytest.mark.parametrize("profile", [None, "base"])
@pytest.mark.parametrize("mode", ["text", "schema", "enforced"])
@pytest.mark.parametrize("nested", [False, True])
async def test_workflow_read_preset_with_output_mode(tmp_path, monkeypatch, mode, profile, nested):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inventory.txt").write_text(INVENTORY + "\n", encoding="utf-8")
    if nested:
        for args in (
            ("init", "-q"),
            ("add", "inventory.txt"),
            ("-c", "user.name=Test User", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "Initialize inventory"),
        ):
            subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        history = [message["content"] for message in body["messages"] if message["role"] == "tool"]
        if not any(INVENTORY in output for output in history):
            if not any(tool["function"]["name"] == "file_read" for tool in body.get("tools", [])):
                return completion_http_response(request, text="The inventory is unavailable.", output_tokens=3)
            name, arguments = "file_read", {"path": "inventory.txt"}
        else:
            if mode == "text":
                return completion_http_response(request, text=INVENTORY, output_tokens=3)
            if mode == "schema":
                name, arguments = "structured_output", {"subtotal": 42}
            else:
                name, arguments = "submit_findings", {
                    "findings": [{
                        "aspect": "subtotal", "claim": INVENTORY, "evidence_anchor": "inventory.txt:1",
                        "verified": True, "confidence": "high",
                    }],
                    "summary": INVENTORY,
                    "insufficient_evidence": False,
                }
        return http_response(request, 200, json={
            "id": f"inventory-{len(requests)}", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": f"inventory-tool-{len(requests)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments),
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        })

    install_sdk_transport(monkeypatch, "openai", respond)

    async def read_inventory(ctx, _inputs):
        options = {}
        if mode == "schema":
            options["schema"] = RESULT_SCHEMA
        elif mode == "enforced":
            options.update(enforcement_strength=ENFORCEMENT_ON, commit_reserve=2048)
        return await ctx.agent("Read inventory.txt and report the subtotal.", tools="read", **options)

    async def flow(ctx, inputs):
        if not nested:
            return await read_inventory(ctx, inputs)
        candidate = await ctx.candidate_workflow(read_inventory, inputs, label="inventory-reader")
        return candidate.output

    app = OpenCollab(
        workspace, model="offline-inventory", provider="openai",
        api_key="fixture-unused",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"wire_protocol": "chat_completions", "llm_stream_chat": False,
                "max_output_tokens": 128, "llm_max_retries": 0},
    )
    artifacts = tmp_path / "artifacts"
    result = await app.workflow(flow, budget=50_000, agent_profile=profile, artifacts=artifacts, limit_mode="explicit")
    assert result.ok and result.agent_failures == ()
    assert len(requests) == 2 and result.tokens == 10
    expected_tools = {
        "text": READ_TOOLS,
        "schema": ["structured_output", *READ_TOOLS],
        "enforced": [*READ_TOOLS, "submit_findings"],
    }[mode]
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == expected_tools
    trace = (artifacts / "orchestration.jsonl").read_text().splitlines()
    started = [row["payload"] for line in trace if (row := json.loads(line))["type"] == "workflow_agent_started"]
    assert [row["tools"] for row in started] == [expected_tools]
    if mode == "schema":
        assert result.output == {"subtotal": 42}
    elif mode == "enforced":
        assert INVENTORY in result.output and "inventory.txt:1" in result.output
    else:
        assert result.output == INVENTORY
