"""Interrupted mixed batches retain completed output before pending-row publication."""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.application.tool_execution import DeferredCall
from opencollab.bootstrap import build_session, load_session, snapshot_session
from opencollab.domain.agent import Agent
from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import FakeLLMClient, llm_response


class _ForkableEnvironment(LocalEnvironment):
    def fork_snapshot(self):
        directory = tempfile.mkdtemp(prefix="snapshot-", dir=Path(self.workspace).parent)
        shutil.copytree(self.workspace, directory, dirs_exist_ok=True)
        return _ForkableEnvironment(directory)


@pytest.mark.parametrize("recovery", ["snapshot", "restore"])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("autosave", [False, True])
@pytest.mark.parametrize("pause_at", ["tool_end", "tool_start"])
async def test_recovery_preserves_completed_output_and_interrupts_remaining_calls(
    tmp_path, recovery, deferred, autosave, pause_at,
):
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "input.txt").write_text("one-time-value\n")
    held, release, release_child = asyncio.Event(), asyncio.Event(), asyncio.Event()
    producers = []

    class DelegatedTool(Tool):
        name = "spawn_agent"
        description = "Run delegated analysis asynchronously."
        parameters = {"type": "object", "properties": {}}

        async def execute_with_runtime(self, args, runtime):
            async def produce():
                await release_child.wait()
                source.state.pending_events.fill(runtime.tool_call_id, result="child completed")

            producers.append(asyncio.create_task(produce()))
            return DeferredCall(ref=7)

    def call(name, arguments, ident):
        return {
            "id": ident, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def shell(code):
        return {"command": shlex.join((sys.executable, "-B", "-c", code))}

    class Model:
        async def complete(self, messages, **kwargs):
            results = {row["tool_call_id"]: row["content"] for row in messages if row["role"] == "tool"}
            if not results:
                consume = (
                    "from pathlib import Path\n"
                    "p=Path('input.txt')\nvalue=p.read_text()\np.unlink()\n"
                    "with Path('executions.txt').open('a') as stream: stream.write('once\\n')\n"
                    "print('CONSUMED_'+value)\n"
                )
                calls = [call("spawn_agent", {}, "child")] if deferred else []
                calls.extend([
                    call("bash", shell(consume), "consume"),
                    call("bash", shell("from pathlib import Path\nPath('late.txt').touch()\n"), "remaining"),
                ])
                return llm_response(tool_calls=calls, finish_reason="tool_calls")
            if "delivery" not in results:
                assert "CONSUMED_one-time-value" in results["consume"]
                delivery = (
                    "from pathlib import Path\n"
                    "assert not Path('input.txt').exists()\n"
                    "assert Path('executions.txt').read_text()=='once\\n'\n"
                    f"Path('delivery.txt').write_text({results['consume']!r})\n"
                    "print('DELIVERY_EXECUTED')\n"
                )
                return llm_response(tool_calls=[call("bash", shell(delivery), "delivery")], finish_reason="tool_calls")
            assert "DELIVERY_EXECUTED" in results["delivery"]
            return llm_response(content="Delivered the consumed value.")

    async def pause(event):
        target = "consume" if pause_at == "tool_end" else "remaining"
        if event.type == pause_at and event.data.get("tool_call_id") == target:
            held.set()
            await release.wait()

    agent = Agent(name="worker", system_prompt="Return the consumed output.",
                  tools=[BashTool(), *([DelegatedTool()] if deferred else [])])
    model = Model()
    source = build_session(
        agent=agent, env=_ForkableEnvironment(str(workspace)), llm=model, event_sink=pause,
        auto_save_path=str(tmp_path / "live.json") if autosave else None,
    )
    await source.add_user_message("Consume the input, run the remaining command, and deliver the value.")
    running = asyncio.create_task(source.run_loop())
    recovered = None
    try:
        await asyncio.wait_for(held.wait(), 5)
        assert source.phase is SessionPhase.EXECUTING_TOOLS
        if recovery == "snapshot":
            recovered = snapshot_session(source)
        else:
            saved = tmp_path / "snapshot.json"
            source.save(str(saved))
            restored_workspace = tmp_path / "restored"
            shutil.copytree(workspace, restored_workspace)
            recovered = load_session(
                str(saved), agent=agent, env=LocalEnvironment(str(restored_workspace)),
                llm=model, auto_save_path=str(tmp_path / "restored-autosave.json") if deferred else None,
            )
            if deferred:
                recovered_messages = recovered.state.enriched_messages()
                recovered.restore(str(saved))
                assert recovered.state.enriched_messages() == recovered_messages

        tool_messages = [row for row in recovered.messages if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in tool_messages] == [
            *(["child"] if deferred else []), "consume", "remaining",
        ]
        results = {row["tool_call_id"]: row["content"] for row in tool_messages}
        assert "CONSUMED_one-time-value" in results["consume"]
        assert "interrupted" in results["remaining"] or "missing" in results["remaining"]
        if deferred:
            assert "interrupted" in results["child"]
        assert recovered._open_tool_call_ids() == []
        assert recovered.state.pending_events.is_empty()
        assert await recovered.run_loop() == "Delivered the consumed value."
        directory = Path(recovered.env.workspace)
        assert "CONSUMED_one-time-value" in (directory / "delivery.txt").read_text()
        assert (directory / "executions.txt").read_text() == "once\n"
        assert not (directory / "input.txt").exists()
        assert not (directory / "late.txt").exists()

        release_child.set()
        await asyncio.gather(*producers)
        release.set()
        assert await asyncio.wait_for(running, 5) == "Delivered the consumed value."
        assert (workspace / "executions.txt").read_text() == "once\n"
        assert (workspace / "late.txt").exists()
        assert "CONSUMED_one-time-value" in (workspace / "delivery.txt").read_text()
    finally:
        release_child.set()
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, *producers, return_exceptions=True)
        for session in (recovered, source):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()


@pytest.mark.parametrize("recovery", ["snapshot", "restore"])
@pytest.mark.parametrize("status", [RowStatus.PENDING, RowStatus.DONE])
async def test_recovery_keeps_registered_deferred_rows_ahead_of_duplicate_receipts(tmp_path, recovery, status):
    workspace = tmp_path / "source"
    workspace.mkdir()
    agent = Agent(name="worker", system_prompt="Recover the saved tool exchange.")
    source = build_session(
        agent=agent, env=_ForkableEnvironment(str(workspace)),
        llm=FakeLLMClient([llm_response(content="Recovered the saved exchange.")]),
    )
    source.state.append_message({"role": "user", "content": "Complete both operations."})
    source.state.append_message({
        "role": "assistant", "content": None, "tool_calls": [
            {"id": ident, "type": "function", "function": {"name": name, "arguments": "{}"}}
            for ident, name in (("child", "spawn_agent"), ("consume", "bash"))
        ],
    })
    source.state.pending_events.add(PendingRow(
        tool_call_id="child", kind=RowKind.CHILD_AGENT, order=0, ref=7, status=status,
        result="registered child result" if status is RowStatus.DONE else None,
    ))
    source.state.set_phase(SessionPhase.EXECUTING_TOOLS)
    source.state.set_step_count(1)
    source._loop_checkpoint_step = 1
    source._loop_checkpoint_results = [
        {"role": "tool", "tool_call_id": "child", "content": "conflicting checkpoint receipt"},
        {"role": "tool", "tool_call_id": "consume", "content": "completed immediate result"},
    ]
    recovered = None
    try:
        if recovery == "snapshot":
            recovered = snapshot_session(source)
        else:
            path = tmp_path / "snapshot.json"
            source.save(str(path))
            recovered = load_session(str(path), agent=agent, env=LocalEnvironment(str(workspace)), llm=source._llm)

        messages = [row for row in recovered.messages if row["role"] == "tool"]
        assert [row["tool_call_id"] for row in messages] == ["child", "consume"]
        expected_child = (
            "registered child result" if status is RowStatus.DONE else
            "deferred child interrupted by session restore"
        )
        assert [row["content"] for row in messages] == [expected_child, "completed immediate result"]
        assert recovered._open_tool_call_ids() == []
        assert recovered.state.pending_events.is_empty()
        assert source.state.pending_events.rows["child"].status is status
        assert await recovered.run_loop() == "Recovered the saved exchange."
    finally:
        for session in (recovered, source):
            if session is not None:
                await session.aclose()
                await session.env.cleanup()
