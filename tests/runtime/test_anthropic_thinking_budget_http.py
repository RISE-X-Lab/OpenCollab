"""Manual thinking stops gracefully when a finite session budget runs out."""

from __future__ import annotations

import json

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.tools.fs import FileReadTool
from opencollab.application.compaction_prompt import build_summary_request
from opencollab.bootstrap import container
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport

_THINKING = {"thinking": {"type": "enabled", "budget_tokens": 1024}}
_MODEL = "claude-sonnet-4-6"


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "false")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "every-step")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "on")


@pytest.mark.parametrize("budget,thinking,expected", [
    (7000, True, "completed"), (3800, True, "stopped"),
    (3000, True, "stopped"), (3800, False, "completed"),
])
async def test_public_agent_preserves_tool_results_when_thinking_budget_runs_out(
    monkeypatch, tmp_path, budget, thinking, expected,
):
    requests = []
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            content = []
            if thinking:
                content.append({"type": "thinking", "thinking": "Inspect the requested file.",
                                "signature": "mock-signature"})
            content.append({"type": "tool_use", "id": "call_read", "name": "file_read",
                            "input": {"path": "sample.txt"}})
            return http_response(request, 200, json={
                "id": "msg_first", "type": "message", "role": "assistant", "model": body["model"],
                "content": content, "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 300, "output_tokens": 1800},
            })
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    app = OpenCollab(
        tmp_path, provider="anthropic", model=_MODEL,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": thinking, "thinking_params": _THINKING,
                "max_output_tokens": 2048, "llm_max_retries": 0},
    )
    artifacts = tmp_path / "artifacts"
    result = await app.agent(
        "Read sample.txt and answer ok.", tools=(FileReadTool(),),
        system_prompt="Inspect the requested file and answer.",
        budget=budget, max_steps=2, trace=False, artifacts=artifacts,
    )

    assert result.status == expected
    assert result.error is None
    assert requests[0]["max_tokens"] == 2048
    assert app.configuration["thinking_params"] == _THINKING
    history = OpenCollab.read_session_snapshot(artifacts / "agent.json")["messages"]
    tool_result = next(message for message in history if message["role"] == "tool")
    assert tool_result["tool_call_id"] == "call_read"
    assert "alpha" in tool_result["content"]
    if expected == "stopped":
        assert len(requests) == 1
        assert result.tokens == 2100
        assert result.reason.startswith("budget exhausted before model call")
        assert "1025" in result.reason
    else:
        assert len(requests) == 2
        assert result.tokens == 2104
        if thinking:
            assert requests[1]["max_tokens"] == 2048
            assert requests[1]["thinking"] == _THINKING["thinking"]
        else:
            assert requests[1]["max_tokens"] < 1024


@pytest.mark.parametrize("output_budget,expected_calls", [(1024, 0), (1025, 1)])
async def test_manual_thinking_output_budget_boundary(monkeypatch, tmp_path, output_budget, expected_calls):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with LLMClient(
        provider="anthropic", model=_MODEL,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        messages = [{"role": "user", "content": "Return ok"}]
        budget = client.estimate_request_tokens(messages, thinking=True, thinking_params=_THINKING) + output_budget
        session = build_session(
            agent=Agent(name="boundary", system_prompt="system", model=_MODEL, provider="anthropic",
                        thinking=True, thinking_params=_THINKING, max_tokens_per_step=2048),
            llm=client, env=LocalEnvironment(str(tmp_path)), max_budget_tokens=budget,
        )
        session.messages = messages
        answer = await session.run_loop()
        assert len(requests) == expected_calls
        if expected_calls:
            assert answer == "ok"
            assert requests[0]["max_tokens"] == 1025
            assert requests[0]["thinking"] == _THINKING["thinking"]
        else:
            assert session.phase is SessionPhase.STOPPED
            assert session.used_tokens == 0


@pytest.mark.parametrize("budget", [500, 7000])
@pytest.mark.parametrize("params,limit,error", [
    (_THINKING, 1024, "budget_tokens must be less than max_output_tokens"),
    ({"thinking": {"type": "enabled", "budget_tokens": 800}}, 2048, "must be at least 1024"),
    ({"thinking": {"type": "enabled", "budget_tokens": 1024}, "extra": True}, 2048,
     "unsupported Anthropic thinking parameter"),
])
async def test_initial_invalid_thinking_configuration_remains_failed(
    monkeypatch, tmp_path, budget, params, limit, error,
):
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    app = OpenCollab(
        tmp_path, provider="anthropic", model=_MODEL,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": True, "thinking_params": params,
                "max_output_tokens": limit, "llm_max_retries": 0},
    )
    result = await app.agent("Return ok", tools=(), trace=False, budget=budget, max_steps=1)
    assert result.status == "failed"
    assert isinstance(result.error, ValueError)
    assert error in result.reason
    assert result.tokens == 0
    assert requests == []


async def test_summary_uses_ordinary_output_budget_for_manual_thinking_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request, text="<summary>Preserved context.</summary>")

    install_sdk_transport(monkeypatch, "anthropic", handler)
    async with LLMClient(
        provider="anthropic", model=_MODEL,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        agent = Agent(name="summary", system_prompt="system", model=_MODEL, provider="anthropic",
                      thinking=True, thinking_params=_THINKING, max_tokens_per_step=2048)
        session = build_session(agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)), max_budget_tokens=None)
        summarizer = container._build_summarizer(
            agent, client, client, 600.0, None, completion_handler=session.runner._invoke_summary,
        )
        prepared = build_summary_request([{"role": "user", "content": "Keep this context"}])
        session.runner.max_budget_tokens = client.estimate_request_tokens(prepared) + 128
        assert await summarizer.asummarize([{"role": "user", "content": "Keep this context"}]) == (
            "Summary:\nPreserved context."
        )
        assert session.used_tokens == 4
    assert requests[0]["max_tokens"] == 128
    assert "thinking" not in requests[0]


async def test_custom_client_without_output_requirement_keeps_thinking_kwargs(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    calls = []

    class CustomClient:
        async def complete(self, messages, tools=None, **kwargs):
            calls.append(kwargs)
            return LLMResponse(content="ok", finish_reason="stop", usage=Usage())

    messages = [{"role": "user", "content": "Return ok"}]
    session = build_session(
        agent=Agent(name="custom", system_prompt="system", thinking=True, thinking_params={"custom": True}),
        llm=CustomClient(), env=LocalEnvironment(str(tmp_path)),
        max_budget_tokens=estimate_request_tokens(messages) + 128,
    )
    session.messages = messages
    assert await session.run_loop() == "ok"
    assert calls[0]["max_output_tokens"] == 128
    assert calls[0]["thinking"] is True
    assert calls[0]["thinking_params"] == {"custom": True}


@pytest.mark.parametrize("output_budget", [127, 128])
async def test_custom_client_can_supply_its_output_requirement(monkeypatch, tmp_path, output_budget):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requirements = []
    calls = []

    class CustomClient:
        def minimum_output_tokens(self, **kwargs):
            requirements.append(kwargs)
            return 128

        async def complete(self, messages, tools=None, **kwargs):
            calls.append(kwargs)
            return LLMResponse(content="ok", finish_reason="stop", usage=Usage())

    messages = [{"role": "user", "content": "Return ok"}]
    session = build_session(
        agent=Agent(name="custom", system_prompt="system", thinking=True, thinking_params={"custom": True}),
        llm=CustomClient(), env=LocalEnvironment(str(tmp_path)),
        max_budget_tokens=estimate_request_tokens(messages) + output_budget,
    )
    session.messages = messages
    answer = await session.run_loop()
    assert requirements == [{"max_output_tokens": 8192, "thinking": True, "thinking_params": {"custom": True}}]
    if output_budget == 127:
        assert session.phase is SessionPhase.STOPPED
        assert calls == []
    else:
        assert answer == "ok"
        assert calls[0]["max_output_tokens"] == 128


async def test_adaptive_thinking_allows_a_small_output_budget(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        return completion_http_response(request)

    install_sdk_transport(monkeypatch, "anthropic", handler)
    app = OpenCollab(
        tmp_path, provider="anthropic", model=_MODEL,
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1",
        config={"thinking": True, "thinking_params": {"thinking": {"type": "adaptive"}},
                "max_output_tokens": 2048, "llm_max_retries": 0},
    )
    result = await app.agent("Return ok", tools=(), system_prompt="system", trace=False, budget=1100, max_steps=1)
    assert result.status == "completed"
    assert requests[0]["max_tokens"] < 1024
    assert requests[0]["thinking"] == {"type": "adaptive"}
