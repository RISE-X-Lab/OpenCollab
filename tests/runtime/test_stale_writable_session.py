from __future__ import annotations

import asyncio
import errno
import os
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters import safe_files
from opencollab.application.ports import SnapshotSequenceConflict
from opencollab.application.session import Session
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.environments import local_environment
from tests.support.session_run_test_support import FakeLLM, llm_response


async def _settle_autosave(session: Session) -> None:
    while tasks := session.pending_cleanup_tasks:
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("entry", ["new-prompt", "queued-prompt", "enqueue-autosave"])
async def test_stale_writable_session_rejects_turn_until_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    autosave_path = tmp_path / "autosave.json"
    journal_path = tmp_path / "autosave.json.journal"
    autosave = str(autosave_path)
    queued_prompt = entry == "queued-prompt"
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
        saved_bytes = autosave_path.read_bytes(), journal_path.read_bytes()
        stale_messages = deepcopy(resumed.messages)
        stale_turn = resumed.state.checkpoint_user_turn()
        stale_steps = resumed.step_count

        with pytest.raises(SnapshotSequenceConflict):
            if entry == "new-prompt":
                await resumed.add_user_message(follow_up)
            elif queued_prompt:
                await resumed.run_loop()
            else:
                resumed.enqueue_auto_save()

        await _settle_autosave(resumed)
        assert resumed_llm.calls == []
        assert resumed.messages == stale_messages
        assert resumed.state.checkpoint_user_turn() == stale_turn
        assert resumed.used_tokens == 10
        assert resumed.step_count == stale_steps
        assert OpenCollab.read_session_snapshot(autosave) == before
        assert (autosave_path.read_bytes(), journal_path.read_bytes()) == saved_bytes

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


@pytest.mark.parametrize(
    ("failure_stage", "peer_write", "retry_save"),
    [
        ("base_rename", False, True),
        ("base_sync", False, True),
        ("journal_reset", False, True),
        ("base_sync", False, False),
        ("journal_reset", False, False),
        ("base_rename", True, True),
        ("base_sync", True, True),
        ("journal_reset", True, True),
    ],
)
async def test_manual_checkpoint_can_retry_its_own_failed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str, peer_write: bool, retry_save: bool,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    path = tmp_path / "autosave.json"
    journal = tmp_path / "autosave.json.journal"
    exported = tmp_path / "export.json"
    autosave = str(path)
    agent = Agent(name="reviewer", system_prompt="Review the chapter.")
    llm = FakeLLM([
        llm_response(content="First section reviewed.", total_tokens=11),
        llm_response(content="Second section reviewed.", total_tokens=13),
    ])

    async with AsyncExitStack() as cleanup:
        env = local_environment(tmp_path)
        cleanup.push_async_callback(env.cleanup)
        session = build_session(agent=agent, llm=llm, env=env, auto_save_path=autosave)
        cleanup.push_async_callback(session.aclose)
        cleanup.push_async_callback(_settle_autosave, session)
        await session.add_user_message("Review the first section.")
        assert await session.run_loop() == "First section reviewed."
        await session.add_user_message("Review the second section.")
        await _settle_autosave(session)
        if not journal.stat().st_size:
            await session.enqueue_auto_save()
        original = OpenCollab.read_session_snapshot(path)
        sequence = original["_autosave_sequence"]
        original_base = path.read_bytes()
        original_journal = journal.read_bytes()
        assert original_journal

        error = OSError(errno.EIO, f"checkpoint {failure_stage} failed")
        replace_file = os.replace
        sync_directory = safe_files._fsync_directory

        def fail_rename(source, destination):
            failing_path = path if failure_stage == "base_rename" else journal
            if Path(destination) == failing_path:
                raise error
            return replace_file(source, destination)

        def fail_base_sync(directory):
            if directory == path.parent:
                raise error
            return sync_directory(directory)

        with monkeypatch.context() as fault:
            if failure_stage == "base_sync":
                fault.setattr(safe_files, "_fsync_directory", fail_base_sync)
            else:
                fault.setattr(os, "replace", fail_rename)
            with pytest.raises(OSError) as failure:
                session.save(autosave)
        assert failure.value is error

        published = failure_stage != "base_rename"
        expected = {**original, "_autosave_sequence": sequence + int(published)}
        assert OpenCollab.read_session_snapshot(path) == expected
        assert (path.read_bytes() != original_base) == published
        assert journal.read_bytes() == original_journal
        source_bytes = path.read_bytes(), journal.read_bytes()
        session.save(str(exported))
        assert OpenCollab.read_session_snapshot(exported) == {**original, "_autosave_sequence": sequence + 1}
        assert (path.read_bytes(), journal.read_bytes()) == source_bytes

        if peer_write:
            peer = load_session(autosave, agent=agent, llm=FakeLLM([]), env=env, auto_save_path=autosave, aid=42)
            cleanup.push_async_callback(peer.aclose)
            peer.save(autosave)
            peer_snapshot = OpenCollab.read_session_snapshot(path)
            assert peer_snapshot["aid"] == 42
            assert peer_snapshot["_autosave_sequence"] == expected["_autosave_sequence"] + 1
            peer_bytes = path.read_bytes(), journal.read_bytes()
            with pytest.raises(SnapshotSequenceConflict):
                session.save(autosave)
            with pytest.raises(SnapshotSequenceConflict):
                await session.run_loop()
            assert len(llm.calls) == 1
            assert OpenCollab.read_session_snapshot(path) == peer_snapshot
            assert (path.read_bytes(), journal.read_bytes()) == peer_bytes
            return

        if retry_save:
            session.save(autosave)
            saved = OpenCollab.read_session_snapshot(path)
            assert saved == {**original, "_autosave_sequence": saved["_autosave_sequence"]}
            assert saved["_autosave_sequence"] > sequence
            assert journal.read_bytes() == b""
        assert await session.run_loop() == "Second section reviewed."
        assert len(llm.calls) == 2
        final = OpenCollab.read_session_snapshot(path)
        assert final["session_state"]["used_tokens"] == 24
        assert final["session_state"]["pending_external_user_turn"] is None
        assert [message["content"] for message in final["messages"]] == [
            "Review the chapter.",
            "Review the first section.",
            "First section reviewed.",
            "Review the second section.",
            "Second section reviewed.",
        ]


@pytest.mark.parametrize("destination_turns", [1, 2])
async def test_export_over_compacted_autosave_invalidates_loaded_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination_turns: int,
) -> None:
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    target, source = tmp_path / "target.json", tmp_path / "source.json"
    target_journal = target.with_suffix(".json.journal")
    agent = Agent(name="editor", system_prompt="Review the draft.")
    target_llm = FakeLLM(
        [llm_response(content="Draft A reviewed.", total_tokens=11)] * destination_turns
        + [llm_response(content="Current draft summarized.", total_tokens=5)]
    )
    async with AsyncExitStack() as cleanup:
        env = local_environment(tmp_path)
        cleanup.push_async_callback(env.cleanup)
        destination = build_session(agent=agent, llm=target_llm, env=env, auto_save_path=str(target))
        exporting = build_session(
            agent=agent, env=env, auto_save_path=str(source),
            llm=FakeLLM([llm_response(content="Draft B reviewed.", total_tokens=17)]),
        )
        cleanup.push_async_callback(destination.aclose)
        cleanup.push_async_callback(exporting.aclose)
        for index in range(destination_turns):
            await destination.add_user_message(f"Review draft A, section {index + 1}.")
            assert await destination.run_loop() == "Draft A reviewed."
        await exporting.add_user_message("Review draft B.")
        assert await exporting.run_loop() == "Draft B reviewed."

        target_before = OpenCollab.read_session_snapshot(target)
        source_before = OpenCollab.read_session_snapshot(source)
        source_bytes = source.read_bytes()
        assert target_journal.read_bytes() == b""
        assert target_before["_autosave_sequence"] >= source_before["_autosave_sequence"]

        exporting.save(str(target))
        replacement = OpenCollab.read_session_snapshot(target)
        assert replacement["_autosave_sequence"] > target_before["_autosave_sequence"]
        assert replacement["_autosave_export_source_sequence"] == source_before["_autosave_sequence"]
        assert replacement["messages"] == source_before["messages"]
        assert source.read_bytes() == source_bytes
        replacement_bytes = target.read_bytes()

        with pytest.raises(SnapshotSequenceConflict):
            await destination.add_user_message("Summarize the current draft.")
        assert len(target_llm.calls) == destination_turns
        assert target.read_bytes() == replacement_bytes
        assert target_journal.read_bytes() == b""

        destination.restore(str(target))
        assert destination.used_tokens == 17
        await destination.add_user_message("Summarize the current draft.")
        assert await destination.run_loop() == "Current draft summarized."
        assert destination.used_tokens == 22
        assert len(target_llm.calls) == destination_turns + 1
