from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli import workflow as workflow_cli
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport


@pytest.mark.parametrize("surface", ["cli", "sdk"])
@pytest.mark.parametrize("settle_first", [False, True])
def test_budget_stop_reports_spend_after_owned_cleanup(tmp_path, monkeypatch, surface, settle_first):
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(usage_path))
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "chat_completions")
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "false")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def measure(ctx, _inputs):
        first = asyncio.create_task(ctx.agent("First measurement", budget=80_000, tools=[]))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            if settle_first:
                release.set()
                initial = await first
                followup = await ctx.agent("Review measurement", tools=[])
                return [initial, followup]
            return await ctx.agent("Review measurement", tools=[])
        finally:
            release.set()

    async def respond(request):
        messages = json.loads(request.content)["messages"]
        prompt = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        calls.append(prompt)
        if prompt == "First measurement":
            started.set()
            await release.wait()
        return completion_http_response(request, text="Verified subtotal", output_tokens=5)

    install_sdk_transport(monkeypatch, "openai", respond)
    base_url = "https://fixture.invalid/v1"
    if surface == "cli":
        registry = Registry()
        registry.register(WorkflowSpec("measure", "Measure the subtotal.", measure))
        monkeypatch.setattr(workflow_cli, "load_registry", lambda _workspace: registry)
        run = CliRunner().invoke(workflow_cli.app, [
            "run", "measure", "--workspace", str(tmp_path), "--model", "offline-checker", "--provider", "openai",
            "--api-key", tmp_path.name, "--base-url", base_url, "--budget", "80000", "--no-trace",
        ])
        assert run.exit_code == (0 if settle_first else 1), run.output
        start = run.output.rindex("[\n" if settle_first else "{\n")
        output = json.loads(run.output[start:])
        manifest_path, = (tmp_path / ".opencollab" / "sessions").glob("*/workflow.json")
    else:
        app = OpenCollab(tmp_path, model="offline-checker", provider="openai", api_key=tmp_path.name, base_url=base_url)
        artifacts = tmp_path / "artifacts"
        result = asyncio.run(app.workflow(measure, artifacts=artifacts, budget=80_000, trace=False))
        assert result.status == ("completed" if settle_first else "stopped")
        assert result.reason == (None if settle_first else "budget_exceeded")
        assert result.tokens == (14 if settle_first else 7)
        output = result.output
        manifest_path = artifacts / "workflow.json"

    manifest = json.loads(manifest_path.read_text())
    ledger = [json.loads(row) for row in usage_path.read_text().splitlines()]
    paid = sum(row["usage"]["total_tokens"] for row in ledger)
    assert len(calls) == len(ledger) == (2 if settle_first else 1)
    assert manifest["evidence_complete"] is True
    assert manifest["tokens_spent"] == paid == (14 if settle_first else 7)
    if settle_first:
        assert output == ["Verified subtotal", "Verified subtotal"]
    else:
        assert output == {
            "status": "budget_exceeded",
            "error": "workflow budget exhausted: spent 0 of 80000",
            "tokens_spent": paid,
            "budget_total": 80_000,
        }
