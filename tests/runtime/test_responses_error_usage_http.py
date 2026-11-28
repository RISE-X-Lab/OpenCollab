"""Real SDK and Session accounting for reported Responses failure costs."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm import client as client_module
from opencollab.adapters.llm import retry
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.responses_errors import ResponsesProtocolError, ResponsesTransientEventError
from opencollab.adapters.llm.responses_provider import _wait_for_transport
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.trace import Tracer
from opencollab.application.session_run import GenerationTimeoutError
from opencollab.application.structured_output import StructuredOutputTool
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.runtime.test_async_compaction import history
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import http_response, install_sdk_transport, provider_http
from tests.support.responses_provider_test_support import function_item, message_item

KNOWN_USAGE = {
    "input_tokens": 30, "output_tokens": 7, "total_tokens": 37,
    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0},
}
STRUCTURED_SCHEMA = {
    "type": "object", "properties": {"answer": {"type": "integer"}},
    "required": ["answer"], "additionalProperties": False,
}

def _response(body, spec):
    kind = spec.get("kind", "answer")
    items = [] if kind in {"filter", "empty", "failed"} else [message_item(spec.get("text", "ok"))]
    if "function_arguments" in spec:
        items = [function_item("call_usage", "structured_output", spec["function_arguments"])]
    return {
        "id": "resp_usage_test", "object": "response", "created_at": 1,
        "model": spec.get("provider_model", body["model"]),
        "instructions": body.get("instructions"),
        "status": "incomplete" if kind == "filter" else "failed" if kind == "failed" else "completed",
        "error": (
            {"code": "server_error", "message": "An internal server error occurred."} if kind == "failed" else None
        ),
        "incomplete_details": {"reason": "content_filter"} if kind == "filter" else None,
        "output": items, "usage": spec.get("usage", KNOWN_USAGE),
    }

def _payload(body, spec):
    response = _response(body, spec)
    if not body["stream"]:
        return "application/json", json.dumps(response).encode()
    events = [{"type": "response.output_item.done", "sequence_number": i,
               "output_index": i, "item": item} for i, item in enumerate(response["output"])]
    terminal = {"completed": "response.completed", "failed": "response.failed", "incomplete": "response.incomplete"}
    events.append({"type": terminal[response["status"]], "sequence_number": len(events), "response": response})
    return "text/event-stream", "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()

@pytest.fixture
def local_responses_server():
    servers = []

    def start(specs):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                spec = specs[min(len(requests) - 1, len(specs) - 1)]
                content_type, payload = _payload(body, spec)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/v1", requests

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()

@pytest.fixture
def usage_log(monkeypatch, tmp_path):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(path))
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry.random, "uniform", lambda _lo, _hi: 0.0)
    return path

def _records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]

@pytest.fixture
def trajectory(tmp_path):
    tracer = Tracer("error-usage", str(tmp_path / "trace"))
    try:
        yield tracer
    finally:
        tracer.close()

def _llm_records(tracer):
    tracer.flush()
    return [record for record in _records(tracer.path) if record["type"] == "llm_call"]

def _client(model, base_url, *, retries=0):
    return LLMClient(
        model=model, wire_protocol="responses", api_key="controlled-local",  # pragma: allowlist secret
        base_url=base_url, max_retries=retries, context_window=40000,
        first_event_timeout=None, request_timeout=None,
    )

def _agent(model="gpt-4o"):
    return Agent(name="reader", system_prompt="Reply briefly", model=model, wire_protocol="responses")

async def test_ordinary_success_usage_is_charged_once(
    local_environment_factory, local_responses_server, usage_log, trajectory,
):
    base_url, requests = local_responses_server([{}])
    async with _client("gpt-4o", base_url) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client, tracer=trajectory)
        await session.add_user_message("Give a short answer")
        assert await session.run_loop() == "ok"
    assert session.phase is SessionPhase.DONE
    assert session.used_tokens == 37
    assert session.state.context_tokens == 30
    assert len(requests) == 1
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["usage"]["total_tokens"] == 37
    assert records[0]["usage"]["raw_usage"] == KNOWN_USAGE
    calls = _llm_records(trajectory)
    assert len(calls) == 1
    assert calls[0]["payload"]["usage"]["total_tokens"] == 37
    assert "status" not in calls[0]["payload"]

@pytest.mark.parametrize("model", ["gpt-4o", "o1-pro"])
@pytest.mark.parametrize("kind", ["filter", "failed"])
async def test_reported_terminal_error_usage_reaches_session_and_ledger(
    local_environment_factory,
    model, kind, local_responses_server, usage_log, trajectory,
):
    reported = {
        **KNOWN_USAGE, "input_tokens_details": {"cached_tokens": 5},
        "output_tokens_details": {"reasoning_tokens": 2},
    }
    provider_model = f"{model}-provider-revision"
    base_url, requests = local_responses_server([{"kind": kind, "usage": reported, "provider_model": provider_model}])
    async with _client(model, base_url) as client:
        session = build_session(
            env=local_environment_factory(),
            agent=_agent(model), llm=client, tracer=trajectory, aid=7)
        await session.add_user_message("Give a short answer")
        with pytest.raises(ResponsesProtocolError) as captured:
            await session.run_loop()
    assert session.phase is SessionPhase.ERROR
    assert session.used_tokens == 37
    assert len(requests) == 1
    assert requests[0]["stream"] is (model == "gpt-4o")
    if kind == "failed":
        assert captured.value.code == "server_error"
    else:
        assert "content_filter" in str(captured.value)
    assert getattr(captured.value, "provider_model", None) == provider_model
    assert captured.value.usage.raw_usage == reported
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["usage"]["total_tokens"] == 37
    assert records[0]["usage"]["estimated"] is False
    calls = _llm_records(trajectory)
    assert len(calls) == 1
    payload = calls[0]["payload"]
    assert payload["status"] == "error"
    assert payload["error"] == {"type": type(captured.value).__name__, "message": str(captured.value)}
    assert payload["purpose"] == "completion"
    assert (payload["aid"], payload["session_step"], payload["model"]) == (7, 1, model)
    assert payload["provider_model"] == provider_model
    assert payload["wire_protocol"] == "responses"
    assert payload["usage"] == {
        "input_tokens": 30, "output_tokens": 7, "total_tokens": 37,
        "cache_read_tokens": 5, "cache_creation_tokens": None, "uncached_input_tokens": None,
        "estimated": False, "reasoning_tokens": 2, "raw_usage": reported,
    }
    assert calls[0]["metrics"]["tokens"] == 37

@pytest.mark.parametrize("kind", ["filter", "failed"])
async def test_terminal_error_without_model_keeps_provider_identity_unknown(monkeypatch, usage_log, kind):
    def respond(request):
        content_type, body = _payload(json.loads(request.content), {"kind": kind, "provider_model": None})
        return http_response(request, 200, headers={"content-type": content_type}, content=body)

    install_sdk_transport(monkeypatch, "openai", respond)
    async with _client("gpt-4o", "https://fixture.invalid/v1") as client:
        with pytest.raises(ResponsesProtocolError) as captured:
            await client.complete([{"role": "user", "content": "Report the measurement."}])
    assert getattr(captured.value, "provider_model", None) is None
    assert captured.value.usage.total_tokens == 37

@pytest.mark.parametrize("model", ["gpt-4o", "o1-pro"])
async def test_invalid_function_json_retains_terminal_usage(model, local_responses_server, usage_log):
    base_url, requests = local_responses_server([{"function_arguments": '{"answer":4'}])
    async with _client(model, base_url, retries=1) as client:
        with pytest.raises(ResponsesProtocolError, match="invalid JSON") as captured:
            await client.complete(
                [{"role": "user", "content": "Return answer four"}],
                tools=[StructuredOutputTool(STRUCTURED_SCHEMA).to_openai_schema()],
            )
    assert len(requests) == 1
    assert requests[0]["stream"] is (model == "gpt-4o")
    assert requests[0]["tools"][0]["name"] == "structured_output"
    assert captured.value.usage.raw_usage == KNOWN_USAGE
    assert captured.value.usage.total_tokens == 37
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "ResponsesProtocolError"
    assert records[0]["usage"]["total_tokens"] == 37
    assert records[0]["usage"]["estimated"] is False

@pytest.mark.parametrize("model", ["gpt-4o", "o1-pro"])
@pytest.mark.parametrize("malformed", [False, True])
async def test_public_structured_correction_accounts_for_every_request(
    model, malformed, local_responses_server, usage_log, tmp_path,
):
    specs = [{"function_arguments": '{"answer":4' if malformed else '{"answer":4}'}]
    if malformed:
        specs.append({"function_arguments": '{"answer":4}', "usage": {
            "input_tokens": 20, "output_tokens": 3, "total_tokens": 23,
        }})
    base_url, requests = local_responses_server(specs)
    snapshots = []

    async def flow(ctx, _args):
        value = await ctx.agent("Return answer four", schema=STRUCTURED_SCHEMA, tools=[])
        snapshots.extend((session.phase, session.used_tokens) for session in ctx.sessions)
        return {"value": value, "tokens_spent": ctx.tokens_spent()}

    artifacts = tmp_path / "artifacts"
    client = OpenCollab(
        tmp_path, provider="openai", model=model,
        api_key="controlled-local",  # pragma: allowlist secret
        base_url=base_url, config={"wire_protocol": "responses", "llm_max_retries": 0},
    )
    result = await client.workflow(
        flow, artifacts=artifacts, trace=False, budget=10_000, max_steps=4,
        concurrency=1, limit_mode="explicit",
    )
    expected_tokens = 60 if malformed else 37
    assert result.status == "completed"
    assert result.output == {"value": {"answer": 4}, "tokens_spent": expected_tokens}
    assert result.tokens == expected_tokens
    assert len(requests) == (2 if malformed else 1)
    assert all(request["stream"] is (model == "gpt-4o") for request in requests)
    assert all(request["tools"][0]["name"] == "structured_output" for request in requests)
    assert snapshots == (
        [(SessionPhase.ERROR, 37), (SessionPhase.STOPPED, 23)]
        if malformed else [(SessionPhase.STOPPED, 37)]
    )
    persisted = json.loads((artifacts / "workflow.json").read_text())
    assert persisted["tokens_spent"] == expected_tokens
    records = _records(usage_log)
    assert [record["status"] for record in records] == (["error", "success"] if malformed else ["success"])
    assert [record["usage"]["total_tokens"] for record in records] == ([37, 23] if malformed else [37])
    assert all(record["usage"]["estimated"] is False for record in records)

@pytest.mark.parametrize("model", ["gpt-4o", "o1-pro"])
@pytest.mark.parametrize("first_kind", ["empty", "failed"])
@pytest.mark.parametrize("recover", [False, True])
async def test_retry_usage_is_charged_once_with_final_request_context(
    local_environment_factory,
    model, first_kind, recover, local_responses_server, usage_log,
):
    specs = [{"kind": first_kind}, {"kind": "answer" if recover else first_kind}]
    base_url, requests = local_responses_server(specs)
    async with _client(model, base_url, retries=1) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(model), llm=client)
        await session.add_user_message("Give a short answer")
        if recover:
            assert await session.run_loop() == "ok"
            assert session.phase is SessionPhase.DONE
            assert session.state.context_tokens == 30
        else:
            with pytest.raises(ResponsesProtocolError):
                await session.run_loop()
            assert session.phase is SessionPhase.ERROR
    assert len(requests) == 2
    assert session.used_tokens == 74
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == ("success" if recover else "error")
    assert records[0]["usage"]["input_tokens"] == 60
    assert records[0]["usage"]["output_tokens"] == 14
    assert records[0]["usage"]["total_tokens"] == 74
    assert records[0]["usage"]["raw_usage"]["attempts"] == [KNOWN_USAGE, KNOWN_USAGE]

async def test_retry_cost_can_exhaust_session_budget(local_environment_factory, local_responses_server, usage_log):
    usage = {**KNOWN_USAGE, "input_tokens": 600, "output_tokens": 300, "total_tokens": 900}
    base_url, requests = local_responses_server([{"kind": "empty", "usage": usage}, {"usage": usage}])
    async with _client("gpt-4o", base_url, retries=1) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client, max_budget_tokens=1200)
        await session.add_user_message("Give a short answer")
        await session.run_loop()
    assert len(requests) == 2
    assert session.phase is SessionPhase.STOPPED
    assert session.used_tokens == 1800
    assert session.state.context_tokens == 600
    assert "budget exceeded after model call" in session.state.terminal_reason
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["usage"]["total_tokens"] == 1800

@pytest.mark.parametrize("recover", [False, True])
async def test_unknown_failure_usage_is_retained_as_unknown(
    local_environment_factory, recover, local_responses_server, usage_log, trajectory,
):
    specs = [{"kind": "empty" if recover else "filter", "usage": None}]
    if recover:
        specs.append({})
    base_url, requests = local_responses_server(specs)
    async with _client("gpt-4o", base_url, retries=int(recover)) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client, tracer=trajectory)
        await session.add_user_message("Give a short answer")
        if recover:
            assert await session.run_loop() == "ok"
        else:
            with pytest.raises(ResponsesProtocolError):
                await session.run_loop()
    assert len(requests) == (2 if recover else 1)
    assert session.used_tokens == (37 if recover else 0)
    record = _records(usage_log)[0]
    assert record["usage"]["estimated"] is True
    assert record["usage"]["cached_input_tokens"] is None
    assert record["usage"]["cost_usd"] is None
    if recover:
        assert record["usage"]["raw_usage"]["attempts"] == [None, KNOWN_USAGE]
    assert len(_llm_records(trajectory)) == int(recover)

@pytest.mark.parametrize("reported", [{"input_tokens": 30}, {"input_tokens": 30, "output_tokens": 0}])
async def test_partial_and_zero_failure_counts_use_only_reported_tokens(
    local_environment_factory, reported, local_responses_server, usage_log,
):
    base_url, _requests = local_responses_server([{"kind": "filter", "usage": reported}])
    async with _client("gpt-4o", base_url) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client)
        await session.add_user_message("Give a short answer")
        with pytest.raises(ResponsesProtocolError):
            await session.run_loop()
    assert session.used_tokens == 30
    record = _records(usage_log)[0]
    assert record["usage"]["raw_usage"] == reported
    assert record["usage"]["output_tokens"] == 0
    assert record["usage"]["estimated"] is ("output_tokens" not in reported)

@pytest.mark.parametrize("failed_summary", [False, True])
async def test_summary_attempts_and_answer_are_each_charged_once(
    local_environment_factory,
    failed_summary, local_responses_server, usage_log, trajectory,
):
    specs = [{"kind": "filter"}] if failed_summary else [
        {"kind": "empty"}, {"text": "<summary>Earlier context.</summary>"},
    ]
    specs.append({"text": "answer"})
    base_url, requests = local_responses_server(specs)
    async with _client("gpt-4o", base_url, retries=1) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client, tracer=trajectory)
        session.messages = history()
        assert await session.run_loop() == "answer"
    assert len(requests) == (2 if failed_summary else 3)
    assert "Your task is to create a detailed summary" in requests[0]["input"][-1]["content"]
    assert session.used_tokens == (74 if failed_summary else 111)
    assert session.state.context_tokens == 30
    records = _records(usage_log)
    assert len(records) == 2
    assert [record["usage"]["total_tokens"] for record in records] == ([37, 37] if failed_summary else [74, 37])
    calls = _llm_records(trajectory)
    assert len(calls) == 2
    assert sum(call["payload"]["usage"]["total_tokens"] for call in calls) == session.used_tokens
    assert calls[0]["payload"]["purpose"] == "summary"
    assert calls[0]["payload"]["request_tool_names"] == []
    assert calls[0]["payload"]["request_tool_choice"] is None
    assert calls[0]["payload"].get("status") == ("error" if failed_summary else None)

async def _drain(session):
    while session.pending_cleanup_tasks:
        await asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True)
        await asyncio.sleep(0)

@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("retry_budget", [0.0, 15.0])
async def test_direct_responses_completion_cancellation_keeps_reported_usage(
    monkeypatch, tmp_path, usage_log, cancel, retry_budget,
):
    started = asyncio.Event()
    interrupted = asyncio.Event()
    release = asyncio.Event()
    requests, reported = [], []
    cancellations = []

    async def handler(request):
        requests.append(request.url.path)
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
        content_type, payload = _payload(json.loads(request.content), {"text": "completed response"})
        return http_response(request, 200, headers={"content-type": content_type}, content=payload)

    install_sdk_transport(monkeypatch, "openai", handler)
    oc = OpenCollab(
        tmp_path, model="gpt-4o", provider="openai", api_key=tmp_path.name, base_url="https://offline.invalid/v1",
        config={
            "wire_protocol": "responses",
            "llm_timeout": 8.0,
            "llm_max_retries": 0,
            "provider_error_time_budget": retry_budget,
        },
    )
    async with oc.create_model_client() as client:
        messages = [{"role": "user", "content": "Reply briefly"}]

        async def invoke():
            try:
                return await client.complete(messages, on_usage=reported.append)
            except asyncio.CancelledError as exc:
                cancellations.append((exc.args, exc.usage.total_tokens))
                raise

        call = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(started.wait(), 5)
            if cancel:
                call.cancel("caller moved on")
                await asyncio.wait_for(interrupted.wait(), 5)
            release.set()
            done, _ = await asyncio.wait({call}, timeout=5)
            assert call in done
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await call
            else:
                assert (await call).content == "completed response"
        finally:
            release.set()
            await asyncio.gather(call, return_exceptions=True)

    assert requests == ["/v1/responses"]
    assert cancellations == ([(("caller moved on",), 37)] if cancel else [])
    assert [usage.total_tokens for usage in reported] == [37]
    record, = _records(usage_log)
    assert record["status"] == "success"
    assert record["usage"]["total_tokens"] == 37

async def test_completed_transport_reports_simultaneous_caller_cancellation():
    interruptions = []

    async def transport():
        call.cancel("caller moved on")
        return "completed"

    call = asyncio.create_task(_wait_for_transport(transport(), None, on_interrupt=interruptions.append))
    assert await call == "completed"
    interruption, = interruptions
    assert isinstance(interruption, asyncio.CancelledError)
    assert interruption.args == ("caller moved on",)

@pytest.mark.parametrize("kind", ["answer", "failed"])
async def test_direct_responses_cancellation_races_with_terminal_result(monkeypatch, tmp_path, usage_log, kind):
    requests, reported, cancellations = [], [], []

    def cancel_after_terminal(_task):
        cancellations.append(caller.cancel("caller moved on"))

    async def handler(request):
        requests.append(request.url.path)
        assert asyncio.current_task() is not caller
        asyncio.current_task().add_done_callback(cancel_after_terminal)
        content_type, payload = _payload(json.loads(request.content), {"kind": kind})
        return http_response(request, 200, headers={"content-type": content_type}, content=payload)

    install_sdk_transport(monkeypatch, "openai", handler)
    async with LLMClient(
        model="gpt-4o", wire_protocol="responses", api_key=tmp_path.name,
        base_url="https://offline.invalid/v1", max_retries=0, request_timeout=8.0,
        first_event_timeout=None, stream_idle_timeout=None,
    ) as client:
        async def invoke():
            try:
                return await client.complete([{"role": "user", "content": "Reply briefly"}], on_usage=reported.append)
            except asyncio.CancelledError as exc:
                assert exc.args == ("caller moved on",)
                assert exc.usage.total_tokens == 37
                if kind == "failed":
                    assert isinstance(exc.__cause__, ResponsesTransientEventError)
                raise

        caller = asyncio.create_task(invoke())
        with pytest.raises(asyncio.CancelledError):
            await caller

    assert cancellations == [True]
    assert requests == ["/v1/responses"]
    assert [usage.total_tokens for usage in reported] == [37]
    record, = _records(usage_log)
    assert record["usage"]["total_tokens"] == 37
    assert record["status"] == ("error" if kind == "failed" else "success")
    if kind == "failed":
        assert record["error"]["type"] == "ResponsesTransientEventError"

@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize(("trigger", "session_timeout"), [
    pytest.param("cancel", 600, id="cancel-with-deadline"),
    pytest.param("cancel", None, id="cancel-without-deadline"),
    pytest.param("timeout", 0.01, id="timeout"),
])
@pytest.mark.parametrize("kind", ["filter", "answer"])
@pytest.mark.parametrize("repeat_cancel", [False, True])
async def test_late_sdk_terminal_usage_is_saved_once(
    local_environment_factory,
    monkeypatch, tmp_path, usage_log, summary, trigger, session_timeout, kind, repeat_cancel, trajectory,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        content_type, payload = _payload(json.loads(request.content), {"kind": kind})
        return httpx.Response(200, headers={"content-type": content_type}, content=payload)

    install_sdk_transport(monkeypatch, "openai", handler)
    snapshot_path = tmp_path / "late.json"
    async with _client("gpt-4o", "http://controlled.invalid/v1") as client:
        session = build_session(
            env=local_environment_factory(),
            agent=_agent(), llm=client, llm_timeout=session_timeout,
            auto_save_path=str(snapshot_path), tracer=trajectory,
        )
        if summary:
            session.messages = history()
        else:
            await session.add_user_message("Give a short answer")
        session.state.wind_down_done = True
        session.runner._commit_reserve = session.max_budget_tokens - 30
        turn = asyncio.create_task(session.run_loop())
        try:
            await asyncio.wait_for(started.wait(), 1)
            if trigger == "cancel":
                turn.cancel()
                expected_error = asyncio.CancelledError
            else:
                expected_error = GenerationTimeoutError
            done, _ = await asyncio.wait({turn}, timeout=1)
            assert turn in done
            with pytest.raises(expected_error):
                await turn
            await asyncio.wait_for(cancelled.wait(), 1)
            assert session.used_tokens == 0
            if repeat_cancel:
                cancelled.clear()
                session.runner.pending_cleanup_tasks[0].cancel()
                await asyncio.wait_for(cancelled.wait(), 1)
            original_messages = session.messages.copy()
            release.set()
            await asyncio.wait_for(_drain(session), 1)
            assert session.used_tokens == 37
            assert session.runner.late_provider_usage == (37,)
            assert session.state.budget_reserve_consumed is True
            assert session.messages == original_messages
            assert session.phase is (SessionPhase.STOPPED if trigger == "cancel" else SessionPhase.ERROR)
            snapshot = SessionStore().load_snapshot(str(snapshot_path), session.agent.system_prompt)
            assert snapshot["session_state"]["used_tokens"] == 37
            records = _records(usage_log)
            assert len(records) == 1
            assert records[0]["usage"]["total_tokens"] == 37
            if kind == "filter":
                calls = _llm_records(trajectory)
                assert len(calls) == 1
                payload = calls[0]["payload"]
                assert payload["status"] == "error"
                assert payload["purpose"] == ("summary" if summary else "completion")
                assert payload["late"] is True
                assert payload["usage"]["total_tokens"] == 37
        finally:
            release.set()
            await asyncio.gather(turn, return_exceptions=True)
            await _drain(session)

@pytest.mark.parametrize("trace", [False, True])
async def test_error_trace_preserves_original_exception_and_persisted_usage(
    local_environment_factory, tmp_path, monkeypatch, trace, trajectory,
):
    monkeypatch.delenv("OPENCOLLAB_API_USAGE_LOG", raising=False)
    error = RuntimeError("provider failure with billed usage")
    error.usage = SimpleNamespace(input_tokens=30, output_tokens=7, total_tokens=37, raw_usage=KNOWN_USAGE)
    error.provider_model = "provider-confirmed-model"
    error.transport_timing = {"first_event_s": 0.01}
    failed_calls = []
    log_step = trajectory.log_step

    def fail_error_write(step_type, payload, *args, **kwargs):
        if step_type == "llm_call":
            failed_calls.append(payload)
            raise OSError("trace storage unavailable")
        return log_step(step_type, payload, *args, **kwargs)

    monkeypatch.setattr(trajectory, "log_step", fail_error_write)

    class Provider:
        async def complete(self, **kwargs):
            raise error

    snapshot_path = tmp_path / "error.json"
    session = build_session(
        env=local_environment_factory(),
        agent=_agent(), llm=Provider(), tracer=trajectory if trace else None,
        auto_save_path=str(snapshot_path),
    )
    await session.add_user_message("Give a short answer")
    with pytest.raises(RuntimeError) as captured:
        await session.run_loop()
    assert captured.value is error
    assert session.used_tokens == 37
    snapshot = SessionStore().load_snapshot(str(snapshot_path), session.agent.system_prompt)
    assert snapshot["session_state"]["used_tokens"] == 37
    assert len(failed_calls) == int(trace)
    if trace:
        assert failed_calls[0]["status"] == "error"
        assert failed_calls[0]["provider_model"] == error.provider_model
        assert failed_calls[0]["transport_timing"] == error.transport_timing
        assert failed_calls[0]["usage"]["raw_usage"] == KNOWN_USAGE

async def test_cancellation_during_retry_retains_previous_terminal_usage(
    local_environment_factory,
    monkeypatch, local_responses_server, usage_log,
):
    backoff_started = asyncio.Event()

    def retry_after(_error):
        backoff_started.set()
        return 60.0

    monkeypatch.setattr(retry, "extract_retry_after_seconds", retry_after)
    base_url, requests = local_responses_server([{"kind": "empty"}])
    async with _client("gpt-4o", base_url, retries=1) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client)
        await session.add_user_message("Give a short answer")
        turn = asyncio.create_task(session.run_loop())
        await asyncio.wait_for(backoff_started.wait(), 1)
        assert session.used_tokens == 37
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn, 1)
        await asyncio.wait_for(_drain(session), 1)
    assert len(requests) == 1
    assert session.used_tokens == 37
    assert session.runner.late_provider_usage == (37,)
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == "error"
    assert records[0]["error"]["type"] == "CancelledError"
    assert records[0]["usage"]["total_tokens"] == 37

@pytest.mark.parametrize("trigger", ["cancel", "deadline"])
@pytest.mark.parametrize("retry_time_budget", [0.0, 30.0])
@pytest.mark.parametrize(("stage", "late_kind"), [
    ("headers", "answer"), ("first_event", "answer"), ("close", "answer"), ("close", "failed"),
])
async def test_round_timeout_retains_late_usage_after_retries(
    local_environment_factory, monkeypatch, tmp_path, usage_log, trigger, retry_time_budget, stage, late_kind,
):
    blocked = asyncio.Event()
    sdk_cancelled = asyncio.Event()
    release = asyncio.Event()
    prior_spend = []
    reported_usage = [(21, 10), (28, 13), (42, 11), (47, 15)]
    http = provider_http("openai")

    async def wait_for_release():
        blocked.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                sdk_cancelled.set()

    async def handler(request):
        index = len(prior_spend)
        prior_spend.append(session.used_tokens)
        input_tokens, output_tokens = reported_usage[index]
        kind = "failed" if index < 2 else late_kind if index == 2 else "answer"
        content_type, payload = _payload(json.loads(request.content), {
            "kind": kind,
            "usage": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        })
        if index != 2:
            return http_response(request, 200, headers={"content-type": content_type}, content=payload)
        if stage == "headers":
            await wait_for_release()
            return http_response(request, 200, headers={"content-type": content_type}, content=payload)

        class LateStream(http.AsyncByteStream):
            async def __aiter__(self):
                if stage == "first_event":
                    await wait_for_release()
                yield payload

            async def aclose(self):
                if stage == "close":
                    await wait_for_release()

        return http_response(request, 200, headers={"content-type": content_type}, stream=LateStream())

    install_sdk_transport(monkeypatch, "openai", handler)
    snapshot_path = tmp_path / "late-round.json"
    async with LLMClient(
        model="gpt-4o", wire_protocol="responses", api_key="controlled-local",  # pragma: allowlist secret
        base_url="http://controlled.invalid/v1", max_retries=3, request_timeout=10.0,
        provider_error_time_budget=retry_time_budget, context_window=40000,
    ) as client:
        session = build_session(
            env=local_environment_factory(), agent=_agent(), llm=client,
            llm_timeout=0.75 if trigger == "deadline" else 10.0, auto_save_path=str(snapshot_path),
        )
        await session.add_user_message("Give a short answer")
        turn = asyncio.create_task(session.run_loop())
        try:
            await asyncio.wait_for(blocked.wait(), 5)
            if trigger == "cancel":
                turn.cancel()
            expected_error = asyncio.CancelledError if trigger == "cancel" else GenerationTimeoutError
            with pytest.raises(expected_error):
                await asyncio.wait_for(turn, 5)
            await asyncio.wait_for(sdk_cancelled.wait(), 5)
            assert session.used_tokens == 72
            stopped_messages = session.messages.copy()
            owner, = session.runner.pending_cleanup_tasks
            owner.cancel()
            await asyncio.sleep(0)
            assert not owner.done()
            release.set()
            await asyncio.wait_for(_drain(session), 5)
        finally:
            release.set()
            await asyncio.gather(turn, return_exceptions=True)
            await _drain(session)

    assert prior_spend == [0, 31, 72]
    assert session.messages == stopped_messages
    assert session.phase is (SessionPhase.STOPPED if trigger == "cancel" else SessionPhase.ERROR)
    assert session.used_tokens == 125
    assert session.runner.late_provider_usage == (125,)
    snapshot = SessionStore().load_snapshot(str(snapshot_path), session.agent.system_prompt)
    assert snapshot["session_state"]["used_tokens"] == 125
    record, = _records(usage_log)
    assert record["status"] == ("success" if late_kind == "answer" else "error")
    if late_kind == "failed":
        assert record["error"]["type"] == "CancelledError"
    assert record["transport_timing"]["attempts"] == 3
    assert record["usage"]["total_tokens"] == 125
    assert [attempt["total_tokens"] for attempt in record["usage"]["raw_usage"]["attempts"]] == [31, 41, 53]

@pytest.mark.parametrize("kind", ["filter", "answer"])
async def test_cancellation_during_ledger_write_does_not_duplicate_usage(
    local_environment_factory,
    monkeypatch, local_responses_server, usage_log, kind,
):
    started = threading.Event()
    release = threading.Event()
    original_record = client_module.record_api_usage

    def delayed_record(**kwargs):
        started.set()
        assert release.wait(2)
        original_record(**kwargs)

    monkeypatch.setattr(client_module, "record_api_usage", delayed_record)
    base_url, requests = local_responses_server([{"kind": kind}])
    async with _client("gpt-4o", base_url) as client:
        session = build_session(env=local_environment_factory(), agent=_agent(), llm=client)
        await session.add_user_message("Give a short answer")
        turn = asyncio.create_task(session.run_loop())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(turn, 1)
            release.set()
            await asyncio.wait_for(_drain(session), 1)
        finally:
            release.set()
            await asyncio.gather(turn, return_exceptions=True)
            await _drain(session)
    assert len(requests) == 1
    assert session.phase is SessionPhase.STOPPED
    assert session.used_tokens == 37
    assert session.runner.late_provider_usage == (37,)
    records = _records(usage_log)
    assert len(records) == 1
    assert records[0]["status"] == ("success" if kind == "answer" else "error")
    assert records[0]["usage"]["total_tokens"] == 37
