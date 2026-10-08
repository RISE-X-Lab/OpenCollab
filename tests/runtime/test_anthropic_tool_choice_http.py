"""Anthropic tool-choice rejection recovery through real SDK sessions."""

from __future__ import annotations

import json

import anthropic
import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.session_characterization_test_support import FakeTool

_MODEL = "claude-opus-5-5"
_CHOICE_ERROR = 'tool_choice: type "tool" and "any" are not supported for this model.'
_NAMED_CHOICE = {"type": "function", "function": {"name": "submit_findings"}}


def _error_response(request, message=_CHOICE_ERROR, **fields):
    return http_response(request, 400, json={
        "type": "error", "error": {"type": "invalid_request_error", "message": message, **fields},
    })


def _agent(*, tool_choice=None):
    return Agent(
        name="worker", system_prompt="Read and finish the task.",
        tools=[FakeTool("read_info"), FakeTool("submit_findings")],
        model=_MODEL, provider="anthropic", thinking=True,
        thinking_params={"thinking": {"type": "adaptive"}}, tool_choice=tool_choice,
    )


def _client():
    return LLMClient(
        model=_MODEL, provider="anthropic", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    )


@pytest.fixture(autouse=True)
def isolate_usage_log(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))


@pytest.mark.parametrize("tool_choice", [_NAMED_CHOICE, "required"], ids=["named", "required"])
async def test_documented_rejection_retries_named_and_required_choices_once(monkeypatch, tmp_path, tool_choice):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if body.get("tool_choice", {}).get("type") != "auto":
            return _error_response(request)
        return completion_http_response(request, output_tokens=7)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with _client() as client:
        session = build_session(agent=_agent(tool_choice=tool_choice), llm=client, max_budget_tokens=50_000)
        await session.add_user_message("Finish the task.")

        assert await session.run_loop() == "ok"

    assert [body["tool_choice"]["type"] for body in requests] == [
        "tool" if tool_choice == _NAMED_CHOICE else "any", "auto",
    ]
    assert session.phase is SessionPhase.DONE
    assert session.used_tokens == 9
    assert session.state.budget_reserve_consumed is False
    ledger = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [row["status"] for row in ledger] == ["error", "success"]
    assert [row["usage"]["total_tokens"] for row in ledger] == [0, 9]


@pytest.mark.parametrize("fallback_fails", [False, True], ids=["recovered", "second-error"])
async def test_budget_wind_down_recovers_documented_rejection_and_preserves_second_error(
    monkeypatch, tmp_path, fallback_fails,
):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return http_response(request, 200, json={
                "id": "msg_read", "type": "message", "role": "assistant", "model": _MODEL,
                "content": [{"type": "tool_use", "id": "toolu_read", "name": "read_info", "input": {}}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 30_000, "output_tokens": 2},
            })
        if body.get("tool_choice", {}).get("type") == "tool":
            return _error_response(request)
        if fallback_fails:
            return _error_response(request, "Invalid tool input schema", param="tools")
        return completion_http_response(request, output_tokens=7)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, max_budget_tokens=50_000)
        session.runner.configure_enforcement(enforcement_strength="needs-enforcement")
        await session.add_user_message("Read and finish the task.")

        if fallback_fails:
            with pytest.raises(anthropic.BadRequestError) as captured:
                await session.run_loop()
            assert captured.value.body["error"]["message"] == _CHOICE_ERROR
            assert isinstance(captured.value.__cause__, anthropic.BadRequestError)
            assert captured.value.__cause__.body["error"]["param"] == "tools"
            if hasattr(captured.value, "add_note"):
                assert any("Retrying with tool_choice='auto' also failed" in note for note in captured.value.__notes__)
            assert session.phase is SessionPhase.ERROR
        else:
            assert await session.run_loop() == "ok"
            assert session.phase is SessionPhase.DONE

    assert len(requests) == 3
    assert requests[1]["tool_choice"] == {"type": "tool", "name": "submit_findings"}
    assert requests[2]["tool_choice"] == {"type": "auto"}
    assert all(body["thinking"] == {"type": "adaptive"} for body in requests)
    assert session.state.wind_down_done is True
    assert session.state.wind_down_attempts == 1
    assert session.state.budget_reserve_consumed is True
    assert session.used_tokens == (30_002 if fallback_fails else 30_011)
    ledger = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [row["status"] for row in ledger] == ["success", "error", "error" if fallback_fails else "success"]
    assert sum(row["usage"]["total_tokens"] for row in ledger) == session.used_tokens


@pytest.mark.parametrize("param", ["temperature", "tools", "messages"])
async def test_explicit_unrelated_parameter_prevents_tool_choice_retry(monkeypatch, param):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _error_response(request, param=param)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with _client() as client:
        session = build_session(agent=_agent(tool_choice="required"), llm=client)
        await session.add_user_message("Finish the task.")

        with pytest.raises(anthropic.BadRequestError) as captured:
            await session.run_loop()

    assert captured.value.body["error"]["param"] == param
    assert len(requests) == 1
    assert requests[0]["tool_choice"] == {"type": "any"}
    assert session.phase is SessionPhase.ERROR
    assert session.used_tokens == 0


@pytest.mark.parametrize("message", [
    'tool_choice: type "tool" and "any" were requested; invalid temperature',
    'Invalid tools schema; request included tool_choice type "tool" and "any"',
])
async def test_unrelated_validation_message_with_choice_echo_does_not_retry(monkeypatch, message):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return _error_response(request, message)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with _client() as client:
        session = build_session(agent=_agent(tool_choice="required"), llm=client)
        await session.add_user_message("Finish the task.")
        with pytest.raises(anthropic.BadRequestError):
            await session.run_loop()

    assert len(requests) == 1
    assert session.used_tokens == 0
