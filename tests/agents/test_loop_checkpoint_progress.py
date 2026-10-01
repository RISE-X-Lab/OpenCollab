"""Confirmed prefix progress survives an interrupted reserved dispatch."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import FakeAgent, FakeLLMClient
from tests.support.session_runtime_test_support import tool_call
from tests.support.tool_execution_test_support import RuntimeNativeTool


class _InterruptedEnvironment(LocalEnvironment):
    interrupt = False

    async def exec_cmd(self, command, timeout=120):
        result = await super().exec_cmd(command, timeout=timeout)
        if self.interrupt:
            self.interrupt = False
            raise asyncio.CancelledError
        return result


def _call(name, args, identifier):
    return tool_call(identifier, name, json.dumps(args))


async def _execute(session, *calls):
    session.state.advance_step()
    session.state.append_message({"role": "assistant", "content": None, "tool_calls": list(calls)})
    result = await session.tool_execution.process(list(calls))
    result.apply_to(session.state)
    return result


async def _prepare(tmp_path, *, timeout_recovery=False):
    environment = _InterruptedEnvironment(str(tmp_path))
    old = RuntimeNativeTool(output="stable old evidence")
    old.name = "independent"
    agent = FakeAgent(tools=[old, BashTool(), FileReadTool(), FileWriteTool()])
    path = str(tmp_path / "checkpoint.json")
    session = build_session(agent=agent, llm=FakeLLMClient(), env=environment, auto_save_path=path)
    (tmp_path / "observed.txt").write_text("old\n")
    read_args = {"command": "cat observed.txt", "timeout": 10}
    await _execute(session, _call("bash", read_args, "old-read"))
    if timeout_recovery:
        program = "import time; time.sleep(0.1)"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
        for index, timeout in enumerate((0.01, 0.02)):
            await _execute(session, _call("bash", {"command": command, "timeout": timeout}, f"timeout-{index}"))
        reserved = _call("bash", {"command": command, "timeout": 1}, "reserved")
    else:
        await _execute(session, _call("bash", read_args, "second-read"))
        reserved = _call("bash", read_args, "reserved")
    for index in range(4):
        await _execute(session, _call("independent", {}, f"old-{index}"))
    assert session.state.turn.loop_state.blocked_rounds == 2
    session.state.set_phase(SessionPhase.EXECUTING_TOOLS)
    return session, environment, path, reserved


@pytest.mark.parametrize("prefix", ["edit", "fresh-read", "duplicate-unknown"])
async def test_prefix_progress_is_persisted_without_counting_an_unfinished_batch(tmp_path, prefix):
    session, environment, path, reserved = await _prepare(tmp_path, timeout_recovery=prefix != "edit")
    if prefix == "edit":
        prefix_call = _call("file_write", {
            "path": "observed.txt", "mode": "create", "content": "new\n", "overwrite": True,
        }, "prefix")
    elif prefix == "fresh-read":
        (tmp_path / "fresh.txt").write_text("fresh evidence\n")
        prefix_call = _call("file_read", {"path": "fresh.txt"}, "prefix")
    else:
        prefix_call = _call("bash", {"command": "cat observed.txt", "timeout": 10}, "prefix")

    previous_step = session.state.turn.loop_state.last_counted_step
    previous_distinct = session.state.turn.distinct_evidence_count
    previous_steps = session.state.turn.steps_since_progress
    original_checkpoint = session.tool_execution.loop_reservation_checkpoint
    async def checkpoint(results):
        await original_checkpoint(results)
        environment.interrupt = True
    session.tool_execution.loop_reservation_checkpoint = checkpoint
    try:
        with pytest.raises(asyncio.CancelledError):
            await _execute(session, prefix_call, reserved)
        raw = SessionStore().load_snapshot(path, session.agent.system_prompt)["session_state"]
        expected = 2 if prefix == "duplicate-unknown" else 0
        assert raw["loop_state"]["blocked_rounds"] == expected
        assert raw["loop_blocked_since_progress"] == expected
        assert raw["steps_since_progress"] == (previous_steps if prefix == "duplicate-unknown" else 0)
        assert raw["loop_state"]["last_counted_step"] == previous_step
        assert raw["distinct_evidence_count"] == previous_distinct
        assert [row["tool_call_id"] for row in raw["loop_checkpoint_results"]] == ["prefix"]
        restored = load_session(path, agent=session.agent, llm=FakeLLMClient(), env=environment)
        assert restored.state.turn.loop_state.blocked_rounds == expected
        await _execute(restored, _call("independent", {}, "after-restore"))
        assert restored.state.turn.loop_state.blocked_rounds == expected + 1
    finally:
        await environment.cleanup()


async def test_normal_batch_still_folds_its_evidence_and_model_step_once(tmp_path):
    session, environment, _, reserved = await _prepare(tmp_path)
    previous_step = session.state.turn.loop_state.last_counted_step
    previous_distinct = session.state.turn.distinct_evidence_count
    checkpoints = []
    original_checkpoint = session.tool_execution.loop_reservation_checkpoint
    async def checkpoint(results):
        checkpoints.append((
            session.state.turn.loop_state.last_counted_step, session.state.turn.distinct_evidence_count,
        ))
        await original_checkpoint(results)
    session.tool_execution.loop_reservation_checkpoint = checkpoint
    try:
        result = await _execute(session, _call("file_write", {
            "path": "observed.txt", "mode": "create", "content": "new\n", "overwrite": True,
        }, "prefix"), reserved)
        assert checkpoints == [(previous_step, previous_distinct)]
        assert session.state.turn.loop_state.last_counted_step == session.state.step_count
        assert session.state.turn.distinct_evidence_count == previous_distinct + 1
        assert session.state.turn.loop_state.blocked_rounds == 0
        result.apply_evidence_counter_to(session.state)
        assert session.state.turn.distinct_evidence_count == previous_distinct + 1
        assert session.state.turn.loop_state.blocked_rounds == 0
    finally:
        await environment.cleanup()
