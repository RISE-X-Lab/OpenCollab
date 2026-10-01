"""Check batch warnings and finite recovery opportunities through session saves."""

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
from opencollab.bootstrap import build_session as Session
from opencollab.bootstrap import load_session
from opencollab.domain.session import SessionPhase, SessionState
from tests.support.session_characterization_test_support import FakeAgent, FakeLLMClient
from tests.support.session_run_loop_test_support import build_runner
from tests.support.session_runtime_test_support import tool_call
from tests.support.tool_execution_test_support import FakeAgent as ToolAgent
from tests.support.tool_execution_test_support import build_use_case


class _RecordingEnvironment(LocalEnvironment):
    def __init__(self, workspace):
        super().__init__(str(workspace))
        self.commands = []

    async def exec_cmd(self, cmd, timeout=120.0):
        self.commands.append((cmd, timeout))
        return await super().exec_cmd(cmd, timeout=timeout)


def _call(name, args, call_id):
    return tool_call(call_id, name, json.dumps(args))


def _read(call_id):
    return _call("bash", {"command": "cat observed.txt", "timeout": 10}, call_id)


async def _execute(session, *calls):
    session.state.advance_step()
    session.state.append_message({"role": "assistant", "content": None, "tool_calls": list(calls)})
    result = await session.tool_execution.process(list(calls))
    result.apply_to(session.state)
    return result


async def test_three_blocked_calls_in_one_batch_give_one_warning_then_three_batches_stop(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    (tmp_path / "observed.txt").write_text("stable\n")
    state = SessionState(messages=[])
    executor, _ = build_use_case(
        state=state, agent=ToolAgent(tools=[BashTool()]), environment=environment,
    )
    try:
        for index in range(2):
            state.advance_step()
            (await executor.process([_read(f"prime-{index}")])).apply_to(state)
        for batch in range(3):
            calls = [_read(f"blocked-{batch}-{index}") for index in range(3)]
            state.advance_step()
            result = await executor.process(calls)
            result.apply_to(state)
            assert len(result.loop_detections) == 3
            assert [message["tool_call_id"] for message in result.messages_to_append] == [c["id"] for c in calls]
            assert state.turn.loop_state.blocked_rounds == batch + 1
            state.set_phase(SessionPhase.PRECHECK)
            await build_runner(state=state).precheck(None)
            expected = SessionPhase.STOPPED if batch == 2 else SessionPhase.CALLING_LLM
            assert state.phase is expected
        assert len(environment.commands) == 2
    finally:
        await environment.cleanup()


async def test_edit_validation_consumed_before_save_stays_consumed_after_load(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    agent = FakeAgent(tools=[BashTool(), FileReadTool(), FileWriteTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment)
    (tmp_path / "observed.txt").write_text("FAIL\n")
    try:
        for index in range(2):
            await _execute(session, _read(f"prime-{index}"))
        edit = _call(
            "file_write", {"path": "observed.txt", "mode": "create", "content": "PASS\n", "overwrite": True}, "edit",
        )
        result = await _execute(session, edit, _read("retest"))
        assert result.loop_detections == []
        assert "PASS" in result.messages_to_append[-1]["content"]
        path = tmp_path / "session.json"
        session.save(str(path))
        restored = load_session(str(path), agent=agent, llm=FakeLLMClient(), env=environment)
        blocked = await _execute(restored, _read("after-load"))
        assert len(blocked.loop_detections) == 1
        assert len(environment.commands) == 3
        assert restored.state.turn.loop_state.edit_seq == session.state.turn.loop_state.edit_seq
    finally:
        await environment.cleanup()


async def test_successful_timeout_recovery_cannot_be_reissued_after_restore(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    agent = FakeAgent(tools=[BashTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment)
    program = 'import time; time.sleep(0.2); print("PASS")'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    try:
        results = []
        for index, timeout in enumerate((0.02, 0.04, 2.0)):
            results.append(await _execute(
                session, _call("bash", {"command": command, "timeout": timeout}, str(index)),
            ))
        assert all("timed out" in r.messages_to_append[0]["content"] for r in results[:2])
        assert "PASS" in results[2].messages_to_append[0]["content"]
        path = tmp_path / "timeout-session.json"
        session.save(str(path))
        restored = load_session(str(path), agent=agent, llm=FakeLLMClient(), env=environment)
        result = await _execute(restored, _call("bash", {"command": command, "timeout": 3.0}, "after-load"))
        assert len(result.loop_detections) == 1
        assert [timeout for _, timeout in environment.commands] == [0.02, 0.04, 2.0]
    finally:
        await environment.cleanup()


async def test_blocked_round_count_round_trips_without_recounting_messages(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    agent = FakeAgent(tools=[BashTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment)
    (tmp_path / "observed.txt").write_text("stable\n")
    try:
        for index in range(2):
            await _execute(session, _read(f"prime-{index}"))
        await _execute(session, *[_read(f"blocked-{index}") for index in range(3)])
        assert session.state.turn.loop_state.blocked_rounds == 1
        path = tmp_path / "warnings.json"
        session.save(str(path))
        restored = load_session(str(path), agent=agent, llm=FakeLLMClient(), env=environment)
        assert restored.state.turn.loop_state.blocked_rounds == 1
        await _execute(restored, *[_read(f"next-{index}") for index in range(3)])
        assert restored.state.turn.loop_state.blocked_rounds == 2
        assert len(environment.commands) == 2
    finally:
        await environment.cleanup()


async def test_new_user_turn_clears_recovery_window_but_preserves_lifetime_steps(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    agent = FakeAgent(tools=[BashTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment)
    (tmp_path / "observed.txt").write_text("stable\n")
    try:
        for index in range(3):
            await _execute(session, _read(f"old-{index}"))
        assert len(environment.commands) == 2
        steps = session.state.step_count
        await session.add_user_message("Read the file for this new request.")
        result = await _execute(session, _read("fresh"))
        assert result.loop_detections == []
        assert len(environment.commands) == 3
        assert session.state.step_count == steps + 1
        assert session.state.turn.loop_state.blocked_rounds == 0
    finally:
        await environment.cleanup()


async def test_failed_reservation_save_prevents_the_extra_native_command(tmp_path):
    class FailingStore(SessionStore):
        fail = False

        def append_snapshot_delta(self, *args, **kwargs):
            if self.fail:
                raise OSError("reservation disk write failed")
            return super().append_snapshot_delta(*args, **kwargs)

    environment = _RecordingEnvironment(tmp_path)
    store = FailingStore()
    path = tmp_path / "failed-save.json"
    agent = FakeAgent(tools=[BashTool(), FileWriteTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment, store=store, auto_save_path=str(path))
    (tmp_path / "observed.txt").write_text("FAIL\n")
    try:
        for index in range(2):
            await _execute(session, _read(f"prime-{index}"))
        edit = _call(
            "file_write", {"path": "observed.txt", "mode": "create", "content": "PASS\n", "overwrite": True}, "edit",
        )
        await _execute(session, edit)
        session.save(str(path))
        store.fail = True
        with pytest.raises(RuntimeError, match="persist"):
            await _execute(session, _read("unpersisted-retest"))
        assert len(environment.commands) == 2
        assert session.persistence_errors
        assert (tmp_path / "observed.txt").read_text() == "PASS\n"
    finally:
        await environment.cleanup()


async def test_cancel_after_reserved_dispatch_restores_prefix_and_never_reissues_it(tmp_path):
    class CancelAfterReadEnvironment(_RecordingEnvironment):
        async def exec_cmd(self, cmd, timeout=120.0):
            result = await super().exec_cmd(cmd, timeout=timeout)
            if len(self.commands) == 3:
                raise asyncio.CancelledError
            return result

    environment = CancelAfterReadEnvironment(tmp_path)
    path = tmp_path / "interrupted.json"
    agent = FakeAgent(tools=[BashTool(), FileWriteTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment, auto_save_path=str(path))
    (tmp_path / "observed.txt").write_text("FAIL\n")
    try:
        for index in range(2):
            await _execute(session, _read(f"prime-{index}"))
        session.state.set_phase(SessionPhase.EXECUTING_TOOLS)
        edit = _call(
            "file_write", {"path": "observed.txt", "mode": "create", "content": "PASS\n", "overwrite": True}, "edit",
        )
        with pytest.raises(asyncio.CancelledError):
            await _execute(session, edit, _read("interrupted-retest"))
        assert len(environment.commands) == 3
        # The public Session facade publishes a terminal snapshot on cancellation.
        # That later save must retain the completed prefix of the interrupted batch.
        session.state.cancel()
        await session._checkpoint_terminal_snapshot()
        saved = SessionStore().load_snapshot(str(path), agent.system_prompt)
        prior_results = [m["tool_call_id"] for m in saved["messages"] if m["role"] == "tool"]
        assert prior_results == ["prime-0", "prime-1"]
        completed = saved["session_state"]["loop_checkpoint_results"]
        assert [m["tool_call_id"] for m in completed] == ["edit"]
        assert saved["session_state"]["loop_state"]["edit_seq"] == 1
        records = saved["session_state"]["loop_state"]["operations"].values()
        reserved = [record for record in records if record["inflight_tool_call_id"] == "interrupted-retest"]
        assert len(reserved) == 1
        assert reserved[0]["validation_reserved_edit_seq"] == 1

        restored = load_session(str(path), agent=agent, llm=FakeLLMClient(), env=environment)
        restored_results = [m for m in restored.state.messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in restored_results] == [
            "prime-0", "prime-1", "edit", "interrupted-retest",
        ]
        assert restored_results[2]["content"] == completed[0]["content"]
        result = await _execute(restored, _read("after-interruption"))
        assert "unresolved" in result.messages_to_append[0]["content"]
        assert result.loop_detections == []
        assert len(environment.commands) == 3
        assert restored.state.turn.loop_state.blocked_rounds == 0
        assert (tmp_path / "observed.txt").read_text() == "PASS\n"
    finally:
        await environment.cleanup()


async def test_completed_retry_before_later_cancellation_restores_a_completed_owner(tmp_path):
    class CancelTool:
        name = "cancel_tool"

        async def execute_with_runtime(self, args, runtime):
            raise asyncio.CancelledError

    environment = _RecordingEnvironment(tmp_path)
    path = tmp_path / "later-cancel.json"
    agent = FakeAgent(tools=[BashTool(), FileWriteTool(), CancelTool()])
    session = Session(agent=agent, llm=FakeLLMClient(), env=environment, auto_save_path=str(path))
    (tmp_path / "observed.txt").write_text("FAIL\n")
    try:
        for index in range(2):
            await _execute(session, _read(f"prime-{index}"))
        session.state.set_phase(SessionPhase.EXECUTING_TOOLS)
        edit = _call(
            "file_write", {"path": "observed.txt", "mode": "create", "content": "PASS\n", "overwrite": True}, "edit",
        )
        with pytest.raises(asyncio.CancelledError):
            await _execute(session, edit, _read("completed-retry"), _call("cancel_tool", {}, "later-cancel"))
        session.state.cancel()
        await session._checkpoint_terminal_snapshot()
        restored = load_session(str(path), agent=agent, llm=FakeLLMClient(), env=environment)
        results = [m for m in restored.messages if m.get("tool_call_id") == "completed-retry"]
        assert len(results) == 1 and "PASS" in results[0]["content"]
        operation = next(
            item for key, item in restored.state.turn.loop_state.operations.items()
            if key == restored.tool_execution.tool_call_hash("bash", {"command": "cat observed.txt", "timeout": 10})
        )
        assert operation.inflight_tool_call_id is None
        assert operation.last_exit_kind == "completed"
    finally:
        await environment.cleanup()
