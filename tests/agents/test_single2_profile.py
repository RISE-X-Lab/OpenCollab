"""Single2 selection, behavior, and isolation on the OpenCollab 0.7 runtime."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.single2_safety import Single2SafetyPolicy
from opencollab.application.shaping import PerToolResultBudgetShaper
from opencollab.bootstrap import agent_profiles, agent_runtime, programmatic
from opencollab.bootstrap.agent_profiles import resolve_agent_profile
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.profiles import BASE_PROFILE, resolve_profile_name
from opencollab.tools import builtin_tools


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
async def test_default_base_and_single2_use_the_same_model_configuration(
    tmp_path,
    monkeypatch,
):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    sessions = {}
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions[kwargs["agent"].name] = session
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    client = OpenCollab(
        tmp_path,
        model="unit-model",
        provider="openai",
        config={"budget": 200_000},
    )

    variants = [
        ("omitted", {}),
        ("base", {"profile": "base"}),
        ("default", {"profile": "default"}),
        ("single", {"profile": "single"}),
        ("single2", {"profile": "single2"}),
    ]
    llms = [ReplyLLM() for _ in variants]
    alias_llm = ReplyLLM()
    results = await asyncio.gather(
        *(
            client.agent("inspect", name=name, llm=llm, trace=False, **kwargs)
            for (name, kwargs), llm in zip(variants, llms, strict=True)
        ),
        client.agent2("inspect", name="agent2-alias", llm=alias_llm, trace=False),
    )

    assert len(sessions) == len(results) == 6
    for result in results:
        assert result.ok
        assert result.metrics["agent_profile"] == "single2"
    for llm in [*llms, alias_llm]:
        assert len(llm.calls) == 1
        assert llm.calls == llms[0].calls
    for session in sessions.values():
        assert [tool.name for tool in session.agent.tools] == [
            "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
        ]
        assert session.agent.tools[0].require_process_isolation is True
        assert session.agent.tools[0].max_output_chars == 10_000
        assert session.max_steps == 200
        assert session.agent.system_prompt == SINGLE2_SYSTEM_PROMPT
        assert isinstance(session.tool_execution.safety_policy, Single2SafetyPolicy)
        with pytest.raises(PermissionError, match="container-wide"):
            session.tool_execution.safety_policy.check_cmd("find /")
        shaped = session.runner.shaper.shape([{
            "role": "tool", "tool_call_id": "result", "content": "HEAD" + "x" * 25_000 + "TAIL"
        }])
        assert shaped[0]["content"].endswith("TAIL")
    assert all(
        actual is not other
        for actual, other in zip(sessions["omitted"].agent.tools, sessions["single2"].agent.tools, strict=True)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "base", "single2"])
async def test_selected_profile_explicit_limits_survive_unbounded_environment(
    tmp_path,
    monkeypatch,
    profile,
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
    result = await client.agent(
        "inspect",
        profile=profile,
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
        llm=ReplyLLM(),
        trace=False,
    )

    assert result.ok
    assert sessions[0].max_budget_tokens is None
    assert sessions[0].max_steps is None


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "base", "single2"])
async def test_explicit_tools_and_system_prompt_customize_selected_profile(tmp_path, monkeypatch, profile):
    sessions = []
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    tools = builtin_tools("file_read")
    llm = ReplyLLM()
    result = await OpenCollab(tmp_path, model="unit-model", provider="openai").agent(
        "inspect", profile=profile, tools=tools, system_prompt="Follow this task-specific instruction",
        llm=llm, trace=False,
    )

    assert result.ok
    assert sessions[0].agent.tools == list(tools)
    assert sessions[0].agent.tools[0] is tools[0]
    assert llm.calls[0][0][0] == {"role": "system", "content": "Follow this task-specific instruction"}
    assert [tool["function"]["name"] for tool in llm.calls[0][1]] == ["file_read"]


@pytest.mark.asyncio
async def test_programmatic_standalone_omitted_profile_uses_base(tmp_path, monkeypatch):
    sessions = []
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    result = await programmatic.run_agent(
        prompt="inspect", config={"model": "unit-model", "provider": "openai"}, workspace=str(tmp_path),
        tools="coding", max_tokens=100_000, max_steps=200, timeout=None, cleanup_timeout=2.0,
        artifacts=None, trace=False, llm=ReplyLLM(),
    )

    assert result.status == "completed"
    assert result.metrics["agent_profile"] == "single2"
    assert sessions[0].agent.system_prompt == SINGLE2_SYSTEM_PROMPT
    assert sessions[0].agent.tools[0].require_process_isolation is True


@pytest.mark.parametrize("name", [None, "base", " Base ", "default", "single", "single2", "Single2"])
def test_public_profile_name_resolves_to_the_base_implementation(name):
    assert BASE_PROFILE == "single2"
    assert resolve_profile_name(name) == "single2"


@pytest.mark.parametrize("name", ["unknown", "", 1, False])
def test_public_profile_name_rejects_invalid_names(name):
    with pytest.raises(ValueError, match="profile"):
        resolve_profile_name(name)


@pytest.mark.asyncio
async def test_new_named_factory_and_base_mapping_select_the_new_implementation(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    single2 = resolve_agent_profile("single2")
    created = []

    def build_future_profile():
        profile = replace(
            single2, name="future", system_prompt="Future single-agent implementation", default_steps=7,
        )
        created.append(profile)
        return profile

    monkeypatch.setitem(agent_profiles._PROFILE_FACTORIES, "future", build_future_profile)
    monkeypatch.setattr(agent_profiles, "BASE_PROFILE", "future")
    client = OpenCollab(tmp_path, model="unit-model", provider="openai")
    sessions = []
    original = agent_runtime.build_session

    def capture(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", capture)
    selected_llm = ReplyLLM()
    explicit_llm = ReplyLLM()
    selected = await client.agent("inspect", tools=(), llm=selected_llm, trace=False)
    explicit = await client.agent("inspect", profile="future", tools=(), llm=explicit_llm, trace=False)
    retained = await client.agent("inspect", profile="single2", tools=(), llm=ReplyLLM(), trace=False)

    assert selected.ok and explicit.ok and retained.ok
    assert selected.metrics["agent_profile"] == explicit.metrics["agent_profile"] == "future"
    assert retained.metrics["agent_profile"] == "single2"
    assert sessions[0].max_steps == sessions[1].max_steps == 7
    assert sessions[2].max_steps == 200
    assert selected_llm.calls == explicit_llm.calls
    assert selected_llm.calls[0][0][0]["content"] == "Future single-agent implementation"
    assert len(created) == 2 and created[0] is not created[1]
    assert all(resolve_profile_name(name) == "future" for name in (None, "base", "default", "single"))
    assert resolve_profile_name("single2") == "single2"


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
