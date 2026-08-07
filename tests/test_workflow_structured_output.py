"""Tests for workflow structured output (phase 2).

Covers the stdlib JSON-schema validator, the pure ``StructuredOutputTool``
(capture on valid / error-list on invalid), and ``WorkflowContext.agent``'s
``schema=`` path: a free-exploration first pass, a forced-commit corrective
pass (a fresh single-tool session pinned to a named-function ``tool_choice``)
when the first pass answers in free text, returns a dict on capture, yields
``None`` when both passes miss, and counts both passes' tokens in the budget.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

import pytest

from opencollab.application.schema_validate import validate, validate_schema
from opencollab.application.structured_output import StructuredOutputTool
from opencollab.application.tool_execution import ToolRuntime
from opencollab.application.workflow import (
    DEFAULT_DEADLINE_MARGIN_SECONDS,
    WorkflowContext,
)

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


@pytest.mark.parametrize("enum", ["abc", 3, {"x": 1}, [], [float("nan")]])
def test_validate_schema_rejects_malformed_enum(enum):
    errors = validate_schema({"type": "string", "enum": enum})
    assert errors
    assert any("enum" in error for error in errors)


def test_validate_schema_accepts_nonempty_json_enum():
    schema = {"enum": ["red", 3, None, {"kind": "nested"}, ["x"]]}
    assert validate_schema(schema) == []


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_validate_rejects_non_finite_json_numbers(value):
    assert validate(value, {"type": "number"})


def test_validate_rejects_unknown_schema_types_even_when_value_matches():
    errors = validate("anything", {"type": "mystery"})
    assert errors
    assert "unsupported schema type" in errors[0]


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


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "string"},
        {"type": ["object", "null"]},
        {"properties": {}},
    ],
)
def test_tool_rejects_non_object_top_level_schema(schema):
    with pytest.raises(ValueError, match="top-level type must be 'object'"):
        StructuredOutputTool(schema)


# --------------------------------------------------------------------------- #
# agent(schema=...)
# --------------------------------------------------------------------------- #


class FakeState:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []


class _Cursor:
    """Shared payload cursor across the sessions a single agent(schema=) call
    builds — the first (free-exploration) session and the corrective
    forced-commit session draw from one ordered payload stream, so a payload
    consumed on the first pass is not replayed on the retry."""

    def __init__(self, payloads: list[Any]) -> None:
        self._payloads = list(payloads)
        self.idx = 0

    def next(self) -> Any:
        if self.idx >= len(self._payloads):
            self.idx += 1
            return _NO_CALL
        payload = self._payloads[self.idx]
        self.idx += 1
        return payload


class CapturingSession:
    """Session whose run_loop calls structured_output with a scripted payload.

    Draws successive payloads from a shared ``_Cursor`` and invokes the injected
    StructuredOutputTool with each, simulating the model self-correcting.

    Models the real ``Session.run_loop`` DONE short-circuit (session_run.py):
    once a turn finishes, a bare re-run does NOT re-invoke the tool — it just
    returns the prior answer. Only an intervening ``add_user_message`` (which
    resets DONE -> IDLE) lets the next run_loop produce a fresh payload.

    ``max_rounds`` models the post-capture runaway seen live: a single
    run_loop keeps issuing rounds (the model re-calling the tool) until the
    round cap — or until the engine's ``cancel_event`` is set, mirroring the
    real precheck gate that stops the loop before the next LLM call. Each
    round costs ``tokens_each``.
    """

    def __init__(
        self,
        tools: Sequence[Any],
        cursor: _Cursor,
        tokens_each: int = 0,
        max_rounds: int = 1,
    ) -> None:
        self._tools = list(tools)
        self._cursor = cursor
        self._tokens_each = tokens_each
        self._max_rounds = max_rounds
        self.state = FakeState()
        self.run_count = 0
        self.rounds = 0
        self.cancel_events: list[Any] = []
        # True once a turn has finished; cleared by add_user_message.
        self._done = False

    @property
    def messages(self) -> list[dict[str, Any]]:
        # Mirrors the real ``Session.messages`` property (getter -> state.messages,
        # setter -> replace) so the engine can carry first-pass exploration into
        # the corrective session.
        return self.state.messages

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        self.state.messages = list(value)

    def _structured_tool(self) -> StructuredOutputTool | None:
        for t in self._tools:
            if isinstance(t, StructuredOutputTool):
                return t
        return None

    async def add_user_message(self, content: str) -> None:
        self.state.messages.append({"role": "user", "content": content})
        self._done = False  # reset_for_user_turn -> resume_to_idle (DONE -> IDLE)

    async def run_loop(self, cancel_event: Any = None) -> str:
        self.run_count += 1
        self.cancel_events.append(cancel_event)
        # DONE short-circuit: a re-run without an intervening user message
        # returns the prior answer without re-invoking any tool.
        if self._done:
            return "assistant text"
        tool = self._structured_tool()
        for _ in range(self._max_rounds):
            # The real loop checks the event in precheck, BEFORE each LLM call.
            if cancel_event is not None and cancel_event.is_set():
                break
            payload = self._cursor.next()
            if tool is not None and payload is not _NO_CALL:
                await tool.execute_with_runtime(payload, _runtime())
            self.rounds += 1
        self._done = True
        return "assistant text"

    @property
    def used_tokens(self) -> int:
        return self._tokens_each * self.rounds


_NO_CALL = object()


class ScriptedFactory:
    """Builds CapturingSessions sharing one payload cursor, recording builds.

    A single ``agent(schema=)`` call can build two sessions: the first free
    exploration pass and the corrective forced-commit pass. ``session`` is the
    first session built; ``sessions`` holds them all in build order; ``builds``
    records the kwargs each was built with (prompt/tools/isolation/tool_choice).
    """

    def __init__(self, payloads: list[Any], tokens_each: int = 0, max_rounds: int = 1) -> None:
        self._cursor = _Cursor(payloads)
        self._tokens_each = tokens_each
        self._max_rounds = max_rounds
        self.session: CapturingSession | None = None
        self.sessions: list[CapturingSession] = []
        self.builds: list[dict[str, Any]] = []

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> CapturingSession:
        self.builds.append(
            {
                "prompt": prompt,
                "tools": tools,
                "isolation": isolation,
                "tool_choice": tool_choice,
                "thinking": thinking,
            }
        )
        session = CapturingSession(
            tools or [], self._cursor, self._tokens_each, self._max_rounds
        )
        self.sessions.append(session)
        if self.session is None:
            self.session = session
        return session


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
    # first (free) pass produces an invalid payload; the forced corrective pass
    # (a second session) lands a valid one.
    factory = ScriptedFactory(payloads=[{"x": "bad"}, {"x": 9}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 9}
    # two sessions built: free exploration, then forced commit.
    assert len(factory.sessions) == 2
    assert factory.sessions[0].run_count == 1
    assert factory.sessions[1].run_count == 1


@pytest.mark.asyncio
async def test_agent_schema_returns_none_after_failed_retry():
    factory = ScriptedFactory(payloads=[{"x": "bad"}, {"x": "still bad"}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result is None
    assert len(factory.sessions) == 2  # exactly one corrective retry session


@pytest.mark.asyncio
async def test_agent_schema_no_call_at_all_returns_none():
    # model never calls structured_output on either pass -> None after retry
    factory = ScriptedFactory(payloads=[_NO_CALL, _NO_CALL])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result is None
    assert len(factory.sessions) == 2


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
async def test_agent_schema_capture_stops_runaway_session():
    """A successful capture must halt the session before its next LLM call.

    Live failure mode this pins: the model captured a valid payload on round
    one, then kept re-calling structured_output for 28 more rounds until the
    session budget died. With the capture-stop, only the capturing round runs.
    """
    factory = ScriptedFactory(payloads=[{"x": 1}] * 5, tokens_each=100, max_rounds=5)
    ctx = WorkflowContext(factory, budget_total=10_000)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 1}
    assert factory.session.rounds == 1  # post-capture rounds never ran
    assert ctx.budget.spent() == 100  # not 500


@pytest.mark.asyncio
async def test_agent_schema_run_loop_receives_set_cancel_event():
    factory = ScriptedFactory(payloads=[{"x": 1}])
    ctx = WorkflowContext(factory)

    await ctx.agent("give me x", schema=SCHEMA)

    assert len(factory.session.cancel_events) == 1
    event = factory.session.cancel_events[0]
    assert event is not None
    assert event.is_set()  # set by the capture


@pytest.mark.asyncio
async def test_agent_schema_self_correction_stops_at_capture():
    """Invalid then valid within one run: the loop stops right after the
    valid round, not at the round cap."""
    factory = ScriptedFactory(
        payloads=[{"x": "bad"}, {"x": 2}, {"x": 3}], tokens_each=100, max_rounds=5
    )
    ctx = WorkflowContext(factory, budget_total=10_000)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 2}
    assert factory.session.rounds == 2
    assert ctx.budget.spent() == 200


@pytest.mark.asyncio
async def test_agent_schema_appends_instruction_to_prompt():
    factory = ScriptedFactory(payloads=[{"x": 1}])
    ctx = WorkflowContext(factory)

    await ctx.agent("base prompt", schema=SCHEMA)

    seeded = factory.builds[0]["prompt"]
    assert "base prompt" in seeded
    assert "structured_output" in seeded


# --------------------------------------------------------------------------- #
# forced corrective commit (free-text stop -> tool_choice="required" retry)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_schema_free_text_stop_triggers_forced_retry():
    """(a) The first pass ends with content + no structured_output call (the
    free-text stop seen live under tool_choice=auto + thinking). The forced
    corrective pass then lands a valid payload, so the call returns the dict."""
    # first pass: model answers in free text (no tool call); retry: valid commit
    factory = ScriptedFactory(payloads=[_NO_CALL, {"x": 7}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA, tools=[object()])

    assert result == {"x": 7}  # not None — the forced retry rescued it
    assert len(factory.sessions) == 2
    # the corrective turn carries an explicit MUST-call / no-prose instruction
    corrective_prompt = factory.builds[1]["prompt"]
    assert "MUST call" in corrective_prompt
    assert "prose" in corrective_prompt
    assert "structured_output" in corrective_prompt


@pytest.mark.asyncio
async def test_agent_schema_first_pass_capture_skips_forced_retry():
    """(b) The first pass calls structured_output directly -> returns
    immediately, no forced corrective session built."""
    factory = ScriptedFactory(payloads=[{"x": 5}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA)

    assert result == {"x": 5}
    assert len(factory.sessions) == 1  # no forced retry session
    # the lone session ran the free first pass with no forced tool_choice
    assert factory.builds[0]["tool_choice"] is None


@pytest.mark.asyncio
async def test_forced_retry_session_is_single_tool_and_required():
    """(c) The corrective turn's session is built with tool_choice="required"
    and a toolset of exactly [capture_tool] (so "required" can only resolve to
    structured_output), while the first pass keeps free exploration."""
    extra_tool = object()
    # first pass misses, forcing the corrective build; payload there is irrelevant
    factory = ScriptedFactory(payloads=[_NO_CALL, _NO_CALL])
    ctx = WorkflowContext(factory)

    await ctx.agent("give me x", schema=SCHEMA, tools=[extra_tool])

    assert len(factory.builds) == 2
    first, corrective = factory.builds

    # first pass: free exploration — full toolset, no forced tool_choice
    assert first["tool_choice"] is None
    first_tools = list(first["tools"])
    assert extra_tool in first_tools
    assert any(isinstance(t, StructuredOutputTool) for t in first_tools)

    # corrective pass: forced single-tool commit with a named-function choice
    assert corrective["tool_choice"] == {
        "type": "function",
        "function": {"name": "structured_output"},
    }
    corrective_tools = list(corrective["tools"])
    assert len(corrective_tools) == 1
    assert isinstance(corrective_tools[0], StructuredOutputTool)
    # the SAME capture tool instance is reused so its capture/event stay live
    assert corrective_tools[0] is next(
        t for t in first_tools if isinstance(t, StructuredOutputTool)
    )


@pytest.mark.asyncio
async def test_forced_retry_carries_first_pass_exploration():
    """(e) The corrective session starts seeded with the first pass's
    conversation (its exploration history), not blank, so the forced commit
    fills the schema from what was actually gathered. Without the carry-over the
    retry session would hold ONLY its own retry message."""
    factory = ScriptedFactory(payloads=[_NO_CALL, {"x": 7}])
    ctx = WorkflowContext(factory)

    # First pass explores: seed a couple of tool-result messages onto the first
    # session before it answers in free text and triggers the corrective build.
    # CapturingSession.add_user_message appends the seeded prompt on pass 1, so
    # we inject the "exploration" directly into its message list.
    original_build = factory.build_workflow_session
    exploration = [
        {"role": "assistant", "content": "let me grep"},
        {"role": "tool", "content": "grep result: foo.py:10"},
    ]

    def build_with_exploration(**kwargs: Any) -> CapturingSession:
        session = original_build(**kwargs)
        if len(factory.sessions) == 1:  # the first (exploration) session
            session.state.messages.extend(exploration)
        return session

    factory.build_workflow_session = build_with_exploration  # type: ignore[method-assign]

    result = await ctx.agent("give me x", schema=SCHEMA, tools=[object()])

    assert result == {"x": 7}
    assert len(factory.sessions) == 2
    corrective = factory.sessions[1]
    # the first pass's exploration is present in the corrective session...
    assert exploration[0] in corrective.messages
    assert exploration[1] in corrective.messages
    # ...alongside its own retry message (added after the carry-over).
    assert any(
        m.get("role") == "user" and "structured_output" in str(m.get("content", ""))
        for m in corrective.messages
    )
    # carry-over is an independent list copy: the corrective session's appends
    # did not mutate the first session's history length.
    assert len(factory.sessions[0].messages) == len(exploration) + 1  # +seeded prompt


@pytest.mark.asyncio
async def test_structured_agent_forces_thinking_off():
    """PART 3: both sessions a schema= call builds (free exploration + forced
    corrective commit) must carry ``thinking=False`` — these are the death-slow
    generations whose reasoning is disabled regardless of the run-wide default."""
    # First pass misses (_NO_CALL) so the corrective commit session is also built.
    factory = ScriptedFactory(payloads=[_NO_CALL, {"x": 7}])
    ctx = WorkflowContext(factory)

    await ctx.agent("give me x", schema=SCHEMA, tools=[object()])

    assert len(factory.builds) == 2
    assert factory.builds[0]["thinking"] is False  # free-exploration pass
    assert factory.builds[1]["thinking"] is False  # forced corrective commit


@pytest.mark.asyncio
async def test_non_structured_agent_leaves_thinking_default():
    """A plain (non-schema) agent call must NOT force thinking off — it leaves
    ``thinking`` as None so the factory's run-wide default applies."""
    factory = ScriptedFactory(payloads=[])
    ctx = WorkflowContext(factory)

    await ctx.agent("just do it")

    assert factory.builds[0]["thinking"] is None


# --------------------------------------------------------------------------- #
# wall-clock deadline (time_low / seconds_left)
# --------------------------------------------------------------------------- #


def test_time_low_true_when_within_margin():
    factory = ScriptedFactory(payloads=[])
    # Deadline only 10s out, margin 120s -> already "low".
    ctx = WorkflowContext(
        factory,
        deadline_monotonic=time.monotonic() + 10.0,
        deadline_margin_seconds=120.0,
    )
    assert ctx.time_low() is True
    assert ctx.seconds_left() <= 10.0


def test_time_low_false_when_ample_time():
    factory = ScriptedFactory(payloads=[])
    # Deadline far out (1000s) with the default 120s margin -> not low.
    ctx = WorkflowContext(
        factory,
        deadline_monotonic=time.monotonic() + 1000.0,
        deadline_margin_seconds=DEFAULT_DEADLINE_MARGIN_SECONDS,
    )
    assert ctx.time_low() is False
    assert ctx.seconds_left() > 120.0


def test_time_low_false_unbounded():
    # No deadline wired (CLI / tests) -> never low, seconds_left is infinite.
    factory = ScriptedFactory(payloads=[])
    ctx = WorkflowContext(factory)
    assert ctx.time_low() is False
    assert ctx.seconds_left() == float("inf")


@pytest.mark.asyncio
async def test_forced_retry_keys_off_empty_capture_not_prose():
    """(d) The corrective pass fires only on a genuinely-empty capture. A first
    pass that DID capture a valid payload (e.g. a markup-leaked call the parser
    resolved into ``captured``) must NOT spuriously trigger the forced retry,
    even though run_loop also returns assistant prose text."""
    factory = ScriptedFactory(payloads=[{"x": 11}])
    ctx = WorkflowContext(factory)

    result = await ctx.agent("give me x", schema=SCHEMA, tools=[object()])

    assert result == {"x": 11}
    assert len(factory.sessions) == 1  # no corrective session built


def test_schema_satisfied_predicate():
    """(d) Unit-pin the minimal acceptance check that decides a real miss.

    A capture missing a required top-level key is treated like a miss (so the
    forced corrective turn runs); a no-required schema accepts any captured dict
    (the tool already validated it).
    """
    from opencollab.application.workflow import _schema_satisfied

    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    assert _schema_satisfied({"x": 1}, schema) is True
    assert _schema_satisfied({}, schema) is False  # missing required key -> miss
    assert _schema_satisfied(None, schema) is False  # no capture -> miss
    assert _schema_satisfied("not a dict", schema) is False
    # no required keys: any captured dict (even {}) is an accepted commit
    assert _schema_satisfied({}, {"type": "object"}) is True
