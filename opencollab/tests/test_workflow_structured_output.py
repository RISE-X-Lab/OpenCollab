"""Tests for workflow structured output (phase 2).

Covers the stdlib JSON-schema validator, the pure ``StructuredOutputTool``
(capture on valid / error-list on invalid), and ``WorkflowContext.agent``'s
``schema=`` path: returns a dict on capture, retries once then yields ``None``,
and counts retry-run tokens in the budget.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from opencollab.application.schema_validate import validate
from opencollab.application.structured_output import StructuredOutputTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.application.workflow import WorkflowContext


# --------------------------------------------------------------------------- #
# validator
# --------------------------------------------------------------------------- #


def test_validate_valid_object():
    schema = {
        "type": "object",
        "required": ["name", "age"],
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    assert validate({"name": "ada", "age": 36}, schema) == []


def test_validate_missing_required():
    schema = {"type": "object", "required": ["name"], "properties": {}}
    errors = validate({}, schema)
    assert errors
    assert any("name" in e for e in errors)


def test_validate_wrong_type():
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}}
    errors = validate({"age": "old"}, schema)
    assert errors
    assert any("age" in e for e in errors)


def test_validate_enum_violation():
    schema = {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}},
    }
    assert validate({"color": "red"}, schema) == []
    errors = validate({"color": "blue"}, schema)
    assert errors
    assert any("color" in e for e in errors)


def test_validate_nested_object_and_array():
    schema = {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "integer"}},
                },
            },
        },
    }
    assert validate({"items": [{"id": 1}, {"id": 2}]}, schema) == []
    errors = validate({"items": [{"id": 1}, {"id": "two"}]}, schema)
    assert errors
    # missing required inside a nested array item
    errors2 = validate({"items": [{}]}, schema)
    assert errors2


def test_validate_boolean_and_number():
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "ratio": {"type": "number"}},
    }
    assert validate({"ok": True, "ratio": 0.5}, schema) == []
    assert validate({"ok": True, "ratio": 3}, schema) == []  # int is a number
    assert validate({"ok": 1, "ratio": 0.5}, schema)  # bool!=int slot here


def test_validate_integer_rejects_bool():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    assert validate({"n": 5}, schema) == []
    assert validate({"n": True}, schema)


def test_validate_top_level_array():
    schema = {"type": "array", "items": {"type": "string"}}
    assert validate(["a", "b"], schema) == []
    assert validate(["a", 2], schema)


def test_validate_union_type_list():
    # JSON Schema permits ``type`` to be a list ("any of these"). A union type
    # must not raise (unhashable list) and must accept any listed member.
    schema = {
        "type": "object",
        "properties": {"x": {"type": ["string", "null"]}},
    }
    assert validate({"x": "hi"}, schema) == []
    assert validate({"x": None}, schema) == []
    # a value matching none of the union members is rejected, not crashed
    assert validate({"x": 7}, schema)


# --------------------------------------------------------------------------- #
# StructuredOutputTool
# --------------------------------------------------------------------------- #


def _runtime() -> ToolRuntime:
    return ToolRuntime(
        environment=None,
        safety_policy=None,
        permission_policy=None,
    )


@pytest.mark.asyncio
async def test_tool_captures_valid_payload():
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    tool = StructuredOutputTool(schema)
    assert tool.captured is None

    result = await tool.execute_with_runtime({"x": 7}, _runtime())

    assert tool.captured == {"x": 7}
    assert "x" in result.lower() or "record" in result.lower() or result


@pytest.mark.asyncio
async def test_tool_returns_errors_on_invalid_payload():
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    tool = StructuredOutputTool(schema)

    result = await tool.execute_with_runtime({"x": "nope"}, _runtime())

    assert tool.captured is None
    assert "x" in result  # error string mentions the offending field


@pytest.mark.asyncio
async def test_tool_schema_surface():
    schema = {"type": "object", "properties": {}}
    tool = StructuredOutputTool(schema)
    assert tool.name == "structured_output"
    assert isinstance(tool.description, str) and tool.description
    openai = tool.to_openai_schema()
    assert openai["function"]["name"] == "structured_output"
    # the tool's parameters expose the caller's schema as the input shape
    assert openai["function"]["parameters"] == schema


# --------------------------------------------------------------------------- #
# agent(schema=...)
# --------------------------------------------------------------------------- #


class FakeState:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []


class CapturingSession:
    """Session whose run_loop calls structured_output with a scripted payload.

    ``payloads`` is a list of payloads to feed on successive run_loop() calls.
    Each run_loop locates the injected StructuredOutputTool from ``tools`` and
    invokes it with the next payload, simulating the model self-correcting.

    Models the real ``Session.run_loop`` DONE short-circuit (session_run.py):
    once a turn finishes, a bare re-run does NOT re-invoke the tool — it just
    returns the prior answer. Only an intervening ``add_user_message`` (which
    resets DONE -> IDLE) lets the next run_loop produce a fresh payload. So a
    retry test fails if production forgets the corrective ``add_user_message``.
    """

    def __init__(self, tools: Sequence[Any], payloads: list[Any], tokens_each: int = 0) -> None:
        self._tools = list(tools)
        self._payloads = list(payloads)
        self._call = 0
        self._tokens_each = tokens_each
        self.state = FakeState()
        self.run_count = 0
        # True once a turn has finished; cleared by add_user_message.
        self._done = False

    def _structured_tool(self) -> StructuredOutputTool | None:
        for t in self._tools:
            if isinstance(t, StructuredOutputTool):
                return t
        return None

    async def add_user_message(self, content: str) -> None:
        self.state.messages.append({"role": "user", "content": content})
        self._done = False  # reset_for_user_turn -> resume_to_idle (DONE -> IDLE)

    async def run_loop(self) -> str:
        self.run_count += 1
        # DONE short-circuit: a re-run without an intervening user message
        # returns the prior answer without re-invoking any tool.
        if self._done:
            return "assistant text"
        tool = self._structured_tool()
        if tool is not None and self._call < len(self._payloads):
            payload = self._payloads[self._call]
            if payload is not _NO_CALL:
                await tool.execute_with_runtime(payload, _runtime())
        self._call += 1
        self._done = True
        return "assistant text"

    @property
    def used_tokens(self) -> int:
        return self._tokens_each * self.run_count


_NO_CALL = object()


class ScriptedFactory:
    """Builds a single CapturingSession, capturing the injected tools."""

    def __init__(self, payloads: list[Any], tokens_each: int = 0) -> None:
        self._payloads = payloads
        self._tokens_each = tokens_each
        self.session: CapturingSession | None = None
        self.builds: list[dict[str, Any]] = []

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
    ) -> CapturingSession:
        self.builds.append({"prompt": prompt, "tools": tools, "isolation": isolation})
        self.session = CapturingSession(tools or [], self._payloads, self._tokens_each)
        return self.session


SCHEMA = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}


@pytest.mark.asyncio
async def test_agent_schema_returns_dict_on_capture():
    factory = ScriptedFactory(payloads=[{"x": 42}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 42}
    # the structured-output tool was injected into the session toolset
    tools = factory.builds[0]["tools"]
    assert any(isinstance(t, StructuredOutputTool) for t in tools)
    # only one run_loop needed
    assert factory.session.run_count == 1


@pytest.mark.asyncio
async def test_agent_schema_retries_once_then_succeeds():
    # first run produces an invalid payload, second (after corrective msg) valid
    factory = ScriptedFactory(payloads=[{"x": "bad"}, {"x": 9}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 9}
    assert factory.session.run_count == 2
    # a corrective user message was appended before the retry
    user_msgs = [m for m in factory.session.state.messages if m["role"] == "user"]
    assert len(user_msgs) >= 2


@pytest.mark.asyncio
async def test_agent_schema_returns_none_after_failed_retry():
    factory = ScriptedFactory(payloads=[{"x": "bad"}, {"x": "still bad"}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result is None
    assert factory.session.run_count == 2  # exactly one retry


@pytest.mark.asyncio
async def test_agent_schema_no_call_at_all_returns_none():
    # model never calls structured_output on either run -> None after retry
    factory = ScriptedFactory(payloads=[_NO_CALL, _NO_CALL])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result is None
    assert factory.session.run_count == 2


@pytest.mark.asyncio
async def test_agent_schema_retry_tokens_counted_in_budget():
    # both runs invalid (tokens still spent), 100 tokens per run -> 200 total
    factory = ScriptedFactory(payloads=[{"x": "bad"}, {"x": "bad"}], tokens_each=100)
    ctx = WorkflowContext(factory, budget_total=10_000)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result is None
    # two run_loops -> 2 * 100 tokens counted
    assert ctx.budget.spent() == 200


@pytest.mark.asyncio
async def test_agent_schema_appends_instruction_to_prompt():
    factory = ScriptedFactory(payloads=[{"x": 1}])
    ctx = WorkflowContext(factory)

    await ctx.agent("base prompt", schema=SCHEMA)

    seeded = factory.builds[0]["prompt"]
    assert "base prompt" in seeded
    assert "structured_output" in seeded
