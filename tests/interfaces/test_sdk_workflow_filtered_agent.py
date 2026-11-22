from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli.main import app
from tests.runtime.test_llm_chat_refusal_http import _reply
from tests.support.provider_sdk_http import install_sdk_transport

_PARTIAL = "The measurement stopped partway through."
_SUBMIT = {
    "id": "submit",
    "type": "function",
    "function": {"name": "structured_output", "arguments": '{"measured":true}'},
}
_FAILURE = {
    "label": "reviewer",
    "exception_type": "AgentStopped",
    "status_code": None,
    "provider_error_type": "content_filter",
}
_WORKFLOW = """\
from opencollab import workflow
from opencollab.application.session_run import ENFORCEMENT_ON
from opencollab.tools import builtin_tools

@workflow(name="measurement", description="Return a measurement")
async def measurement(ctx, inputs):
    options = {}
    if inputs.get("structured"):
        options["schema"] = {
            "type": "object", "properties": {"measured": {"type": "boolean"}},
            "required": ["measured"],
        }
    if inputs.get("scout"):
        options["tools"] = builtin_tools("file_read")
        options["enforcement_strength"] = ENFORCEMENT_ON
    return await ctx.agent("Record the measurement.", label="reviewer", **options)
"""


@pytest.fixture
def named_workflow(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "measurement.py").write_text(_WORKFLOW)
    monkeypatch.setenv("OPENCOLLAB_WORKFLOWS_DIR", str(workflows))
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")

    def mock_replies(replies):
        requests = []

        async def handler(request):
            payload = json.loads(request.content)
            reply = replies[len(requests)]
            requests.append(payload)
            return _reply(bool(payload.get("stream")), *reply[:3], tool_calls=reply[3])

        install_sdk_transport(monkeypatch, "openai", handler)
        return requests

    return mock_replies


def _run_measurement(workspace, **inputs):
    return asyncio.run(OpenCollab(
        workspace, model="gpt-4o", provider="openai",
        api_key="stationary-fixture",  # pragma: allowlist secret
        base_url="https://measurement.invalid/v1",
        config={"llm_max_retries": 0},
    ).workflow(
        "measurement", inputs, budget=60_000, trace=False,
        artifacts=workspace / "artifacts", timeout=5,
    ))


@pytest.mark.parametrize("structured", [False, True])
def test_named_workflow_preserves_filtered_role_evidence(tmp_path, named_workflow, structured):
    requests = named_workflow([
        (_PARTIAL, None, "content_filter", [_SUBMIT] if structured else None),
        (None, None, "tool_calls", [_SUBMIT]),
    ])

    result = _run_measurement(tmp_path, structured=structured)

    assert result.ok
    assert result.output == (None if structured else _PARTIAL)
    assert result.agent_failures == (_FAILURE,)
    assert result.metrics["sessions"] == len(requests) == 1
    assert result.tokens == 20
    snapshot = OpenCollab.read_session_snapshot(tmp_path / "artifacts" / "000_reviewer.json")
    assert snapshot["session_state"]["terminal_reason"] == "provider filtered the output"
    assert not any(message.get("tool_calls") for message in snapshot["messages"])


@pytest.mark.parametrize("finish_reason", ["tool_calls", "content_filter"])
def test_named_workflow_corrects_a_normal_missing_capture(tmp_path, named_workflow, finish_reason):
    requests = named_workflow([
        ("I measured the duration.", None, "stop", None),
        (None, None, finish_reason, [_SUBMIT]),
    ])

    result = _run_measurement(tmp_path, structured=True)

    assert len(requests) == result.metrics["sessions"] == 2
    assert "I measured the duration." in json.dumps(requests[1]["messages"])
    if finish_reason == "content_filter":
        assert result.output is None
        assert result.agent_failures == (_FAILURE,)
    else:
        assert result.output == {"measured": True}
        assert result.agent_failures == ()


def test_cli_named_workflow_reports_filtered_role_without_a_correction(tmp_path, named_workflow):
    requests = named_workflow([
        (_PARTIAL, None, "content_filter", [_SUBMIT]),
        (None, None, "tool_calls", [_SUBMIT]),
    ])
    result = CliRunner().invoke(app, [
        "workflow", "run", "measurement", "--args", '{"structured":true}',
        "--workspace", str(tmp_path), "--model", "gpt-4o", "--provider", "openai",
        "--api-key", "stationary-fixture",  # pragma: allowlist secret
        "--base-url", "https://measurement.invalid/v1", "--no-trace",
    ])

    assert result.exit_code == 0, result.output
    assert "agent stopped (reviewer): provider filtered the output" in result.output
    assert json.loads(result.output.splitlines()[-1]) is None
    manifest_path, = (tmp_path / ".opencollab" / "sessions").glob("*/workflow.json")
    manifest = json.loads(manifest_path.read_text())
    assert len(requests) == manifest["sessions"] == 1
    assert manifest["tokens_spent"] == 20
    assert manifest["evidence_complete"] is True


def test_named_workflow_harvests_filtered_scout_without_a_synthesis_call(tmp_path, named_workflow):
    (tmp_path / "facts.txt").write_text("The measured time was 42 ms.\n")
    read = {"id": "read", "type": "function", "function": {
        "name": "file_read", "arguments": '{"path":"facts.txt"}',
    }}
    submit = {"id": "findings", "type": "function", "function": {
        "name": "submit_findings",
        "arguments": '{"summary":"A model report","findings":[],"insufficient_evidence":true}',
    }}
    requests = named_workflow([
        (None, None, "tool_calls", [read]),
        (_PARTIAL, None, "content_filter", [submit]),
        (None, None, "tool_calls", [submit]),
    ])

    result = _run_measurement(tmp_path, scout=True)

    assert "The measured time was 42 ms." in json.dumps(requests[1]["messages"])
    assert len(requests) == 2
    assert result.output == _PARTIAL
    assert result.agent_failures == (_FAILURE,)
    assert result.metrics["sessions"] == 1
