"""Profiled workflows retain role permissions, captures, and executable evidence."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys

import pytest

from opencollab import OpenCollab
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.single2_safety import Single2SafetyPolicy
from opencollab.bootstrap import _workflow_runtime_session as workflow_session
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.workflow_runtime import WORKFLOW_AGENT_PROMPT
from opencollab.tools import builtin_tools, profile_tool_limits

SCHEMA = {
    "type": "object",
    "properties": {"choice": {"type": "string", "enum": ["A", "B"]}},
    "required": ["choice"],
}


class _EvidenceBash:
    def __init__(self, tool):
        self.tool = tool
        self.records = []

    def __getattr__(self, name):
        return getattr(self.tool, name)

    @property
    def verified_targets(self):
        return frozenset(
            "test_probe.py" for _, result in self.records
            if "1 passed" in result and result.startswith("Exit code: 0")
        )

    async def execute_with_runtime(self, params, runtime):
        result = await self.tool.execute_with_runtime(params, runtime)
        self.records.append((dict(params), result))
        return result


class _RoleLLM:
    def __init__(self, tool_names, tool_choice):
        self.tool_names = tool_names
        self.tool_choice = tool_choice
        self.calls = []

    def context_window(self):
        return 1_048_576

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(tools)))
        call_name = None
        arguments = {}
        if "bash" in self.tool_names and len(self.calls) == 1:
            call_name = "bash"
            arguments = {
                "command": f"{shlex.quote(sys.executable)} -m pytest -q test_probe.py"
            }
        elif self.tool_choice is not None:
            call_name = "structured_output"
            arguments = {"choice": "A"}
        return LLMResponse(
            content=None if call_name else "finished",
            tool_calls=[] if call_name is None else [{
                "id": f"call-{len(self.calls)}",
                "type": "function",
                "function": {"name": call_name, "arguments": json.dumps(arguments)},
            }],
            finish_reason="tool_calls" if call_name else "stop",
            usage=Usage(4, 2),
        )


def _capture_sessions(monkeypatch):
    sessions = []
    original = workflow_session.build_session

    def build(**kwargs):
        agent = kwargs["agent"]
        llm = _RoleLLM([tool.name for tool in agent.tools], agent.tool_choice)
        session = original(**kwargs, llm=llm)
        sessions.append((session, kwargs["agent_profile"], llm))
        return session

    monkeypatch.setattr(workflow_session, "build_session", build)
    return sessions


@pytest.mark.asyncio
async def test_public_workflow_profiles_both_coders_and_structured_judge(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    (tmp_path / "test_probe.py").write_text("def test_probe():\n    assert 2 + 2 == 4\n")
    sessions = _capture_sessions(monkeypatch)
    role_tools = {}

    async def flow(ctx, inputs):
        for role in ("A", "B"):
            native = builtin_tools(
                "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
                headless=False,
            )
            role_tools[role] = [_EvidenceBash(native[0]), *native[1:]]
            assert await ctx.agent(f"Implement candidate {role}", tools=role_tools[role], label=role)
        return await ctx.agent("Choose candidate A or B", tools=[], schema=SCHEMA, label="judge")

    result = await OpenCollab(tmp_path, model="unit-model", provider="openai").workflow(
        flow, agent_profile="single2", system_prompt="Evaluation role context", trace=False,
    )

    assert result.ok and result.output == {"choice": "A"}
    assert result.metrics["agent_profile"] == "single2"
    assert len(sessions) == 4
    for session, profile, llm in sessions:
        assert profile.name == "single2"
        assert session.agent.system_prompt.startswith(SINGLE2_SYSTEM_PROMPT)
        assert "Evaluation role context" in session.agent.system_prompt
        assert "take precedence" in session.agent.system_prompt
        assert isinstance(session.tool_execution.safety_policy, Single2SafetyPolicy)
        with pytest.raises(PermissionError, match="container-wide"):
            session.tool_execution.safety_policy.check_cmd("find /")
        shaped = session.runner.shaper.shape([{
            "role": "tool", "tool_call_id": "result", "content": "HEAD" + "x" * 25_000 + "TAIL"
        }])
        assert shaped[0]["content"].endswith("TAIL")
        assert llm.calls
    for index, role in enumerate(("A", "B")):
        agent = sessions[index][0].agent
        assert all(actual is expected for actual, expected in zip(agent.tools, role_tools[role], strict=True))
        bash = role_tools[role][0]
        assert bash.max_output_chars == 10_000
        assert len(bash.records) == 1
        assert bash.verified_targets == frozenset({"test_probe.py"})
    judge, corrective = [entry[0].agent for entry in sessions[2:]]
    assert [tool.name for tool in judge.tools] == ["structured_output"]
    assert corrective.tools[0] is judge.tools[0]
    assert len(corrective.tools) == 1
    assert corrective.tool_choice == {"type": "function", "function": {"name": "structured_output"}}
    assert "structured submission requirement takes precedence" in judge.system_prompt
    assert "available tools are `structured_output`" in judge.system_prompt


@pytest.mark.asyncio
async def test_default_and_profiled_workflows_keep_separate_tool_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCOLLAB_UNBOUNDED_LIMITS", raising=False)
    sessions = _capture_sessions(monkeypatch)
    client = OpenCollab(tmp_path, model="unit-model", provider="openai")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def profiled(ctx, inputs):
        entered.set()
        await release.wait()
        return [
            builtin_tools("bash")[0].max_output_chars,
            builtin_tools("bash", limits={"bash": {"max_output_chars": 1234}})[0].max_output_chars,
        ]

    async def default(ctx, inputs):
        await entered.wait()
        value = builtin_tools("bash")[0].max_output_chars
        release.set()
        assert await ctx.agent("Read role context", tools=[])
        return value

    first, second = await asyncio.gather(
        client.workflow(profiled, agent_profile="single2", trace=False),
        client.workflow(default, trace=False),
    )

    assert first.ok and second.ok
    assert first.output == [10_000, 1234]
    assert second.output == 8_000
    assert "agent_profile" not in second.metrics
    assert builtin_tools("bash")[0].max_output_chars == 8_000
    session, profile, _ = sessions[0]
    assert profile is None
    assert session.agent.system_prompt == WORKFLOW_AGENT_PROMPT
    assert session.agent.tools == []
    assert session.max_steps == 100
    assert session.tool_execution.safety_policy is None
    shaped = session.runner.shaper.shape([{
        "role": "tool", "tool_call_id": "result", "content": "HEAD" + "x" * 25_000 + "TAIL"
    }])
    assert not shaped[0]["content"].endswith("TAIL")


@pytest.mark.asyncio
async def test_explicit_workflow_tool_objects_keep_their_configuration(tmp_path, monkeypatch):
    sessions = _capture_sessions(monkeypatch)
    explicit = builtin_tools("file_read")

    async def flow(ctx, inputs):
        return await ctx.agent("Read task files", tools=explicit)

    result = await OpenCollab(tmp_path, model="unit-model", provider="openai").workflow(
        flow, agent_profile="single2", trace=False,
    )

    assert result.ok
    assert sessions[0][0].agent.tools == list(explicit)
    assert sessions[0][0].agent.tools[0] is explicit[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "single2"])
async def test_corrective_model_input_retains_current_single_tool_permissions(tmp_path, monkeypatch, profile):
    sessions = _capture_sessions(monkeypatch)

    async def flow(ctx, inputs):
        return await ctx.agent(
            "Read context and choose a candidate", tools=builtin_tools("file_read"),
            schema=SCHEMA, label="reader",
        )

    result = await OpenCollab(tmp_path, model="unit-model", provider="openai").workflow(
        flow, agent_profile=profile, trace=False,
    )

    assert result.ok and result.output == {"choice": "A"}
    assert len(sessions) == 2
    first_messages = sessions[0][2].calls[0][0]
    corrective_messages = sessions[1][2].calls[0][0]
    first_system = [message for message in first_messages if message["role"] == "system"]
    corrective_system = [message for message in corrective_messages if message["role"] == "system"]
    if profile is None:
        assert first_system == corrective_system == [{"role": "system", "content": WORKFLOW_AGENT_PROMPT}]
    else:
        assert "available tools are `structured_output`, `file_read`" in first_system[0]["content"]
        assert "available tools are `structured_output`." in corrective_system[0]["content"]
        assert "available tools are `structured_output`, `file_read`" not in corrective_system[0]["content"]
    assert any(message.get("content") == "finished" for message in corrective_messages)


@pytest.mark.asyncio
async def test_invalid_workflow_profile_is_rejected_before_execution(tmp_path):
    async def flow(ctx, inputs):
        raise AssertionError("workflow must not execute")

    with pytest.raises(ValueError, match="profile"):
        await OpenCollab(tmp_path).workflow(flow, agent_profile="unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [None, "single2"])
async def test_profile_selection_keeps_workflow_unbounded_limits(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    sessions = _capture_sessions(monkeypatch)

    async def flow(ctx, inputs):
        assert ctx.budget.total is None
        return await ctx.agent("Read task context", tools=[])

    result = await OpenCollab(tmp_path, model="unit-model", provider="openai").workflow(
        flow, agent_profile=profile, budget=123, max_steps=9, trace=False,
    )

    assert result.ok
    assert sessions[0][0].max_budget_tokens is None
    assert sessions[0][0].max_steps is None


@pytest.mark.asyncio
async def test_profile_tool_defaults_restore_after_workflow_failure(tmp_path):
    async def flow(ctx, inputs):
        assert builtin_tools("bash")[0].max_output_chars == 10_000
        raise RuntimeError("workflow failed")

    result = await OpenCollab(tmp_path).workflow(flow, agent_profile="single2", trace=False)

    assert result.status == "failed"
    assert result.metrics["agent_profile"] == "single2"
    assert builtin_tools("bash")[0].max_output_chars == 8_000


def test_public_profile_tool_defaults_are_independent():
    defaults = profile_tool_limits("single2")
    assert defaults == {"bash": {"max_output_chars": 10_000}}
    defaults["bash"]["max_output_chars"] = 1
    assert profile_tool_limits("single2") == {"bash": {"max_output_chars": 10_000}}
    assert profile_tool_limits(None) == profile_tool_limits("default") == {}
