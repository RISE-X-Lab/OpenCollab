"""Single-agent runtime limits and provider configuration propagation."""

from __future__ import annotations

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import agent_runtime


class ReplyLLM:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content="finished",
            usage=Usage(input_tokens=4, output_tokens=2),
            finish_reason="stop",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("unbounded", [False, True])
async def test_agent_configuration_reaches_real_session(monkeypatch, tmp_path, unbounded):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", str(unbounded).lower())
    original = agent_runtime.build_session
    sessions = []

    def capture_session(**kwargs):
        session = original(**kwargs)
        if unbounded:
            session.state.used_tokens = 9_000_000
            session.state.step_count = 1_000
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture_session)
    llm = ReplyLLM()
    result = await OpenCollab(
        tmp_path,
        model="test-model",
        provider="openai",
        config={
            "wire_protocol": "responses",
            "reasoning_effort": "max",
            "thinking": True,
            "context_window": 1_048_576,
            "max_output_tokens": 65_536,
            "llm_connect_timeout": 60.0,
            "llm_first_event_timeout": 600.0,
            "llm_stream_idle_timeout": 600.0,
            "llm_max_retries": 30,
            "provider_error_time_budget": 7200.0,
        },
    ).agent("finish once", budget=100_000, max_steps=4, tools=(), llm=llm)

    assert result.ok
    assert len(sessions) == len(llm.calls) == 1
    session = sessions[0]
    assert session.max_budget_tokens == (None if unbounded else 100_000)
    assert session.max_steps == (None if unbounded else 4)
    assert result.tokens == (9_000_006 if unbounded else 6)
    assert result.metrics["steps"] == (1_001 if unbounded else 1)
    assert session.agent.wire_protocol == "responses"
    assert session.agent.reasoning_effort == "max"
    assert session.agent.thinking is True
    assert session.agent.context_window == 1_048_576
    assert session.agent.llm_connect_timeout == 60.0
    assert session.agent.llm_first_event_timeout == 600.0
    assert session.agent.llm_stream_idle_timeout == 600.0
    assert session.agent.llm_max_retries == 30
    assert session.agent.provider_error_time_budget == 7200.0
    assert llm.calls[0]["max_output_tokens"] == 65_536
    assert llm.calls[0]["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_agent_rejects_invalid_unbounded_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "invalid")
    with pytest.raises(ValueError, match="OPENCOLLAB_UNBOUNDED_LIMITS"):
        await OpenCollab(tmp_path).agent("finish once", llm=ReplyLLM())


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"budget": 0}, {"budget": True}, {"max_steps": -1}, {"steps": False}])
async def test_unbounded_switch_preserves_argument_validation(monkeypatch, tmp_path, kwargs):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    llm = ReplyLLM()
    with pytest.raises(ValueError):
        await OpenCollab(tmp_path).agent("finish once", llm=llm, **kwargs)
    assert llm.calls == []
