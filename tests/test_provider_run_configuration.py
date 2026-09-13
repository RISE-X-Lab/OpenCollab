"""Configured provider behavior and durable request lifecycle observations."""

from __future__ import annotations

import asyncio

import pytest
from responses_provider_test_support import completed_response, message_item
from session_run_loop_test_support import FakeTracer, build_runner

from opencollab import OpenCollab
from opencollab.adapters.llm.errors import TransientProviderError
from opencollab.adapters.llm.responses_provider import parse_responses_response
from opencollab.adapters.llm.retry import with_retry
from opencollab.application.workflow_candidates import _candidate_budget_total
from opencollab.bootstrap import inspection


def test_output_limit_retains_content_usage_and_output_limit_disposition():
    response = completed_response(
        output=[message_item("partial but useful answer")],
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    response.usage = {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
    result = parse_responses_response(response, [], expected_model="gpt-fake")
    assert result.content == "partial but useful answer"
    assert result.finish_reason == "max_tokens"
    assert result.provider_model == response.model
    assert result.usage.total_tokens > 0


@pytest.mark.parametrize("enabled,expected", [("0", 120), ("1", None), ("true", None)])
def test_explicit_unbounded_candidate_budget(monkeypatch, enabled, expected):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", enabled)
    assert _candidate_budget_total(120) == expected


@pytest.mark.asyncio
async def test_uncapped_transient_retry_remains_cancellable_and_delay_is_bounded(monkeypatch):
    delays = []
    calls = 0

    async def sleep(delay):
        delays.append(delay)

    async def request():
        nonlocal calls
        calls += 1
        if calls <= 75:
            raise TransientProviderError("temporary provider failure")
        return "complete"

    monkeypatch.setattr("opencollab.adapters.llm.retry.asyncio.sleep", sleep)
    assert await with_retry(request, max_retries=None) == "complete"
    assert len(delays) == 75
    assert max(delays) <= 60.25

    async def cancelled_request():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await with_retry(cancelled_request, max_retries=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_cancel_trace", [False, True])
async def test_cancelled_model_request_has_matching_start_and_cancel_records(fail_cancel_trace):
    entered = asyncio.Event()

    class WaitingModel:
        async def complete(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    class Tracer(FakeTracer):
        def log_step(self, **kwargs):
            if fail_cancel_trace and kwargs["step_type"] == "llm_call_cancelled":
                raise OSError("trace storage unavailable")
            return super().log_step(**kwargs)

    tracer = Tracer()
    runner = build_runner(llm=WaitingModel(), tracer=tracer)
    task = asyncio.create_task(runner.run_loop())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lifecycle = [step for step in tracer.steps if step["step_type"].startswith("llm_call")]
    if fail_cancel_trace:
        assert [step["step_type"] for step in lifecycle] == ["llm_call_started"]
        return
    assert [step["step_type"] for step in lifecycle] == ["llm_call_started", "llm_call_cancelled"]
    assert lifecycle[0]["payload"]["response_session_id"] == lifecycle[1]["payload"]["response_session_id"]
    assert lifecycle[0]["payload"]["session_step"] == lifecycle[1]["payload"]["session_step"] == 1


def test_public_model_client_uses_effective_transport_configuration(monkeypatch, tmp_path):
    captured = []
    monkeypatch.setattr(inspection, "LLMClient", lambda **kwargs: captured.append(kwargs) or object())
    client = OpenCollab(
        tmp_path,
        model="gpt-fake",
        provider="openai",
        api_key="fake-key-for-test",  # pragma: allowlist secret
        base_url="https://example.test/v1",
        config={
            "context_window": 200000,
            "llm_max_retries": 9,
            "llm_timeout": 1234,
            "provider_error_time_budget": 5678,
            "wire_protocol": "responses",
        },
    )
    client.create_model_client()
    assert captured[0]["context_window"] == 200000
    assert captured[0]["max_retries"] == 9
    assert captured[0]["request_timeout"] == 1234
    assert captured[0]["provider_error_time_budget"] == 5678
    assert captured[0]["wire_protocol"] == "responses"
    assert client.configuration["context_window"] == 200000
    assert "api_key" not in client.configuration


def test_public_snapshot_reader_delegates_journal_replay(monkeypatch, tmp_path):
    captured = []

    class Store:
        def load_snapshot(self, path, password):
            captured.append((path, password))
            return {"session_state": {"step_count": 3}}

    monkeypatch.setattr(inspection, "SessionStore", Store)
    path = tmp_path / "session.json"
    assert OpenCollab.read_session_snapshot(path)["session_state"]["step_count"] == 3
    assert captured == [(str(path), "")]
