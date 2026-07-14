import asyncio
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import opencollab.application.tool_execution as tool_execution_module
import pytest
from opencollab.adapters.env import Environment
from opencollab.application.events import SessionEventFactory, default_session_event_factory
from opencollab.application.tool_execution import (
    MAX_TOOL_CALLS_PER_BATCH,
    ToolExecutionUseCase,
)
from opencollab.domain.session import SessionState
from opencollab.domain.tools import LoopDetection


def run(coro):
    return asyncio.run(coro)


def tool_call(name: str = "fake_tool", arguments: str = "{}") -> dict:
    return {
        "id": "call-1",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


class FakeAgent:
    def __init__(self, tools=None):
        self.tools = tools or []

    def find_tool(self, name):
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


class FakeEventPublisher:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class FakeTracer:
    def __init__(self):
        self.steps = []

    def log_step(self, **kwargs):
        self.steps.append(kwargs)


class FakePermissionPolicy:
    async def confirm(self, prompt: str) -> bool:
        return True


class FakeSafetyPolicy:
    pass


class RuntimeNativeTool:
    name = "fake_tool"

    def __init__(self, output: str = "runtime result"):
        self.output = output
        self.runtime_calls = []

    async def execute_with_runtime(self, args, runtime):
        self.runtime_calls.append((args, runtime))
        return self.output


class SideEffectTool(RuntimeNativeTool):
    parameters = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }


def event_factory() -> SessionEventFactory:
    factory = default_session_event_factory(aid=-1)
    # Wrap to use SimpleNamespace so tests that previously asserted on a
    # plain object (no aid field) keep their assertion shapes.
    return SessionEventFactory(
        step_start=factory.step_start,
        step_end=factory.step_end,
        text_delta=factory.text_delta,
        error=factory.error,
        loop_detected=lambda tool, count: SimpleNamespace(
            type="loop_detected",
            data={"tool": tool, "count": count},
        ),
        tool_start=lambda tool, args: SimpleNamespace(
            type="tool_start",
            data={"tool": tool, "args": args},
        ),
        tool_end=lambda tool, latency: SimpleNamespace(
            type="tool_end",
            data={"tool": tool, "latency": latency},
        ),
    )


def build_use_case(
    *,
    agent=None,
    state=None,
    event_publisher=None,
    tracer=None,
    environment=None,
    permission_policy=None,
    safety_policy=None,
    **use_case_kwargs,
):
    publisher = event_publisher or FakeEventPublisher()
    use_case = ToolExecutionUseCase(
        agent=agent or FakeAgent(),
        environment=environment,
        state=state or SessionState(messages=[]),
        event_publisher=publisher,
        event_factory=event_factory(),
        tracer=tracer,
        permission_policy=permission_policy,
        safety_policy=safety_policy,
        **use_case_kwargs,
    )
    return use_case, publisher


def test_tool_execution_use_case_preserves_invalid_json_error():
    use_case, publisher = build_use_case()

    result = run(use_case.process([tool_call(arguments="{not-json")]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Error: invalid JSON arguments: {not-json",
        }
    ]
    assert publisher.events == []


def test_tool_execution_use_case_rejects_valid_json_non_object_arguments():
    use_case, publisher = build_use_case()

    result = run(use_case.process([tool_call(arguments='["not", "an", "object"]')]))

    assert result.messages_to_append == [
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": (
                "Error: tool arguments must be a JSON object: "
                '["not", "an", "object"]'
            ),
        }
    ]
    assert publisher.events == []


def test_tool_execution_use_case_handles_non_string_and_preparsed_arguments():
    tool = RuntimeNativeTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))

    invalid = run(use_case.process([tool_call(arguments=7)]))
    valid = run(use_case.process([tool_call(arguments={"value": 1})]))

    assert "must be a JSON object" in invalid.messages_to_append[0]["content"]
    assert valid.messages_to_append[0]["content"] == "runtime result"
    assert tool.runtime_calls[0][0] == {"value": 1}


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
        {
            "id": "call-good",
            "function": {"name": "fake_tool", "arguments": '{"value": 1}'},
        },
        {
            "id": "call-bad",
            "function": {"name": "fake_tool", "arguments": '{"value": "x"}'},
        },
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
        {
            "id": "duplicate",
            "function": {"name": "fake_tool", "arguments": '{"value": 1}'},
        },
        {
            "id": "duplicate",
            "function": {"name": "fake_tool", "arguments": '{"value": 2}'},
        },
    ]

    result = run(use_case.process(calls))

    assert tool.runtime_calls == []
    assert all("duplicate tool_call id" in item["content"] for item in result.messages_to_append)


def test_batch_preflight_caps_call_count_before_execution():
    tool = SideEffectTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    calls = [
        {
            "id": f"call-{index}",
            "function": {"name": "fake_tool", "arguments": '{"value": 1}'},
        }
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


def test_tool_execution_use_case_catches_same_file_reread_with_shifting_ranges():
    # Regression (sympy-11400): a model thrashed by re-reading ONE file ~135 times
    # with SHIFTING line ranges. Each exact-arg hash was unique, so the
    # MAX_SIMILAR_CALLS=3 counter never tripped. Read tools now key on the PATH
    # alone, so the re-reads collide and trip at MAX_SAME_FILE_READS (8).
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    # Seven prior reads of the same file at DIFFERENT ranges collapse to one
    # path-only hash (range args are ignored for file_read).
    path_hash = use_case.tool_call_hash("file_read", {"path": "x/ccode.py"})
    state.replace_recent_tool_hashes([path_hash] * 7)
    call = {
        "id": "call-1",
        "function": {
            "name": "file_read",
            "arguments": '{"path": "x/ccode.py", "start": 900, "limit": 50}',
        },
    }

    result = run(use_case.process([call]))

    assert result.loop_detections == [LoopDetection(tool="file_read", count=8)]
    assert "on the same file" in result.messages_to_append[0]["content"]


def test_tool_execution_use_case_allows_a_few_legitimate_rereads():
    # Three reads of one file (varying ranges) is normal distill-as-you-read and
    # must NOT trip — the read threshold is more lenient than the exact-arg loop,
    # so the third read executes the tool instead of short-circuiting.
    state = SessionState(messages=[])
    tool = RuntimeNativeTool()
    tool.name = "file_read"
    agent = FakeAgent(tools=[tool])
    use_case, _ = build_use_case(state=state, agent=agent)
    path_hash = use_case.tool_call_hash("file_read", {"path": "x/ccode.py"})
    state.replace_recent_tool_hashes([path_hash] * 2)  # two prior reads
    call = {
        "id": "call-1",
        "function": {"name": "file_read", "arguments": '{"path": "x/ccode.py", "start": 1}'},
    }

    result = run(use_case.process([call]))

    assert result.loop_detections == []
    assert tool.runtime_calls  # the third read executed normally


def test_reads_without_write_counter_accumulates_and_resets():
    # Closed-loop steering signal: successful reads accumulate
    # reads_since_last_edit; a successful write zeroes it.
    state = SessionState(messages=[])
    read_tool = RuntimeNativeTool()
    read_tool.name = "file_read"
    write_tool = RuntimeNativeTool(output="Created/wrote a.py (10 chars)")
    write_tool.name = "file_write"
    agent = FakeAgent(tools=[read_tool, write_tool])
    use_case, _ = build_use_case(state=state, agent=agent)

    def call(name, cid, args):
        return {"id": cid, "function": {"name": name, "arguments": args}}

    run(use_case.process([call("file_read", "c1", '{"path": "a.py"}')])).apply_to(state)
    run(use_case.process([call("file_read", "c2", '{"path": "b.py"}')])).apply_to(state)
    assert state.reads_since_last_edit == 2

    run(use_case.process([call("file_write", "c3", '{"path": "a.py"}')])).apply_to(state)
    assert state.reads_since_last_edit == 0  # a landed edit resets the counter


def test_reads_counter_ignores_failed_writes():
    # A write whose result is an error must NOT reset the counter.
    state = SessionState(messages=[], reads_since_last_edit=3)
    bad_write = RuntimeNativeTool(output="Error: old_str not found in a.py.")
    bad_write.name = "file_write"
    agent = FakeAgent(tools=[bad_write])
    use_case, _ = build_use_case(state=state, agent=agent)

    result = run(use_case.process([
        {"id": "c1", "function": {"name": "file_write", "arguments": '{"path": "a.py"}'}}
    ]))
    result.apply_to(state)
    assert state.reads_since_last_edit == 3  # failed write does not count as an edit


def _bash_tool(output: str = "ok"):
    tool = RuntimeNativeTool(output=output)
    tool.name = "bash"
    return tool


def test_bash_mutation_resets_counter():
    # Bug B (OPTION 2): the coder lands real source edits via bash (sed -i,
    # heredoc redirect). Such a mutating bash must reset reads_since_last_edit the
    # same as file_write — otherwise the counter climbs forever and the hard
    # "STOP reading" nudge mis-fires at a model already writing. FAILS pre-edit.
    mutating = (
        "sed -i 's/a/b/' x.py",
        "cat > x.py <<'EOF'\nbody\nEOF",
        # idiomatic pathlib read-modify-write shapes the coder commonly emits
        "python -c \"from pathlib import Path; Path('x.py').write_text(src)\"",
        "python -c \"Path('x.py').write_bytes(b)\"",
    )
    for cmd in mutating:
        state = SessionState(messages=[], reads_since_last_edit=5)
        agent = FakeAgent(tools=[_bash_tool(output="done")])
        use_case, _ = build_use_case(state=state, agent=agent)
        run(use_case.process([
            {"id": "c1", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}
        ])).apply_to(state)
        assert state.reads_since_last_edit == 0, f"mutating bash should reset: {cmd!r}"


def test_bash_repro_does_not_reset_counter():
    # A bash repro (python -c print) and a grep-style read are NOT edits — the
    # heuristic must not reset on them (guards against over-firing). Passes today.
    for cmd in ("python -c 'print(1)'", "grep -rn foo x.py", "pytest x.py 2>&1"):
        state = SessionState(messages=[], reads_since_last_edit=5)
        agent = FakeAgent(tools=[_bash_tool(output="output")])
        use_case, _ = build_use_case(state=state, agent=agent)
        run(use_case.process([
            {"id": "c1", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}
        ])).apply_to(state)
        assert state.reads_since_last_edit == 5, f"non-mutating bash must not reset: {cmd!r}"


def test_bash_mutation_error_output_does_not_reset():
    # A mutating-shaped bash whose OUTPUT is an error did not actually edit — it
    # must NOT reset (mirrors test_reads_counter_ignores_failed_writes).
    state = SessionState(messages=[], reads_since_last_edit=4)
    agent = FakeAgent(tools=[_bash_tool(output="Error: sed: no such file")])
    use_case, _ = build_use_case(state=state, agent=agent)
    run(use_case.process([
        {"id": "c1", "function": {"name": "bash", "arguments": json.dumps({"command": "sed -i s/a/b/ x.py"})}}
    ])).apply_to(state)
    assert state.reads_since_last_edit == 4  # error output -> no reset


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
    assert publisher.events[0].data == {"tool": "fake_tool", "args": {"value": 1}}
    assert publisher.events[1].type == "tool_end"
    assert publisher.events[1].data["tool"] == "fake_tool"


def test_tool_event_failures_do_not_discard_executed_result():
    class FailingPublisher:
        def __init__(self, fail_type):
            self.fail_type = fail_type

        async def emit(self, event):
            if event.type == self.fail_type:
                raise RuntimeError(f"{event.type} failed")

    for fail_type in ("tool_start", "tool_end"):
        tool = RuntimeNativeTool()
        use_case, _ = build_use_case(
            agent=FakeAgent(tools=[tool]),
            event_publisher=FailingPublisher(fail_type),
        )

        result = run(use_case.process([tool_call()]))

        assert result.messages_to_append[0]["content"] == "runtime result"
        assert len(tool.runtime_calls) == 1


def test_tool_trace_failure_does_not_discard_executed_result():
    class FailingTracer:
        def log_step(self, **kwargs):
            raise RuntimeError("trace failed")

    tool = RuntimeNativeTool()
    use_case, _ = build_use_case(
        agent=FakeAgent(tools=[tool]),
        tracer=FailingTracer(),
    )

    result = run(use_case.process([tool_call()]))

    assert result.messages_to_append[0]["content"] == "runtime result"


def test_tool_execution_use_case_preserves_trace_payload_capping():
    raw_output = "a" * 10_000
    tool = RuntimeNativeTool(output=raw_output)
    tracer = FakeTracer()
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]), tracer=tracer)

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert result.messages_to_append[0]["content"] == raw_output
    assert len(tracer.steps) == 1
    payload = tracer.steps[0]["payload"]
    assert payload["tool"] == "fake_tool"
    assert payload["args"] == {"value": 1}
    assert payload["result_len"] == len(raw_output)
    assert "\n...[truncated]...\n" in payload["result"]


def test_tool_execution_use_case_persists_full_tool_output():
    # The full result is appended/persisted; bounding what the model sees is the
    # job of the call-time per-tool-result budget shaper, not tool execution.
    raw_output = "a" * 50_000
    tool = RuntimeNativeTool(output=raw_output)
    use_case, _publisher = build_use_case(agent=FakeAgent(tools=[tool]))

    result = run(use_case.process([tool_call()]))

    content = result.messages_to_append[0]["content"]
    assert content == raw_output


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


def test_short_circuit_loop_block_is_traced():
    tracer = FakeTracer()
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state, tracer=tracer)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    run(use_case.process([tool_call(arguments='{"value": 1}')]))

    assert len(tracer.steps) == 1
    step = tracer.steps[0]
    assert step["step_type"] == "loop_blocked"
    assert step["payload"]["tool"] == "fake_tool"
    assert step["payload"]["count"] == 3


def test_loop_block_short_circuit_counts_toward_hard_brake():
    state = SessionState(messages=[])
    use_case, _ = build_use_case(state=state)
    call_hash = use_case.tool_call_hash("fake_tool", {"value": 1})
    state.replace_recent_tool_hashes([call_hash, call_hash])

    result = run(use_case.process([tool_call(arguments='{"value": 1}')]))
    result.apply_to(state)

    assert state.loop_blocked_since_progress == 1
    assert result.loop_detections == [LoopDetection(tool="fake_tool", count=3)]


class RevocableEnvironment(Environment):
    def __init__(self):
        self._aborted = False
        self.writes = []

    async def exec_cmd(self, cmd: str, timeout: float = 120.0):
        self._ensure_active()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        return ""

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        self.writes.append((path, content))


class StubbornLateWriteTool:
    name = "stubborn_late_writer"
    default_timeout = 0.005
    disable_outer_timeout = False

    def __init__(self):
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.write_blocked = asyncio.Event()

    async def execute_with_runtime(self, args, runtime):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_seen.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            try:
                await runtime.environment.write_file("late.py", "late")
            except RuntimeError:
                self.write_blocked.set()
            return "late completion"


def _bounded_tool_use_case(tool, environment):
    return build_use_case(
        agent=FakeAgent(tools=[tool]),
        environment=environment,
        cancellation_cleanup_timeout=0.01,
        cancellation_force_timeout=0.01,
        environment_abort_timeout=0.01,
    )[0]


@pytest.mark.asyncio
async def test_stubborn_timed_out_tool_cannot_write_after_execute_returns(monkeypatch):
    monkeypatch.setattr(tool_execution_module, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)
    environment = RevocableEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)

    started = time.monotonic()
    output, _latency = await use_case.execute_tool(tool, {})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert "Tool cancellation cleanup failed" in output
    assert "environment was revoked" in output
    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1

    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_stubborn_environment_abort_is_bounded_and_exposed(monkeypatch):
    monkeypatch.setattr(tool_execution_module, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)

    class StubbornAbortEnvironment(RevocableEnvironment):
        def __init__(self):
            super().__init__()
            self.abort_started = asyncio.Event()
            self.abort_release = asyncio.Event()

        async def abort(self) -> None:
            self.abort_started.set()
            while not self.abort_release.is_set():
                try:
                    await self.abort_release.wait()
                except asyncio.CancelledError:
                    continue

    environment = StubbornAbortEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)

    started = time.monotonic()
    output, _latency = await use_case.execute_tool(tool, {})
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert environment.abort_started.is_set()
    assert "environment abort did not quiesce within its bounded timeout" in output
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 2

    tool.release.set()
    environment.abort_release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_cooperative_tool_timeout_keeps_existing_result_shape(monkeypatch):
    monkeypatch.setattr(tool_execution_module, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)

    class CooperativeTimeoutTool:
        name = "cooperative"
        default_timeout = 0.005
        disable_outer_timeout = False

        async def execute_with_runtime(self, args, runtime):
            await asyncio.Event().wait()

    tool = CooperativeTimeoutTool()
    environment = RevocableEnvironment()
    use_case = _bounded_tool_use_case(tool, environment)

    output, _latency = await use_case.execute_tool(tool, {})

    assert output.startswith("Tool execution timed out after ")
    assert "while running 'cooperative'." in output
    assert "cleanup failed" not in output
    assert environment._aborted is False
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_execute_tool_success_path_is_unchanged():
    tool = RuntimeNativeTool(output="success")
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))

    output, _latency = await use_case.execute_tool(tool, {"value": 1})

    assert output == "success"
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_execute_tool_preserves_caller_cancellation():
    class CallerCancelledTool:
        name = "caller_cancelled"
        default_timeout = None
        disable_outer_timeout = True

        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def execute_with_runtime(self, args, runtime):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    tool = CallerCancelledTool()
    use_case, _ = build_use_case(agent=FakeAgent(tools=[tool]))
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))
    await tool.started.wait()

    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    await asyncio.wait_for(tool.cancelled.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_repeated_caller_cancel_cannot_interrupt_late_write_cleanup():
    class CallerStubbornLateWriteTool(StubbornLateWriteTool):
        default_timeout = None
        disable_outer_timeout = True

    environment = RevocableEnvironment()
    tool = CallerStubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))
    await tool.started.wait()

    execution.cancel()
    await tool.cancel_seen.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1
    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_caller_cancel_cannot_interrupt_tool_timeout_cleanup(monkeypatch):
    monkeypatch.setattr(tool_execution_module, "TOOL_EXECUTION_TIMEOUT_GRACE", 0.001)
    environment = RevocableEnvironment()
    tool = StubbornLateWriteTool()
    use_case = _bounded_tool_use_case(tool, environment)
    execution = asyncio.create_task(use_case.execute_tool(tool, {}))

    await tool.cancel_seen.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution

    assert environment._aborted is True
    pending = use_case.pending_cleanup_tasks
    assert len(pending) == 1
    tool.release.set()
    await asyncio.gather(*pending)
    await asyncio.wait_for(tool.write_blocked.wait(), timeout=0.2)

    assert tool.write_blocked.is_set()
    assert environment.writes == []
    assert use_case.pending_cleanup_tasks == ()


@pytest.mark.asyncio
async def test_simultaneous_outer_and_cleanup_cancel_cannot_spin():
    use_case, _ = build_use_case()
    cleanup_started = asyncio.Event()

    async def cleanup():
        cleanup_started.set()
        await asyncio.Event().wait()

    cleanup_task = asyncio.create_task(cleanup())
    owner = asyncio.create_task(
        use_case._await_owned_cleanup_despite_cancellation(cleanup_task)
    )
    await cleanup_started.wait()

    owner.cancel()
    cleanup_task.cancel()

    await asyncio.wait_for(owner, timeout=0.2)
    assert cleanup_task.cancelled() is True


@pytest.mark.parametrize(
    "field",
    [
        "cancellation_cleanup_timeout",
        "cancellation_force_timeout",
        "environment_abort_timeout",
    ],
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "invalid"])
def test_tool_cleanup_timeouts_must_be_finite_and_positive(field, value):
    kwargs = {field: value}

    with pytest.raises(ValueError, match="must be a finite positive number"):
        build_use_case(**kwargs)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "invalid"])
def test_invalid_requested_tool_timeout_falls_back_to_positive_bound(value):
    tool = SimpleNamespace(default_timeout=None, disable_outer_timeout=False)
    use_case, _ = build_use_case()

    timeout = use_case.tool_execution_timeout(tool, {"timeout": value})

    assert timeout is not None
    assert math.isfinite(timeout)
    assert timeout > 0


def test_application_tool_execution_module_does_not_import_outer_layers():
    package_root = Path(__file__).resolve().parents[1]
    source = (package_root / "opencollab/application/tool_execution.py").read_text(encoding="utf-8")

    assert "opencollab.core.session" not in source
    assert "opencollab.tools" not in source
    assert "opencollab.bootstrap" not in source
    assert "opencollab.tui" not in source
