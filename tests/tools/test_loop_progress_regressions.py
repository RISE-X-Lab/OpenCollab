"""Exercise loop recovery with native tools and disposable local workspaces."""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.domain.session import SessionPhase, SessionState
from tests.support.session_run_loop_test_support import build_runner
from tests.support.tool_execution_test_support import FakeAgent, RuntimeNativeTool, build_use_case, tool_call


class _RecordingEnvironment(LocalEnvironment):
    def __init__(self, workspace):
        super().__init__(str(workspace))
        self.commands = []

    async def exec_cmd(self, cmd, timeout=120.0):
        self.commands.append((cmd, timeout))
        return await super().exec_cmd(cmd, timeout=timeout)


@pytest.fixture
async def local_tools(tmp_path):
    environment = _RecordingEnvironment(tmp_path)
    state = SessionState(messages=[])
    executor, _ = build_use_case(
        state=state,
        agent=FakeAgent(tools=[BashTool(), FileReadTool(), FileWriteTool()]),
        environment=environment,
    )
    yield executor, state, environment
    await environment.cleanup()


def _call(name, args, call_id):
    return tool_call(name, json.dumps(args), call_id=call_id)


async def _step(executor, state, *calls):
    state.advance_step()
    result = await executor.process(list(calls))
    result.apply_to(state)
    return result


def _read(call_id, *, timeout=10):
    return _call("bash", {"command": "cat observed.txt", "timeout": timeout}, call_id)


def _write(content, call_id):
    return _call(
        "file_write",
        {"path": "observed.txt", "mode": "create", "content": content, "overwrite": True},
        call_id,
    )


@pytest.mark.parametrize("same_batch", [False, True])
async def test_confirmed_edit_allows_one_retest_without_recounting_the_call(local_tools, tmp_path, same_batch):
    executor, state, environment = local_tools
    (tmp_path / "observed.txt").write_text("FAIL\n")
    for index in range(2):
        await _step(executor, state, _read(f"before-{index}"))

    if same_batch:
        result = await _step(executor, state, _write("PASS\n", "edit"), _read("retest"))
    else:
        await _step(executor, state, _write("PASS\n", "edit"))
        result = await _step(executor, state, _read("retest"))

    assert result.loop_detections == []
    assert "PASS" in result.messages_to_append[-1]["content"]
    assert len(environment.commands) == 3
    assert state.turn.steps_since_progress == 0
    assert state.turn.loop_blocked_since_progress == 0

    blocked = await _step(executor, state, _read("duplicate"))
    assert len(blocked.loop_detections) == 1
    assert len(environment.commands) == 3


async def test_same_content_overwrite_keeps_physical_write_without_progress_reward(local_tools, tmp_path):
    executor, state, environment = local_tools
    target = tmp_path / "observed.txt"
    target.write_text("same bytes\n")
    before_inode = target.stat().st_ino
    state.turn.steps_since_progress = 2
    state.turn.low_yield_since_progress = 2
    state.turn.loop_blocked_since_progress = 2

    result = await _step(executor, state, _write("same bytes\n", "noop"))

    assert target.read_text() == "same bytes\n"
    assert target.stat().st_ino != before_inode
    assert not result.write_succeeded
    assert state.turn.steps_since_progress >= 2
    assert state.turn.low_yield_since_progress >= 2
    assert state.turn.loop_blocked_since_progress == 2
    assert environment.commands == []


async def test_noop_overwrite_cannot_reopen_an_exhausted_read(local_tools, tmp_path):
    executor, state, environment = local_tools
    (tmp_path / "observed.txt").write_text("same\n")
    for index in range(2):
        await _step(executor, state, _read(f"read-{index}"))
    result = await _step(executor, state, _write("same\n", "noop"), _read("still-blocked"))
    assert len(result.loop_detections) == 1
    assert len(environment.commands) == 2


async def test_timeout_drift_preserves_actual_parameters_but_shares_repeat_identity(local_tools, tmp_path):
    executor, state, environment = local_tools
    (tmp_path / "observed.txt").write_text("stable\n")
    outputs = []
    for index, timeout in enumerate((10, 11, 12, 13)):
        outputs.append(await _step(executor, state, _read(str(index), timeout=timeout)))

    assert [timeout for _, timeout in environment.commands] == [10, 11]
    assert outputs[0].loop_detections == outputs[1].loop_detections == []
    assert len(outputs[2].loop_detections) == len(outputs[3].loop_detections) == 1


async def test_two_actual_timeouts_allow_one_larger_attempt_and_no_further_credit(local_tools):
    executor, state, environment = local_tools
    program = 'import time; time.sleep(0.2); print("PASS")'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    results = []
    for index, timeout in enumerate((0.02, 0.04, 2.0, 3.0)):
        results.append(await _step(
            executor, state, _call("bash", {"command": command, "timeout": timeout}, str(index))
        ))

    assert all("timed out" in item.messages_to_append[0]["content"] for item in results[:2])
    assert "PASS" in results[2].messages_to_append[0]["content"]
    assert results[2].loop_detections == []
    assert state.turn.steps_since_progress >= 1
    assert len(results[3].loop_detections) == 1
    assert [timeout for _, timeout in environment.commands] == [0.02, 0.04, 2.0]


async def test_timeout_epsilon_after_exhausted_recovery_stays_blocked(local_tools):
    executor, state, environment = local_tools
    program = "import time; time.sleep(1)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    results = []
    for index, timeout in enumerate((0.02, 0.04, 0.041, 0.042, 0.043)):
        results.append(await _step(
            executor, state, _call("bash", {"command": command, "timeout": timeout}, str(index))
        ))
    assert len(environment.commands) == 3
    assert all("timed out" in item.messages_to_append[0]["content"] for item in results[:3])
    assert all(len(item.loop_detections) == 1 for item in results[3:])


async def test_requested_timeout_above_the_outer_deadline_does_not_buy_recovery(local_tools, monkeypatch):
    from opencollab.application import tool_execution_runtime

    monkeypatch.setattr(tool_execution_runtime, "MAX_TOOL_EXECUTION_TIMEOUT", 0.02)
    executor, state, environment = local_tools
    program = "import time; time.sleep(0.2)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"
    results = []
    for index, timeout in enumerate((0.1, 0.2, 0.3)):
        results.append(await _step(
            executor, state, _call("bash", {"command": command, "timeout": timeout}, str(index)),
        ))
    assert all("timed out" in result.messages_to_append[0]["content"] for result in results[:2])
    assert len(results[2].loop_detections) == 1
    assert len(environment.commands) == 2


async def test_printing_timeout_text_and_exiting_124_does_not_grant_recovery(local_tools):
    executor, state, environment = local_tools
    command = "printf 'Command timed out after 10s\\n'; exit 124"
    results = []
    for index, timeout in enumerate((10, 11, 12)):
        results.append(await _step(
            executor, state, _call("bash", {"command": command, "timeout": timeout}, str(index)),
        ))
    assert all("Exit code: 124" in result.messages_to_append[0]["content"] for result in results[:2])
    assert len(results[2].loop_detections) == 1
    assert len(environment.commands) == 2


async def test_variable_output_cannot_create_recovery_credits(local_tools):
    executor, state, environment = local_tools
    command = "printf 'noise-%s\\n' \"$$\""
    results = []
    for index, timeout in enumerate((10, 11, 12, 13)):
        results.append(await _step(
            executor, state, _call("bash", {"command": command, "timeout": timeout}, str(index))
        ))
    assert results[0].messages_to_append[0]["content"] != results[1].messages_to_append[0]["content"]
    assert len(environment.commands) == 2
    assert all(len(item.loop_detections) == 1 for item in results[2:])


async def test_quoted_shell_commands_keep_distinct_execution_semantics(local_tools):
    executor, state, environment = local_tools
    commands = [
        "LABEL=ALPHA; printf '%s\\n' '$LABEL'",
        'LABEL=ALPHA; printf \'%s\\n\' "$LABEL"',
    ]
    assert shlex.split(commands[0]) == shlex.split(commands[1])
    results = []
    for index, command in enumerate(commands):
        results.append(await _step(executor, state, _call("bash", {"command": command}, str(index))))
    assert all(item.loop_detections == [] for item in results)
    assert "$LABEL" in results[0].messages_to_append[0]["content"]
    assert "ALPHA" in results[1].messages_to_append[0]["content"]
    assert [command for command, _ in environment.commands] == commands


async def test_custom_tool_named_bash_retains_its_timeout_parameter():
    class SemanticTimeoutTool(RuntimeNativeTool):
        name = "bash"

        async def execute_with_runtime(self, args, runtime):
            self.runtime_calls.append((args, runtime))
            return f"semantic timeout {args['timeout']}"

    state = SessionState(messages=[])
    tool = SemanticTimeoutTool()
    executor, _ = build_use_case(state=state, agent=FakeAgent(tools=[tool]))
    for index, timeout in enumerate((10, 11, 12)):
        result = await _step(executor, state, _call("bash", {"command": "query", "timeout": timeout}, str(index)))
        assert result.loop_detections == []
    assert [args["timeout"] for args, _ in tool.runtime_calls] == [10, 11, 12]


async def test_fresh_read_evidence_resets_warning_when_an_old_operation_is_blocked(local_tools, tmp_path):
    executor, state, environment = local_tools
    (tmp_path / "observed.txt").write_text("old\n")
    (tmp_path / "pages.txt").write_text("alpha\nbeta\ngamma\n")
    for index in range(2):
        await _step(executor, state, _read(f"prime-{index}"))
    for index in range(3):
        result = await _step(
            executor, state, _read(f"blocked-{index}"),
            _call("file_read", {"path": "pages.txt", "offset": index + 1, "limit": 1}, f"page-{index}"),
        )
        assert len(result.loop_detections) == 1
        assert state.turn.loop_blocked_since_progress == 0
    assert len(environment.commands) == 2


async def test_unknown_bash_effect_cannot_mask_confirmed_repeat_blocks(local_tools):
    executor, state, environment = local_tools
    probe = RuntimeNativeTool(output="stable observation")
    probe.name = "probe"
    executor.agent.tools.append(probe)
    for index in range(2):
        await _step(executor, state, _call("probe", {"query": "same"}, f"prime-{index}"))

    for index in range(5):
        result = await _step(
            executor, state,
            _call("probe", {"query": "same"}, f"blocked-{index}"),
            _call("bash", {"command": f"true # {index}"}, f"unknown-{index}"),
        )
        assert len(result.loop_detections) == 1
        assert state.turn.loop_state.blocked_rounds == index
        if index:
            assert state.turn.steps_since_progress == index
            assert not state.turn.last_progress_unknown
        state.set_phase(SessionPhase.PRECHECK)
        await build_runner(state=state).precheck(None)
        if index == 3:
            assert state.phase is SessionPhase.STOPPED
            break
        assert state.phase is SessionPhase.CALLING_LLM

    assert len(environment.commands) == 4
    assert len(probe.runtime_calls) == 2
