"""Pagination must advance while repeated read cycles still stop."""

import asyncio
import json

from opencollab.domain.session import SessionPhase, SessionState
from tests.support.session_run_loop_test_support import build_runner
from tests.support.tool_execution_test_support import (
    FakeAgent,
    RuntimeNativeTool,
    build_use_case,
    tool_call,
)


class RangeReadTool(RuntimeNativeTool):
    name = "file_read"

    async def execute_with_runtime(self, args, runtime):
        self.runtime_calls.append((args, runtime))
        return f"File: large.ts\n{args.get('offset', 1)}\tsection {args.get('limit', 500)}"


def reads(ranges, *, batch):
    return [
        tool_call(
            name="file_read",
            arguments=json.dumps({"path": "large.ts", "offset": offset, "limit": limit}),
            call_id=f"{batch}-{index}",
        )
        for index, (offset, limit) in enumerate(ranges)
    ]


def test_distinct_pages_after_writes_do_not_stop_the_session():
    # Captures the failure shape from a real large-file task. The last batch
    # used to block the eighth, ninth and tenth reads of this path together.
    state = SessionState(messages=[])
    read = RangeReadTool()
    write = RuntimeNativeTool(output="Created/wrote types.ts")
    write.name = "file_write"
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[read, write]))
    batches = [
        reads([(1, 180), (181, 220)], batch=0),
        [tool_call(name="file_write", arguments='{"path":"types.ts","content":"type T = string"}')],
        reads([(620, 220), (1120, 180), (1580, 190), (1740, 180),
               (1960, 180), (2000, 330), (2360, 230), (2600, 180)], batch=2),
    ]
    for batch in batches:
        result = asyncio.run(executor.process(batch))
        result.apply_to(state)
        assert result.loop_detections == []

    assert len(read.runtime_calls) == 10
    assert state.turn.distinct_evidence_count == 11
    assert state.turn.loop_blocked_since_progress == 0
    assert state.turn.steps_since_progress == 0
    state.transition_to(SessionPhase.PRECHECK)
    asyncio.run(build_runner(state=state).precheck(None))
    assert state.phase is SessionPhase.CALLING_LLM


def test_pagination_cycle_still_reaches_the_loop_stop():
    state = SessionState(messages=[])
    read = RangeReadTool()
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[read]))
    for batch in range(8):
        result = asyncio.run(executor.process(reads([(1, 100), (101, 100), (201, 100)], batch=batch)))
        result.apply_to(state)

    assert len(read.runtime_calls) == 21
    assert [item.count for item in result.loop_detections] == [8, 8, 8]
    assert state.turn.distinct_evidence_count == 3
    assert state.turn.loop_blocked_since_progress == 3
    assert state.turn.loop_state.blocked_rounds == 1
    state.transition_to(SessionPhase.PRECHECK)
    asyncio.run(build_runner(state=state).precheck(None))
    assert state.phase is SessionPhase.CALLING_LLM
    for batch in range(8, 10):
        result = asyncio.run(executor.process(reads([(1, 100), (101, 100), (201, 100)], batch=batch)))
        result.apply_to(state)
    assert len(read.runtime_calls) == 21
    assert state.turn.loop_state.blocked_rounds == 3
    state.set_phase(SessionPhase.PRECHECK)
    asyncio.run(build_runner(state=state).precheck(None))
    assert state.phase is SessionPhase.STOPPED
    assert state.terminal_reason == "loop block limit reached: 3 unproductive tool batches"


def test_omitted_and_explicit_read_defaults_share_the_repeat_count():
    state = SessionState(messages=[])
    read = RangeReadTool()
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[read]))
    for index in range(8):
        args = {"path": "large.ts"}
        if index % 2:
            args.update(offset=1, limit=500)
        result = asyncio.run(executor.process([
            tool_call(name="file_read", arguments=json.dumps(args), call_id=str(index))
        ]))
        result.apply_to(state)

    assert len(read.runtime_calls) == 7
    assert [item.count for item in result.loop_detections] == [8]
    assert state.turn.distinct_evidence_count == 1
