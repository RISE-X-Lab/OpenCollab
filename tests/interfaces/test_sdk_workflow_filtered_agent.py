from __future__ import annotations

import asyncio
import json
import time

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli.main import app
from opencollab.bootstrap.workflow_runtime import run_workflow
from tests.runtime.test_llm_chat_refusal_http import _reply
from tests.support.provider_sdk_http import http_response, install_sdk_transport
from tests.support.responses_provider_test_support import function_item, message_item
from tests.support.workflow_runtime_test_support import _cfg

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
def workflow_dir(tmp_path, monkeypatch):
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "measurement.py").write_text(_WORKFLOW)
    monkeypatch.setenv("OPENCOLLAB_WORKFLOWS_DIR", str(workflows))
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    return workflows


@pytest.fixture
def named_workflow(monkeypatch, workflow_dir):
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


def _run_measurement(workspace, *, wire_protocol="chat_completions", **inputs):
    return asyncio.run(OpenCollab(
        workspace, model="gpt-4o", provider="openai",
        api_key="stationary-fixture",  # pragma: allowlist secret
        base_url="https://measurement.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": wire_protocol},
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


@pytest.mark.parametrize("filtered", [True, False], ids=["filtered", "normal"])
def test_cli_named_workflow_reports_filtered_role_without_a_correction(tmp_path, named_workflow, filtered):
    requests = named_workflow([
        (_PARTIAL, None, "content_filter", [_SUBMIT]) if filtered else (None, None, "tool_calls", [_SUBMIT]),
        (None, None, "tool_calls", [_SUBMIT]),
    ])
    result = CliRunner().invoke(app, [
        "workflow", "run", "measurement", "--args", '{"structured":true}',
        "--workspace", str(tmp_path), "--model", "gpt-4o", "--provider", "openai",
        "--api-key", "stationary-fixture",  # pragma: allowlist secret
        "--base-url", "https://measurement.invalid/v1", "--no-trace",
    ])

    assert result.exit_code == 0, result.output
    notice = "agent stopped (reviewer): provider filtered the output"
    assert result.output.count(notice) == int(filtered)
    expected = None if filtered else {"measured": True}
    json_lines = result.output.splitlines()
    json_start = next(index for index, line in enumerate(json_lines) if line in {"null", "{"})
    assert json.loads("\n".join(json_lines[json_start:])) == expected
    manifest_path, = (tmp_path / ".opencollab" / "sessions").glob("*/workflow.json")
    manifest = json.loads(manifest_path.read_text())
    assert len(requests) == manifest["sessions"] == 1
    assert manifest["tokens_spent"] == 20
    assert manifest["evidence_complete"] is True


@pytest.mark.parametrize("finish_reason", ["content_filter", "stop"])
async def test_workflow_progress_sink_does_not_delay_a_queued_role(tmp_path, monkeypatch, finish_reason):
    first_request_started = asyncio.Event()
    second_role_queued = asyncio.Event()
    release_notice = asyncio.Event()
    requests = []

    class ProgressSink:
        async def emit(self, event):
            if getattr(event, "kind", None) == "log" and "provider filtered the output" in event.message:
                await release_notice.wait()

    async def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            first_request_started.set()
            await second_role_queued.wait()
            return _reply(False, _PARTIAL, None, finish_reason)
        return _reply(False, "Independent measurement completed.", None, "stop")

    async def measurement(ctx, _inputs):
        async def sibling():
            await first_request_started.wait()
            second_role_queued.set()
            return await ctx.agent("Verify the measurement.", label="sibling", tools=[], budget=10_000)

        return await asyncio.gather(
            ctx.agent("Record the measurement.", label="reviewer", tools=[], budget=10_000),
            sibling(),
        )

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "")
    try:
        result = await run_workflow(
            measurement, {},
            cfg=_cfg(provider="openai", model="gpt-4o", thinking=False, llm_max_retries=0),
            workspace=str(tmp_path), trace=False, return_details=True,
            max_concurrency=1, event_sink=ProgressSink(), deadline_monotonic=time.monotonic() + 2,
        )
    finally:
        release_notice.set()

    assert result.output == [_PARTIAL, "Independent measurement completed."]
    assert result.agent_failures == ((_FAILURE,) if finish_reason == "content_filter" else ())
    assert len(requests) == result.sessions == 2
    assert result.tokens == 40


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


@pytest.fixture
def responses_workflow(monkeypatch, workflow_dir):
    def install(replies):
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            assert request.url.path.endswith("/responses") and payload["stream"]
            requests.append(payload)
            reply = replies[len(requests) - 1]
            response = {
                "id": f"measurement-{len(requests)}", "object": "response", "model": payload["model"],
                "status": "completed", "error": None, "incomplete_details": None, "output": [],
                "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
                **reply,
            }
            events = [
                {"type": "response.output_item.done", "output_index": index, "item": item}
                for index, item in enumerate(response["output"])
            ]
            events.append({"type": f"response.{response['status']}", "response": response})
            frames = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            return http_response(request, 200, headers={"Content-Type": "text/event-stream"}, text=frames)

        install_sdk_transport(monkeypatch, "openai", handler)
        return requests

    return install


_RESPONSES_FILTER = {
    "status": "incomplete", "incomplete_details": {"reason": "content_filter"},
    "output": [message_item(_PARTIAL)],
}
_RESPONSES_OTHER_ERROR = {
    "status": "failed", "error": {"code": "invalid_prompt", "message": "Unexpected content_filter option."},
}
_RESPONSES_FAILURE = {**_FAILURE, "exception_type": "ResponsesTerminalEventError"}
_RESPONSES_OTHER_FAILURE = {**_RESPONSES_FAILURE, "provider_error_type": None}


@pytest.mark.parametrize("filtered", [True, False], ids=["filtered", "other-error"])
def test_responses_workflow_corrects_only_the_non_filter_failure(tmp_path, responses_workflow, filtered):
    capture = function_item("capture", "structured_output", '{"measured":true}')
    requests = responses_workflow([
        _RESPONSES_FILTER if filtered else _RESPONSES_OTHER_ERROR,
        {"output": [capture]},
    ])
    result = _run_measurement(tmp_path, wire_protocol="responses", structured=True)

    assert result.ok
    assert len(requests) == result.metrics["sessions"] == (1 if filtered else 2)
    assert result.tokens == (20 if filtered else 40)
    assert result.output == (None if filtered else {"measured": True})
    expected_failure = _RESPONSES_FAILURE if filtered else _RESPONSES_OTHER_FAILURE
    assert result.agent_failures == (expected_failure,)
    snapshot = OpenCollab.read_session_snapshot(tmp_path / "artifacts" / "000_reviewer.json")
    assert snapshot["session_state"]["phase"] == "error"
    assert [message["role"] for message in snapshot["messages"]] == ["system", "user"]


@pytest.mark.parametrize("filtered", [True, False], ids=["filtered", "other-error"])
def test_responses_workflow_synthesizes_scout_only_after_non_filter_failure(tmp_path, responses_workflow, filtered):
    (tmp_path / "facts.txt").write_text("The measured time was 42 ms.\n")
    read = function_item("read", "file_read", '{"path":"facts.txt"}')
    summary = "The available log covers a single measurement."
    synthesize = function_item("report", "submit_findings", json.dumps({
        "summary": summary, "findings": [], "insufficient_evidence": True,
    }))
    requests = responses_workflow([
        {"output": [read]},
        _RESPONSES_FILTER if filtered else _RESPONSES_OTHER_ERROR,
        {"output": [synthesize]},
    ])

    result = _run_measurement(tmp_path, wire_protocol="responses", scout=True)

    assert len(requests) == (2 if filtered else 3)
    assert result.metrics["sessions"] == (1 if filtered else 2)
    assert result.tokens == (40 if filtered else 60)
    assert ("The measured time was 42 ms." if filtered else summary) in result.output
    assert _PARTIAL not in result.output
    expected_failure = _RESPONSES_FAILURE if filtered else _RESPONSES_OTHER_FAILURE
    assert result.agent_failures == (expected_failure,)
