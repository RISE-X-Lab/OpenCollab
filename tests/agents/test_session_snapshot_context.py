"""Runnable snapshots retain the effective context view and independent owners."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.adapters.tools.submit import SubmitTool
from opencollab.application.shaping import AutoCompactShaper, PerToolResultBudgetShaper
from opencollab.bootstrap import SnapshotSessionError, build_session, load_session, snapshot_session
from opencollab.bootstrap.agent_profiles import resolve_agent_profile
from opencollab.bootstrap.context_policy import ContextPolicy
from opencollab.domain.agent import Agent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from tests.runtime.test_async_compaction import history
from tests.runtime.test_compaction_accounting import AccountingModel
from tests.support.scheduler_awaiting_test_support import build_scheduler
from tests.support.session_characterization_test_support import FakeLLMClient, llm_response

_MARKER = "END_OF_FIXTURE"

class _ForkableEnvironment(LocalEnvironment):
    def fork_snapshot(self):
        directory = tempfile.mkdtemp(prefix="snapshot-", dir=Path(self.workspace).parent)
        shutil.copytree(self.workspace, directory, dirs_exist_ok=True)
        return _ForkableEnvironment(directory)

class _MarkerProvider:
    def __init__(self):
        self.calls = []

    def context_window(self):
        return 128_000

    async def complete(self, messages, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        results = [message["content"] for message in messages if message["role"] == "tool"]
        if not results:
            return llm_response(tool_calls=[{
                "id": "read", "type": "function",
                "function": {"name": "file_read", "arguments": '{"path":"fixture.txt"}'},
            }], finish_reason="tool_calls")
        return llm_response(content="FOUND" if any(_MARKER in result for result in results) else "MISSING")


@pytest.mark.parametrize("event_type", ["tool_end", "tool_start", "step_end"])
@pytest.mark.parametrize("autosave", [False, True])
async def test_snapshot_preserves_completed_native_output_before_the_batch_is_published(
    tmp_path, event_type, autosave,
):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "input.txt").write_text("one-time-value\n")
    held, release = asyncio.Event(), asyncio.Event()
    events, requests = [], []

    def call(name, arguments, ident):
        return {
            "id": ident, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def command(code):
        return shlex.join((sys.executable, "-B", "-c", code))

    class Model:
        async def complete(self, messages, **kwargs):
            requests.append(copy.deepcopy(messages))
            results = {row["tool_call_id"]: row["content"] for row in messages if row["role"] == "tool"}
            if not results:
                consume = (
                    "from pathlib import Path\n"
                    "p=Path('input.txt')\nvalue=p.read_text()\np.unlink()\n"
                    "with Path('executions.txt').open('a') as stream: stream.write('once\\n')\n"
                    "print('CONSUMED_'+value)\n"
                )
                return llm_response(tool_calls=[
                    call("bash", {"command": command(consume)}, "consume"),
                    call("bash", {"command": command("print('SECOND_EXECUTED')")}, "second"),
                ], finish_reason="tool_calls")
            if "delivery" not in results:
                assert "CONSUMED_one-time-value" in results["consume"]
                verify = (
                    "from pathlib import Path\n"
                    "assert not Path('input.txt').exists()\n"
                    "assert Path('executions.txt').read_text()=='once\\n'\n"
                    f"Path('delivery.txt').write_text({results['consume']!r})\n"
                    "print('DELIVERY_EXECUTED')\n"
                )
                return llm_response(tool_calls=[
                    call("bash", {"command": command(verify)}, "delivery"),
                ], finish_reason="tool_calls")
            assert "DELIVERY_EXECUTED" in results["delivery"]
            return llm_response(tool_calls=[
                call("submit", {"summary": "Delivered the consumed value."}, "submit"),
            ], finish_reason="tool_calls")

    async def hold_snapshot(event):
        events.append(event)
        target = {"tool_end": "consume", "tool_start": "second"}.get(event_type)
        if event.type == event_type and not held.is_set() and (
            target is None or event.data.get("tool_call_id") == target
        ):
            held.set()
            await release.wait()

    agent = Agent(name="worker", system_prompt="Deliver the command's output.", tools=[BashTool(), SubmitTool()])
    model = Model()
    source = build_session(
        agent=agent, env=_ForkableEnvironment(str(workspace)), llm=model,
        event_sink=hold_snapshot,
        auto_save_path=str(tmp_path / "live.json") if autosave else None,
    )
    branch = restored = None
    await source.add_user_message("Read and consume the one-time input, then deliver its value.")
    running = asyncio.create_task(source.run_loop())
    try:
        await asyncio.wait_for(held.wait(), 5)
        branch = snapshot_session(source)
        saved = tmp_path / "snapshot.json"
        source.save(str(saved))
        restored_workspace = tmp_path / "restored"
        shutil.copytree(workspace, restored_workspace)
        restored = load_session(
            str(saved), agent=agent, env=LocalEnvironment(str(restored_workspace)), llm=model,
        )
        for session in (branch, restored):
            assert await session.run_loop() == "Delivered the consumed value."
            assert session.phase is SessionPhase.DONE
            directory = Path(session.env.workspace)
            assert "CONSUMED_one-time-value" in (directory / "delivery.txt").read_text()
            assert (directory / "executions.txt").read_text() == "once\n"
            assert [row["tool_call_id"] for row in session.messages if row["role"] == "tool"] == [
                "consume", "second", "delivery", "submit",
            ]
            second = next(row["content"] for row in session.messages if row.get("tool_call_id") == "second")
            assert ("SECOND_EXECUTED" in second) is (event_type == "step_end")
        release.set()
        assert await asyncio.wait_for(running, 5) == "Delivered the consumed value."
        assert (workspace / "executions.txt").read_text() == "once\n"
        assert "CONSUMED_one-time-value" in (workspace / "delivery.txt").read_text()
        assert len(requests) == 7
        assert [(event.type, event.data["tool_call_id"]) for event in events
                if event.type in {"tool_start", "tool_end"}] == [
            ("tool_start", "consume"), ("tool_end", "consume"),
            ("tool_start", "second"), ("tool_end", "second"),
            ("tool_start", "delivery"), ("tool_end", "delivery"),
            ("tool_start", "submit"), ("tool_end", "submit"),
        ]
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for session in (branch, restored, source):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


@pytest.mark.parametrize("autosave", [False, True])
async def test_cancelled_native_batch_keeps_the_output_of_completed_commands(tmp_path, autosave):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "input.txt").write_text("one-time-value\n")

    class Model:
        async def complete(self, messages, **kwargs):
            results = {row["tool_call_id"]: row["content"] for row in messages if row["role"] == "tool"}
            if results:
                return llm_response(content=results["consume"])
            commands = [
                ("consume", "from pathlib import Path\np=Path('input.txt')\nvalue=p.read_text()\np.unlink()\n"
                 "with Path('executions.txt').open('a') as stream: stream.write('once\\n')\n"
                 "print('CONSUMED_'+value)\n"),
                ("waiting", "import time\nfrom pathlib import Path\nPath('started').touch()\n"
                 "while not Path('release').exists(): time.sleep(.01)\nPath('late.txt').touch()\n"),
            ]
            return llm_response(tool_calls=[{
                "id": ident, "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({
                        "command": shlex.join((sys.executable, "-B", "-c", code)),
                    }),
                },
            } for ident, code in commands], finish_reason="tool_calls")

    model = Model()
    agent = Agent(name="worker", system_prompt="Return the consumed output.", tools=[BashTool()])
    source = build_session(
        agent=agent, env=LocalEnvironment(str(workspace)), llm=model,
        auto_save_path=str(tmp_path / "live.json") if autosave else None,
    )
    restored = None
    await source.add_user_message("Consume the input, then wait.")
    running = asyncio.create_task(source.run_loop())
    try:
        async def wait_for_start():
            while not (workspace / "started").exists():
                await asyncio.sleep(.01)

        await asyncio.wait_for(wait_for_start(), 5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert source.phase is SessionPhase.STOPPED
        results = [row for row in source.messages if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in results] == ["consume", "waiting"]
        assert "CONSUMED_one-time-value" in results[0]["content"]
        assert "interrupted by session cancellation" in results[1]["content"]
        saved = tmp_path / "cancelled.json"
        source.save(str(saved))
        restored = load_session(str(saved), agent=agent, env=LocalEnvironment(str(workspace)), llm=model)
        for session in (source, restored):
            await session.add_user_message("Return the completed command's output.")
            assert "CONSUMED_one-time-value" in await session.run_loop()
            assert (workspace / "executions.txt").read_text() == "once\n"
            assert not (workspace / "input.txt").exists()
        (workspace / "release").touch()
        await asyncio.sleep(.05)
        assert not (workspace / "late.txt").exists()
    finally:
        (workspace / "release").touch()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for session in (source, restored):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


@pytest.mark.parametrize("event_type", ["tool_end", "text_delta", "step_end"])
@pytest.mark.parametrize("max_steps", [1, 10])
@pytest.mark.parametrize("autosave", [False, True])
async def test_submitted_snapshot_finishes_without_another_model_call(tmp_path, event_type, max_steps, autosave):
    workspace = tmp_path / "source"
    workspace.mkdir()
    summary = "Saved the verified result."
    held, release = asyncio.Event(), asyncio.Event()
    events = []

    async def hold_submission(event):
        events.append(event)
        accepted = event.data.get("tool") == "submit" if event_type == "tool_end" else (
            source.state.submitted_summary == summary
        )
        if event.type == event_type and accepted:
            held.set()
            await release.wait()

    model = FakeLLMClient([llm_response(tool_calls=[
        {"id": "write", "type": "function", "function": {
            "name": "file_write", "arguments": json.dumps({
                "path": "result.txt", "mode": "create", "content": "verified result\n",
            }),
        }},
        {"id": "submit", "type": "function", "function": {
            "name": "submit", "arguments": json.dumps({"summary": summary}),
        }},
        {"id": "skipped", "type": "function", "function": {
            "name": "file_write", "arguments": json.dumps({
                "path": "skipped.txt", "mode": "create", "content": "late write\n",
            }),
        }},
    ], finish_reason="tool_calls")])
    agent = Agent(name="worker", system_prompt="Write and submit the result.", tools=[FileWriteTool(), SubmitTool()])
    source = build_session(
        agent=agent, env=_ForkableEnvironment(str(workspace)), llm=model,
        event_sink=hold_submission, max_steps=max_steps,
        auto_save_path=str(tmp_path / "live.json") if autosave else None,
    )
    branch = restored = None
    await source.add_user_message("Write and submit the verified result")
    running = asyncio.create_task(source.run_loop())
    try:
        await asyncio.wait_for(held.wait(), 3)
        assert source.phase is (
            SessionPhase.EXECUTING_TOOLS if event_type == "tool_end" else SessionPhase.AUTOSAVING
        )
        if event_type != "tool_end":
            assert source.messages[-1] == {"role": "assistant", "content": summary}
        branch = snapshot_session(source)
        saved = tmp_path / "submitted.json"
        source.save(str(saved))
        restored_workspace = tmp_path / "restored"
        shutil.copytree(workspace, restored_workspace)
        restored = load_session(
            str(saved), agent=agent, env=LocalEnvironment(str(restored_workspace)),
            llm=model, max_steps=max_steps,
            auto_save_path=str(tmp_path / "restored-autosave.json") if event_type == "tool_end" else None,
        )
        if event_type == "tool_end":
            recovered_messages = restored.state.enriched_messages()
            restored.restore(str(saved))
            assert restored.state.enriched_messages() == recovered_messages
        for session in (branch, restored):
            assert await session.run_loop() == summary
            assert session.phase is SessionPhase.DONE
            assert session.state.terminal_reason == "submitted"
            assert session.used_tokens == source.used_tokens
            assert sum(message == {"role": "assistant", "content": summary} for message in session.messages) == 1
            assert (Path(session.env.workspace) / "result.txt").read_text() == "verified result\n"
            assert not (Path(session.env.workspace) / "skipped.txt").exists()
            session.save(str(tmp_path / f"completed-{id(session)}.json"))
            assert await session.run_loop() == summary
        assert len(model.calls) == 1
        release.set()
        assert await asyncio.wait_for(running, 3) == summary
        assert source.phase is SessionPhase.DONE
        assert not (workspace / "skipped.txt").exists()
        assert sum(message == {"role": "assistant", "content": summary} for message in source.messages) == 1
        assert [(event.type, event.data["tool"]) for event in events if event.type in {"tool_start", "tool_end"}] == [
            ("tool_start", "file_write"), ("tool_end", "file_write"),
            ("tool_start", "submit"), ("tool_end", "submit"),
        ]
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for session in (branch, restored, source):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


async def test_snapshot_during_native_tool_execution_resumes_without_replaying_it(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "work.py").write_text(
        "import time\nfrom pathlib import Path\n"
        "Path('started').touch()\n"
        "while not Path('release').exists(): time.sleep(.01)\n"
        "Path('native.txt').write_text('completed')\n"
    )

    class Model:
        async def complete(self, messages, **kwargs):
            if any(message.get("role") == "tool" for message in messages):
                return llm_response(content="resume accepted")
            return llm_response(tool_calls=[{
                "id": "native", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({
                        "command": shlex.quote(sys.executable) + " work.py", "timeout": 5,
                    }),
                },
            }], finish_reason="tool_calls")

    agent = Agent(name="worker", system_prompt="Run the command.", tools=[BashTool()])
    model = Model()
    source = build_session(agent=agent, env=_ForkableEnvironment(str(source_dir)), llm=model)
    branch = restored = None
    await source.add_user_message("Run the local command")
    running = asyncio.create_task(source.run_loop())
    try:
        async def wait_for_start() -> None:
            while not (source_dir / "started").exists():
                await asyncio.sleep(.01)

        await asyncio.wait_for(wait_for_start(), 3)
        assert source.state.phase is SessionPhase.EXECUTING_TOOLS
        branch = snapshot_session(source)
        source.save(str(tmp_path / "active.json"))
        restored_dir = tmp_path / "restored"
        shutil.copytree(source_dir, restored_dir)
        restored = build_session(agent=agent, env=LocalEnvironment(str(restored_dir)), llm=model)
        restored.restore(str(tmp_path / "active.json"))

        assert await branch.run_loop() == "resume accepted"
        assert await restored.run_loop() == "resume accepted"
        assert branch.state.phase is restored.state.phase is SessionPhase.DONE
        assert any("interrupted" in message.get("content", "") for message in branch.messages
                   if message.get("role") == "tool")
        assert not (Path(branch.env.workspace) / "native.txt").exists()

        (source_dir / "release").touch()
        assert await asyncio.wait_for(running, 3) == "resume accepted"
        assert (source_dir / "native.txt").read_text() == "completed"
    finally:
        (source_dir / "release").touch()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        for session in (branch, restored, source):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


async def test_snapshot_marks_deferred_work_without_a_branch_producer_as_interrupted(tmp_path):
    class Model:
        async def complete(self, messages, **kwargs):
            assert any("interrupted" in message.get("content", "") for message in messages
                       if message.get("role") == "tool")
            return llm_response(content="resumed")

    workspace = tmp_path / "source"
    workspace.mkdir()
    source = build_session(
        agent=Agent(name="worker", system_prompt="Work."),
        env=_ForkableEnvironment(str(workspace)), llm=Model(),
    )
    source.state.append_message({"role": "user", "content": "Continue"})
    source.state.append_message({
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "child", "type": "function",
            "function": {"name": "spawn_agent", "arguments": "{}"},
        }],
    })
    source.state.pending_events.add(PendingRow(
        tool_call_id="child", kind=RowKind.CHILD_AGENT, order=0,
        ref=9, status=RowStatus.PENDING,
    ))
    source.state.set_phase(SessionPhase.AWAITING_EVENTS)
    source.state.active_turn_start_message_index = 0
    branch = snapshot_session(source)
    try:
        assert branch.state.pending_events.rows["child"].status is RowStatus.FAILED
        assert source.state.pending_events.rows["child"].status is RowStatus.PENDING
        assert await branch.run_loop() == "resumed"
        assert branch.state.phase is SessionPhase.DONE
    finally:
        await branch.aclose()
        await branch.env.cleanup()
        await source.aclose()
        await source.env.cleanup()


async def test_stopped_snapshot_closes_an_open_deferred_tool_call(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    source = build_session(
        agent=Agent(name="worker", system_prompt="Work."),
        env=_ForkableEnvironment(str(workspace)), llm=_MarkerProvider(),
    )
    source.state.append_message({"role": "user", "content": "Continue"})
    source.state.append_message({
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "child", "type": "function",
            "function": {"name": "spawn_agent", "arguments": "{}"},
        }],
    })
    source.state.pending_events.add(PendingRow(
        tool_call_id="child", kind=RowKind.CHILD_AGENT, order=0,
        ref=9, status=RowStatus.PENDING,
    ))
    source.state.set_phase(SessionPhase.STOPPED)
    source.state.terminal_reason = "cancelled"
    branch = snapshot_session(source)
    try:
        assert source.state.pending_events.rows["child"].status is RowStatus.PENDING
        assert branch.state.pending_events.is_empty()
        assert branch._open_tool_call_ids() == []
        assert await branch.run_loop() == "MISSING"
        assert branch.state.phase is SessionPhase.DONE
    finally:
        await branch.aclose()
        await branch.env.cleanup()
        await source.aclose()
        await source.env.cleanup()


async def test_snapshot_events_follow_registered_lead_identity(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text(_MARKER)
    events = []
    source = build_session(
        agent=Agent(name="reader", system_prompt="Inspect the file.", tools=[FileReadTool()]),
        env=_ForkableEnvironment(str(workspace)), llm=_MarkerProvider(), aid=7,
    )
    snapshot = snapshot_session(source, event_sink=events.append)
    scheduler, _ = build_scheduler(snapshot, [])
    try:
        assert snapshot.state.aid == 0
        assert await scheduler.run("Read the fixture") == "FOUND"
        assert {event.data["aid"] for event in events if "aid" in event.data} == {0}
        assert {event.type for event in events} >= {"step_start", "text_delta", "tool_start", "tool_end"}
    finally:
        await scheduler.cleanup()
        await snapshot.env.cleanup()
        await source.aclose()
        await source.env.cleanup()

@pytest.mark.parametrize("configuration", ["policy", "injected", "profile"])
async def test_snapshot_keeps_the_configured_tool_result_view(tmp_path, configuration):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "fixture.txt").write_text("x" * 19_800 + "\n" + _MARKER)
    options = {
        "policy": {"context_policy": ContextPolicy(name="no_history_compaction", tool_result_budget=32_000)},
        "injected": {"shaper": PerToolResultBudgetShaper(16_000, preserve_tail=True)},
        "profile": {"agent_profile": resolve_agent_profile("single2")},
    }[configuration]
    provider = _MarkerProvider()
    source = build_session(
        agent=Agent(name="reader", system_prompt="Inspect the file.", tools=[FileReadTool()]),
        env=_ForkableEnvironment(str(workspace)), llm=provider, **options,
    )
    snapshot = None
    try:
        await source.add_user_message("Read the fixture")
        assert await source.run_loop() == "FOUND"
        snapshot = snapshot_session(source)
        assert snapshot.runner.shaper is not source.runner.shaper
        for branch in (source, snapshot):
            await branch.add_user_message("Inspect the final marker in the saved result")
            assert await branch.run_loop() == "FOUND"
            assert any(_MARKER in message.get("content", "") for message in provider.calls[-1]
                       if message["role"] == "tool")
            assert any(_MARKER in message.get("content", "") for message in branch.messages
                       if message["role"] == "tool")
        if configuration == "injected":
            snapshot.runner.shaper.max_chars = 123
            assert source.runner.shaper.max_chars == 16_000
        else:
            snapshot.runner.shaper._shapers[0].max_chars = 123
            assert source.runner.shaper._shapers[0].max_chars == (32_000 if configuration == "policy" else 16_000)
    finally:
        if snapshot is not None:
            await snapshot.aclose()
            await snapshot.env.cleanup()
        await source.aclose()
        await source.env.cleanup()

async def test_snapshot_summary_charges_its_own_budget_and_has_an_independent_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    workspace = tmp_path / "source"
    workspace.mkdir()
    provider = AccountingModel()
    source = build_session(
        agent=Agent(name="summary", system_prompt="Help the user."),
        env=_ForkableEnvironment(str(workspace)), llm=provider,
        max_budget_tokens=30_000, llm_timeout=0.5,
        auto_save_path=str(workspace / "session.json"),
    )
    source.messages = history()
    snapshot = None
    try:
        await source.runner._shape_and_trace(source.messages)
        assert source.used_tokens == 7_600
        snapshot = snapshot_session(source)
        source_compact = next(layer for layer in source.runner.shaper._shapers if isinstance(layer, AutoCompactShaper))
        branch_compact = next(
            layer for layer in snapshot.runner.shaper._shapers if isinstance(layer, AutoCompactShaper)
        )
        assert branch_compact is not source_compact
        assert branch_compact.summarizer is not source_compact.summarizer
        assert branch_compact.summarizer._transcript_path is None
        assert source_compact.summarizer._transcript_path == source.auto_save_path
        assert snapshot.runner._per_call_timeout == 0.5
        source_cache = copy.deepcopy(source_compact._summary_cache)
        assert await snapshot.run_loop() == "answer"
        assert snapshot.used_tokens == 15_210
        assert source.used_tokens == 7_600
        assert [call["summary"] for call in provider.calls] == [True, True, False]
        assert source_compact._summary_cache == source_cache
        branch_compact._summary_cache.clear()
        assert source_compact._summary_cache == source_cache
    finally:
        if snapshot is not None:
            await snapshot.aclose()
            await snapshot.env.cleanup()
        await source.aclose()
        await source.env.cleanup()

class _OwnedShaper:
    def __init__(self, provider):
        self.provider = provider
        self.calls = []

    def shape(self, messages):
        self.calls.append(len(messages))
        return messages

async def test_injected_shaper_reuses_the_model_and_rebinds_session_owners(tmp_path):
    class Model(AccountingModel):
        def __deepcopy__(self, memo):
            raise AssertionError("the live model client must retain its owner")

    provider = Model()
    workspace = tmp_path / "source"
    workspace.mkdir()
    shaper = _OwnedShaper(provider)
    source = build_session(
        agent=Agent(name="owner", system_prompt="Help the user."),
        env=_ForkableEnvironment(str(workspace)), llm=provider, shaper=shaper,
    )
    shaper.runner = source.runner
    shaper.events = source.event_bus
    shaper.session = source
    snapshot = None
    try:
        await source.add_user_message("First request")
        assert await source.run_loop() == "answer"
        snapshot = snapshot_session(source)
        branch_shaper = snapshot.runner.shaper
        assert branch_shaper.provider is provider
        assert branch_shaper.runner is snapshot.runner
        assert branch_shaper.events is snapshot.event_bus
        assert branch_shaper.session is snapshot
        assert branch_shaper.calls == shaper.calls
        assert branch_shaper.calls is not shaper.calls
        await snapshot.add_user_message("Branch request")
        assert await snapshot.run_loop() == "answer"
        assert len(branch_shaper.calls) == 2
        assert len(shaper.calls) == 1
        assert source.used_tokens == 10
        assert snapshot.used_tokens == 20
    finally:
        if snapshot is not None:
            await snapshot.aclose()
            await snapshot.env.cleanup()
        await source.aclose()
        await source.env.cleanup()

@pytest.mark.parametrize("copy_failure", ["exception", "same_object"])
async def test_snapshot_reports_an_uncloneable_injected_shaper(tmp_path, copy_failure):
    class Shaper(_OwnedShaper):
        def __deepcopy__(self, memo):
            if copy_failure == "exception":
                raise TypeError("this context owner cannot be copied")
            return self

    provider = AccountingModel()
    workspace = tmp_path / "source"
    workspace.mkdir()
    source = build_session(
        agent=Agent(name="owner", system_prompt="Help the user."),
        env=_ForkableEnvironment(str(workspace)), llm=provider, shaper=Shaper(provider),
    )
    try:
        with pytest.raises(SnapshotSessionError, match="context shaper"):
            snapshot_session(source)
        await source.add_user_message("Continue the source")
        assert await source.run_loop() == "answer"
    finally:
        await source.aclose()
        await source.env.cleanup()
