"""Single2 selection, behavior, and isolation on the OpenCollab 0.7 runtime."""

from __future__ import annotations

import asyncio
import copy

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.application.shaping import PerToolResultBudgetShaper
from opencollab.bootstrap import agent_runtime
from opencollab.bootstrap.agent_profiles import resolve_agent_profile
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT


class ReplyLLM:
    def __init__(self):
        self.calls = []

    def context_window(self):
        return 1_048_576

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools), kwargs))
        return LLMResponse(
            content="finished",
            finish_reason="stop",
            usage=Usage(4, 2),
        )


def test_single2_prompt_matches_the_evaluated_static_source():
    assert len(SINGLE2_SYSTEM_PROMPT) == 3_120
    assert "DO NOT MODIFY: Tests, configuration files" in SINGLE2_SYSTEM_PROMPT
    assert "Run project tests through `bash`." in SINGLE2_SYSTEM_PROMPT
    assert "The evaluator extracts the patch from the working tree" in SINGLE2_SYSTEM_PROMPT
    assert "Do NOT run `git commit`." in SINGLE2_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_default_and_single2_run_concurrently_without_profile_leaks(
    tmp_path,
    monkeypatch,
):
    sessions = {}
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions[kwargs["agent"].name] = session
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    default_llm = ReplyLLM()
    single2_llm = ReplyLLM()
    client = OpenCollab(
        tmp_path,
        model="unit-model",
        provider="openai",
        config={"budget": 200_000},
    )

    default, single2 = await asyncio.gather(
        client.agent("inspect", name="default-agent", llm=default_llm, trace=False),
        client.agent2("inspect", llm=single2_llm, trace=False),
    )

    assert default.ok and single2.ok
    assert "agent_profile" not in default.metrics
    assert single2.metrics["agent_profile"] == "single2"
    default_session = sessions["default-agent"]
    single2_session = sessions["single2"]
    assert [tool.name for tool in default_session.agent.tools] == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "git_diff",
        "grep",
    ]
    assert [tool.name for tool in single2_session.agent.tools] == [
        "bash",
        "file_read",
        "file_write",
        "apply_patch",
        "git_diff",
        "grep",
    ]
    assert default_session.agent.tools[0].require_process_isolation is False
    assert default_session.agent.tools[0].max_output_chars == 8_000
    assert single2_session.agent.tools[0].require_process_isolation is True
    assert single2_session.agent.tools[0].max_output_chars == 10_000
    assert default_session.max_steps == 100
    assert single2_session.max_steps == 200
    assert default_session.agent.system_prompt != SINGLE2_SYSTEM_PROMPT
    assert single2_session.agent.system_prompt == SINGLE2_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_single2_explicit_limits_survive_unbounded_environment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    sessions = []
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    client = OpenCollab(tmp_path, model="unit-model", provider="openai")
    result = await client.agent2(
        "inspect",
        budget=1_000_000_000_000,
        max_steps=1_000_000_000_000,
        llm=ReplyLLM(),
        trace=False,
    )

    assert result.ok
    assert len(sessions) == 1
    session = sessions[0]
    assert session.max_budget_tokens == 1_000_000_000_000
    assert session.max_steps == 1_000_000_000_000
    assert session.runner.max_budget_tokens == 1_000_000_000_000
    assert session.runner.max_steps == 1_000_000_000_000


@pytest.mark.asyncio
async def test_default_agent_keeps_unbounded_environment_semantics(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    sessions = []
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    result = await OpenCollab(
        tmp_path,
        model="unit-model",
        provider="openai",
    ).agent(
        "inspect",
        budget=1_000_000_000_000,
        max_steps=1_000_000_000_000,
        llm=ReplyLLM(),
        trace=False,
    )

    assert result.ok
    assert sessions[0].max_budget_tokens is None
    assert sessions[0].max_steps is None


def test_single2_shaper_preserves_the_tail_without_changing_default():
    value = "HEAD" + "a" * 25_000 + "LAST FAILURE"
    messages = [{"role": "tool", "tool_call_id": "c", "content": value}]

    default = PerToolResultBudgetShaper().shape(messages)[0]["content"]
    single2 = PerToolResultBudgetShaper(preserve_tail=True).shape(messages)[0][
        "content"
    ]

    assert "LAST FAILURE" not in default
    assert single2.startswith("HEAD")
    assert single2.endswith("LAST FAILURE")
    assert len(single2) == 16_000
    assert messages[0]["content"] == value


@pytest.mark.asyncio
async def test_invalid_profile_is_rejected_before_execution(tmp_path):
    client = OpenCollab(tmp_path)
    with pytest.raises(ValueError, match="profile"):
        await client.agent("test", profile="unknown")
    with pytest.raises(ValueError, match="profile"):
        await client.agent2("test", profile="default")


def test_profile_is_fresh_and_has_no_mutable_tool_instances():
    first = resolve_agent_profile("single2")
    second = resolve_agent_profile("single2")
    assert first is not second
    assert first is not None and second is not None
    first_tools = first.resolve_tools("coding")
    second_tools = second.resolve_tools("coding")
    assert all(left is not right for left, right in zip(first_tools, second_tools, strict=True))
