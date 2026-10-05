import asyncio
import json
from types import SimpleNamespace

import pytest

from opencollab.application.structured_output import StructuredOutputTool
from opencollab.application.submit_findings import SubmitFindingsTool
from opencollab.application.tool_execution import (
    MAX_TOOL_CALLS_PER_BATCH,
    _bash_likely_mutates,
)
from opencollab.domain.session import SessionState
from opencollab.domain.tools import LoopDetection
from tests.support.tool_execution_test_support import (
    AlwaysAllowPermissionPolicy as FakePermissionPolicy,
)
from tests.support.tool_execution_test_support import (
    FakeAgent,
    FakeTracer,
    RuntimeNativeTool,
    build_use_case,
    tool_call,
)


def run(coro):
    return asyncio.run(coro)


class FakeSafetyPolicy:
    pass


class SideEffectTool(RuntimeNativeTool):
    parameters = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }


@pytest.mark.parametrize(
    ("schema", "arguments", "expected_error"),
    [
        (
            {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            '{"value": 1, "unexpected": true}',
            "unexpected property",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "number", "minimum": 0}},
                "required": ["value"],
            },
            '{"value": -1}',
            "must be >= 0",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "number", "maximum": 10}},
                "required": ["value"],
            },
            '{"value": 11}',
            "must be <= 10",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "string", "minLength": 3}},
                "required": ["value"],
            },
            '{"value": "ab"}',
            "must have length >= 3",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "string", "maxLength": 3}},
                "required": ["value"],
            },
            '{"value": "abcd"}',
            "must have length <= 3",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "string", "pattern": "^[A-Z]+$"}},
                "required": ["value"],
            },
            '{"value": "lower"}',
            "must match pattern",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "array", "minItems": 2}},
                "required": ["value"],
            },
            '{"value": [1]}',
            "must contain at least 2 items",
        ),
        (
            {
                "type": "object",
                "properties": {"value": {"type": "array", "maxItems": 2}},
                "required": ["value"],
            },
            '{"value": [1, 2, 3]}',
            "must contain at most 2 items",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "value": {
                        "oneOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "string", "minLength": 3},
                        ]
                    }
                },
                "required": ["value"],
            },
            '{"value": false}',
            "must match exactly one schema in oneOf",
        ),
    ],
    ids=[
        "additional-properties",
        "minimum",
        "maximum",
        "min-length",
        "max-length",
        "pattern",
        "min-items",
        "max-items",
        "one-of",
    ],
)
def test_declared_schema_constraints_block_side_effects(schema, arguments, expected_error):
    tool = SideEffectTool()
    tool.parameters = schema
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))

    result = run(use_case.process([tool_call(arguments=arguments)]))

    assert tool.runtime_calls == []
    assert result.messages_to_append[0]["content"].startswith(
        "Error: schema validation failed:"
    )
    assert expected_error in result.messages_to_append[0]["content"]


def test_structured_output_rejects_unsupported_assertion_schema_before_provider_use():
    with pytest.raises(ValueError, match="anyOf: unsupported schema keyword"):
        StructuredOutputTool(
            {
                "anyOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ]
            }
        )


def test_structured_output_preflight_accepts_large_json_integer():
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }
    tool = StructuredOutputTool(schema)
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    value = int("1" + "0" * 400) + 1
    call = tool_call(name=tool.name, arguments=json.dumps({"value": value}))

    assert use_case.preflight_tool_batch([call]) == [""]


def test_structured_output_preflight_rejects_large_integer_maximum():
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer", "maximum": 10}},
    }
    tool = StructuredOutputTool(schema)
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    value = int("1" + "0" * 400) + 1
    call = tool_call(name=tool.name, arguments=json.dumps({"value": value}))

    errors = use_case.preflight_tool_batch([call])

    assert len(errors) == 1
    assert "must be <= 10" in errors[0]


def test_structured_output_preflight_checks_large_integer_minimum():
    value = int("1" + "0" * 400) + 1
    schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer", "minimum": value + 1}},
    }
    tool = StructuredOutputTool(schema)
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    call = tool_call(name=tool.name, arguments=json.dumps({"value": value}))

    errors = use_case.preflight_tool_batch([call])

    assert len(errors) == 1
    assert "must be >=" in errors[0]


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        ("{not-json", "Error: invalid JSON arguments: {not-json"),
        (
            '["not", "an", "object"]',
            'Error: tool arguments must be a JSON object: ["not", "an", "object"]',
        ),
    ],
    ids=["invalid-json", "non-object-json"],
)
def test_tool_execution_use_case_preserves_argument_errors(arguments, expected_error):
    use_case, publisher = build_use_case()

    result = run(use_case.process([tool_call(arguments=arguments)]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": expected_error,
        }
    ]
    assert publisher.events == []


@pytest.mark.parametrize(
    "failure",
    ["invalid-json", "non-object", "unknown-tool", "schema"],
)
def test_short_circuited_tool_steps_advance_progress_watchdog(failure):
    state = SessionState(messages=[])
    if failure == "unknown-tool":
        use_case, _ = build_use_case(state=state)
        call = tool_call(name="missing", arguments="{}")
    elif failure == "schema":
        use_case, _ = build_use_case(
            agent=FakeAgent(tools=[SideEffectTool()]),
            state=state,
        )
        call = tool_call(arguments="{}")
    else:
        use_case, _ = build_use_case(
            agent=FakeAgent(tools=[RuntimeNativeTool()]),
            state=state,
        )
        arguments = "{not-json" if failure == "invalid-json" else "[]"
        call = tool_call(arguments=arguments)

    for _ in range(3):
        result = run(use_case.process([call]))
        result.apply_to(state)
        assert result.messages_to_append[0]["tool_call_id"] == "call-1"

    assert state.turn.steps_since_progress == 3


def test_rejected_batch_advances_progress_watchdog_once():
    state = SessionState(messages=[])
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[RuntimeNativeTool()]),
        state=state,
    )
    calls = [
        tool_call(call_id="valid", arguments="{}"),
        tool_call(call_id="broken", arguments="{not-json"),
    ]

    result = run(use_case.process(calls))
    result.apply_to(state)

    assert state.turn.steps_since_progress == 1
    assert [message["tool_call_id"] for message in result.messages_to_append] == [
        "valid",
        "broken",
    ]


def test_tool_execution_use_case_handles_non_string_and_preparsed_arguments():
    tool = RuntimeNativeTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))

    invalid = run(use_case.process([tool_call(arguments=7)]))
    valid = run(use_case.process([tool_call(arguments={"value": 1})]))

    assert "must be a JSON object" in invalid.messages_to_append[0]["content"]
    assert valid.messages_to_append[0]["content"] == "runtime result"
    assert tool.runtime_calls[0][0] == {"value": 1}


def test_terminal_structured_output_skips_later_side_effect_and_answers_every_call():
    terminal = StructuredOutputTool(
        {
            "type": "object",
            "required": ["answer"],
            "properties": {"answer": {"type": "string"}},
        }
    )
    side_effect = SideEffectTool(output="side effect executed")
    use_case, _ = build_use_case(agent=FakeAgent(tools=[terminal, side_effect]))

    result = run(
        use_case.process(
            [
                tool_call(
                    name="structured_output",
                    arguments='{"answer": "done"}',
                    call_id="terminal",
                ),
                tool_call(
                    name="fake_tool",
                    arguments='{"value": 1}',
                    call_id="side-effect",
                ),
            ]
        )
    )

    assert terminal.captured == {"answer": "done"}
    assert side_effect.runtime_calls == []
    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "terminal",
            "content": "Recorded. Structured output accepted. Your task is complete.",
        },
        {
            "role": "tool",
            "tool_call_id": "side-effect",
            "content": "Skipped: terminal structured output accepted earlier in this batch.",
        },
    ]


def test_terminal_submit_findings_skips_later_side_effect():
    terminal = SubmitFindingsTool()
    side_effect = SideEffectTool(output="side effect executed")
    use_case, _ = build_use_case(agent=FakeAgent(tools=[terminal, side_effect]))

    result = run(
        use_case.process(
            [
                tool_call(
                    name="submit_findings",
                    arguments=(
                        '{"findings": [], "summary": "done", '
                        '"insufficient_evidence": true}'
                    ),
                    call_id="terminal",
                ),
                tool_call(
                    name="fake_tool",
                    arguments='{"value": 1}',
                    call_id="side-effect",
                ),
            ]
        )
    )

    assert terminal.captured is not None
    assert side_effect.runtime_calls == []
    assert [message["tool_call_id"] for message in result.messages_to_append] == [
        "terminal",
        "side-effect",
    ]
    assert result.messages_to_append[-1]["content"] == (
        "Skipped: terminal structured output accepted earlier in this batch."
    )


def test_tool_execution_use_case_preserves_unknown_tool_error():
    agent = FakeAgent(tools=[SimpleNamespace(name="known_tool")])
    use_case, publisher = build_use_case(agent=agent)

    result = run(use_case.process([tool_call(name="missing_tool", arguments="{}")]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Error: unknown tool 'missing_tool'. Available: ['known_tool']",
        }
    ]
    assert publisher.events == []


def test_batch_preflight_prevents_partial_execution_on_late_schema_error():
    tool = SideEffectTool()
    use_case, publisher = build_use_case(agent=FakeAgent(tools=[tool]))
    calls = [
        tool_call(arguments='{"value": 1}', call_id="call-good"),
        tool_call(arguments='{"value": "x"}', call_id="call-bad"),
    ]

    result = run(use_case.process(calls))

    assert tool.runtime_calls == []
    assert publisher.events == []
    assert len(result.messages_to_append) == 2
    assert all(
        "entire tool-call batch rejected before execution" in message["content"]
        for message in result.messages_to_append
    )


def test_batch_preflight_rejects_duplicate_ids_before_execution():
    tool = SideEffectTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    calls = [
        tool_call(arguments='{"value": 1}', call_id="duplicate"),
        tool_call(arguments='{"value": 2}', call_id="duplicate"),
    ]

    result = run(use_case.process(calls))

    assert tool.runtime_calls == []
    assert all("duplicate tool_call id" in item["content"] for item in result.messages_to_append)


def test_batch_preflight_caps_call_count_before_execution():
    tool = SideEffectTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    calls = [
        tool_call(arguments='{"value": 1}', call_id=f"call-{index}")
        for index in range(MAX_TOOL_CALLS_PER_BATCH + 1)
    ]

    result = run(use_case.process(calls))

    assert tool.runtime_calls == []
    assert len(result.messages_to_append) == MAX_TOOL_CALLS_PER_BATCH + 1
    assert all("maximum is" in item["content"] for item in result.messages_to_append)


def test_tool_execution_use_case_preserves_loop_detection_event():
    state = SessionState(messages=[])
    use_case, publisher = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]
    assert "Loop detected" in result.messages_to_append[0]["content"]
    assert [(event.type, event.data) for event in publisher.events] == [
        ("loop_detected", {"tool": "fake_tool", "count": 3})
    ]


def test_tool_execution_use_case_detects_cyclic_loop_spread_across_window():
    # Regression: real 100-step stalls thrash in a CYCLE — the same (tool, args)
    # recurs every ~13 calls (observed 10-17 apart), not back-to-back, because
    # cleared tool outputs force re-reads. The old detector only scanned the last
    # 6 hashes and never saw three of a cyclically-repeated call, so it never
    # fired. The detector must count across the whole per-turn window.
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    filler = [f"other-{i}" for i in range(12)]  # one full thrash cycle of 13
    # two prior occurrences, each separated by a full cycle -> 13 calls apart
    state.replace_recent_tool_hashes([call_hash, *filler, call_hash, *filler])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]
    assert "Loop detected" in result.messages_to_append[0]["content"]


def test_tool_execution_use_case_catches_same_file_and_range_reread():
    # Re-reading the same slice still trips the existing read threshold.
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    read_hash = use_case.tool_call_hash("file_read", {"path": "x/ccode.py", "offset": 900, "limit": 50})
    state.replace_recent_tool_hashes([read_hash] * 7)
    call = tool_call(
        name="file_read",
        arguments='{"path": "x/ccode.py", "offset": 900, "limit": 50}',
    )

    result = run(use_case.process([call]))

    assert result.loop_detections == [LoopDetection(tool="file_read", count=8)]
    assert "on the same file and line range" in result.messages_to_append[0]["content"]


def test_tool_execution_use_case_allows_a_few_legitimate_rereads():
    # Three reads of one range is normal distill-as-you-read and
    # must NOT trip — the read threshold is more lenient than the exact-arg loop,
    # so the third read executes the tool instead of short-circuiting.
    state = SessionState(messages=[])
    tool = RuntimeNativeTool()
    tool.name = "file_read"
    agent = FakeAgent(tools=[tool])
    use_case, _ = build_use_case(state=state, agent=agent)
    path_hash = use_case.tool_call_hash("file_read", {"path": "x/ccode.py"})
    state.replace_recent_tool_hashes([path_hash] * 2)  # two prior reads
    call = tool_call(name="file_read", arguments='{"path": "x/ccode.py", "offset": 1}')

    result = run(use_case.process([call]))

    assert result.loop_detections == []
    assert tool.runtime_calls  # the third read executed normally


def _bash_tool(output: str = "ok"):
    tool = RuntimeNativeTool(output=output)
    tool.name = "bash"
    return tool


def test_tool_execution_use_case_executes_runtime_native_tool_and_events():
    tool = RuntimeNativeTool()
    agent = FakeAgent(tools=[tool])
    env = object()
    safety_policy = FakeSafetyPolicy()
    permission_policy = FakePermissionPolicy()
    use_case, publisher = build_use_case(
        agent=agent,
        environment=env,
        safety_policy=safety_policy,
        permission_policy=permission_policy,
    )

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.messages_to_append == [
        {"role": "tool", "tool_call_id": "call-1", "content": "runtime result"}
    ]
    assert len(tool.runtime_calls) == 1
    args, runtime = tool.runtime_calls[0]
    assert args == {"value": 1}
    assert runtime.environment is env
    assert runtime.safety_policy is safety_policy
    assert runtime.permission_policy is permission_policy
    assert publisher.events[0].type == "tool_start"
    assert publisher.events[0].data == {
        "tool": "fake_tool",
        "args": {"value": 1},
        "tool_call_id": "call-1",
    }
    assert publisher.events[1].type == "tool_end"
    assert publisher.events[1].data["tool"] == "fake_tool"
    assert publisher.events[1].data["tool_call_id"] == "call-1"


def _named_runtime_tool(name: str, output: str = "ok"):
    tool = RuntimeNativeTool(output=output)
    tool.name = name
    return tool


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -c 'print(1 > 0)'", False),
        ("echo guarantee output", False),
        ("printf '%s' '>'", False),
        ("cat input 2>&1", False),
        ("cat input >/dev/null", False),
        # A discarded stream stays discarded when another command follows it.
        # Reading it as a write silently switches off the read-loop steering,
        # because every such command resets the reads-since-last-edit counter.
        ("ls / 2>/dev/null; echo done", False),
        ("ls -la /root 2>/dev/null; ls -la /tmp 2>/dev/null", False),
        ("find / -name x 2>/dev/null | head; ls 2>/dev/null", False),
        ("cat a 2>/dev/null && grep b c", False),
        # ... and a real write after a discarded stream is still a write.
        ("ls 2>/dev/null; echo hi > target", True),
        ("cat a 2>/dev/null && echo x > b", True),
        ("cp source target", True),
        ("mv source target", True),
        ("install source target", True),
        ("git apply change.patch", True),
        ("perl -pi -e 's/old/new/' target", True),
        ("cat input | tee output", True),
        ("sed -i.bak 's/old/new/' target", True),
        ("printf x > target", True),
    ],
)
def test_bash_mutation_detection_uses_shell_tokens(command, expected):
    assert _bash_likely_mutates({"command": command}) is expected


@pytest.mark.parametrize(
    ("command", "output", "expected_reads"),
    [
        ("python -c 'print(1 > 0)'", "True", 5),
        ("cp source target", "copied", 0),
        ("cp source target", "Error: copy failed", 5),
    ],
)
def test_only_successful_mutating_bash_resets_read_counter(
    command, output, expected_reads
):
    state = SessionState(messages=[])
    state.turn.reads_since_last_edit = 5
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[_bash_tool(output)]),
        state=state,
    )

    result = run(
        use_case.process(
            [tool_call(name="bash", arguments={"command": command})]
        )
    )
    result.apply_read_write_counter_to(state)

    assert state.turn.reads_since_last_edit == expected_reads


def test_failed_read_does_not_advance_read_without_write_counter():
    state = SessionState(messages=[])
    state.turn.reads_since_last_edit = 5
    read = _named_runtime_tool("file_read", "Error: file unavailable")
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[read]),
        state=state,
    )

    result = run(use_case.process([tool_call(name="file_read")]))
    result.apply_read_write_counter_to(state)

    assert state.turn.reads_since_last_edit == 5


@pytest.mark.parametrize(
    ("names", "expected_reads"),
    [
        (["file_read", "file_write"], 0),
        (["file_write", "file_read", "file_read"], 2),
        (["file_read", "file_write", "file_read"], 1),
        (["file_write", "file_read", "file_write"], 0),
    ],
)
def test_read_write_counter_replays_successful_batch_order(names, expected_reads):
    state = SessionState(messages=[])
    tools = [_named_runtime_tool(name) for name in set(names)]
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=tools),
        state=state,
    )
    calls = [
        tool_call(name=name, call_id=f"call-{index}")
        for index, name in enumerate(names)
    ]

    result = run(use_case.process(calls))
    result.apply_read_write_counter_to(state)

    assert state.turn.reads_since_last_edit == expected_reads


@pytest.mark.parametrize("fail_type", ["tool_start", "tool_end"])
def test_tool_event_failures_do_not_discard_executed_result(fail_type):
    class FailingPublisher:
        def __init__(self, fail_type):
            self.fail_type = fail_type

        async def emit(self, event):
            if event.type == self.fail_type:
                raise RuntimeError(f"{event.type} failed")

    tool = RuntimeNativeTool()
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[tool]),
        event_publisher=FailingPublisher(fail_type),
    )

    result = run(use_case.process([tool_call()]))

    assert result.messages_to_append[0]["content"] == "runtime result"
    assert len(tool.runtime_calls) == 1


def test_short_circuit_invalid_json_is_traced():
    tracer = FakeTracer()
    use_case, _ = build_use_case(tracer=tracer)

    run(use_case.process([tool_call(arguments="{not-json")]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "tool_error"
    assert step["payload"]["tool"] == "fake_tool"
    assert step["payload"]["error"] == "invalid_json_args"


def test_short_circuit_unknown_tool_is_traced():
    tracer = FakeTracer()
    agent = FakeAgent(tools=[SimpleNamespace(name="known_tool")])
    use_case, _ = build_use_case(agent=agent, tracer=tracer)

    run(use_case.process([tool_call(name="missing_tool", arguments="{}")]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "tool_error"
    assert step["payload"]["tool"] == "missing_tool"
    assert step["payload"]["error"] == "unknown_tool"


def test_loop_block_short_circuit_counts_toward_hard_brake():
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))
    result.apply_to(state)

    assert state.turn.loop_blocked_since_progress == 1
    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]
