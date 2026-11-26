from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.application.ports import SnapshotSequenceConflict
from opencollab.application.session import Session
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.environments import local_environment
from tests.support.session_run_test_support import FakeLLM, llm_response


async def _settle_autosave(session: Session) -> None:
    while tasks := session.pending_cleanup_tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("queued_prompt", [False, True], ids=["new-prompt", "queued-prompt"])
async def test_stale_writable_session_rejects_turn_until_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued_prompt: bool,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    autosave = str(tmp_path / "autosave.json")
    initial_prompt = "Review the first section."
    second_prompt = "Review the second section."
    follow_up = "Summarize the complete review."
    original_llm = FakeLLM([
        llm_response(content="First section reviewed.", total_tokens=10),
        llm_response(content="Second section reviewed.", total_tokens=20),
    ])
    resumed_llm = FakeLLM([
        llm_response(content="Review summarized.", total_tokens=5),
    ])

    async with AsyncExitStack() as cleanup:
        original_env = local_environment(tmp_path)
        cleanup.push_async_callback(original_env.cleanup)
        original = build_session(
            agent=Agent(name="reviewer", system_prompt="Review the report."),
            llm=original_llm,
            env=original_env,
            auto_save_path=autosave,
        )
        cleanup.push_async_callback(original.aclose)
        cleanup.push_async_callback(_settle_autosave, original)
        await original.add_user_message(initial_prompt)
        assert await original.run_loop() == "First section reviewed."

        if queued_prompt:
            await original.add_user_message(second_prompt)
            await _settle_autosave(original)
            snapshot = OpenCollab.read_session_snapshot(autosave)
            assert snapshot["session_state"]["pending_external_user_turn"]["content"] == second_prompt

        resumed_env = local_environment(tmp_path)
        cleanup.push_async_callback(resumed_env.cleanup)
        resumed = load_session(
            autosave,
            agent=Agent(name="reviewer", system_prompt="Review the report."),
            llm=resumed_llm,
            env=resumed_env,
            auto_save_path=autosave,
        )
        cleanup.push_async_callback(resumed.aclose)
        cleanup.push_async_callback(_settle_autosave, resumed)
        assert resumed.used_tokens == 10
        if queued_prompt:
            assert resumed.state.pending_external_user_turn["content"] == second_prompt

        if not queued_prompt:
            await original.add_user_message(second_prompt)
        assert await original.run_loop() == "Second section reviewed."
        before = OpenCollab.read_session_snapshot(autosave)
        assert before["session_state"]["used_tokens"] == 30
        stale_messages = deepcopy(resumed.messages)
        stale_turn = resumed.state.checkpoint_user_turn()
        stale_steps = resumed.step_count

        with pytest.raises(SnapshotSequenceConflict):
            if queued_prompt:
                await resumed.run_loop()
            else:
                await resumed.add_user_message(follow_up)

        assert resumed_llm.calls == []
        assert resumed.messages == stale_messages
        assert resumed.state.checkpoint_user_turn() == stale_turn
        assert resumed.used_tokens == 10
        assert resumed.step_count == stale_steps
        assert OpenCollab.read_session_snapshot(autosave) == before

        resumed.restore(autosave)
        assert resumed.used_tokens == 30
        await resumed.add_user_message(follow_up)
        assert await resumed.run_loop() == "Review summarized."
        assert len(resumed_llm.calls) == 1
        after = OpenCollab.read_session_snapshot(autosave)
        assert after["session_state"]["used_tokens"] == 35
        assert [message["content"] for message in after["messages"]] == [
            "Review the report.",
            initial_prompt,
            "First section reviewed.",
            second_prompt,
            "Second section reviewed.",
            follow_up,
            "Review summarized.",
        ]
