"""A serialized turn accepted before shutdown survives snapshot restore."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from contextlib import AsyncExitStack

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import SnapshotSequenceConflict
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from tests.support.session_characterization_test_support import fake_worktree_pool, llm_response


class _Provider:
    def __init__(self, *, hold=False):
        self.hold = hold
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs["messages"]))
        self.started.set()
        if self.hold:
            await self.release.wait()
        return llm_response(content=f"answer-{len(self.calls)}")

class _Factory:
    def __init__(self, provider):
        self.provider = provider

    def build_spawn_session(self, *, role, aid, budget, **_kwargs):
        return build_session(
            agent=Agent(name=role, system_prompt=role),
            llm=self.provider,
            aid=aid,
            max_budget_tokens=budget,
        )

async def test_shutdown_queued_turn_restore_claims_it_before_the_next_request(tmp_path):
    path = tmp_path / "lead.json"
    worker_provider = _Provider(hold=True)
    lead_provider = _Provider()
    lead = build_session(
        agent=Agent(name="lead", system_prompt="lead"),
        llm=lead_provider,
        env=LocalEnvironment(str(tmp_path)),
        auto_save_path=str(path),
    )
    scheduler = Scheduler(
        session_factory=_Factory(worker_provider),
        worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(),
        prebuild_team=True,
        serialize_turns=True,
        roles=("lead", "worker"),
    )
    scheduler.register_lead(lead)
    await scheduler.ensure_team_prebuilt()
    worker = asyncio.create_task(scheduler.run_turn(1, "worker request"))
    await asyncio.wait_for(worker_provider.started.wait(), 2)
    queued = asyncio.create_task(scheduler.run("old accepted request"))

    async def wait_for_queue():
        while not any(row["aid"] == 0 and row["turn_queued"] for row in scheduler.team_status_rows()):
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_for_queue(), 2)
        assert lead_provider.calls == []
        await asyncio.wait_for(scheduler.cleanup(), 2)
    finally:
        worker_provider.release.set()
        await scheduler.cleanup()
        await asyncio.gather(worker, queued, return_exceptions=True)

    snapshot = json.loads(path.read_text())
    assert snapshot["session_state"]["phase"] == "stopped"
    assert snapshot["session_state"]["pending_external_user_turn"]["content"] == "old accepted request"

    restored_provider = _Provider()
    restored = load_session(
        str(path),
        agent=Agent(name="lead", system_prompt="lead"),
        llm=restored_provider,
        env=LocalEnvironment(str(tmp_path)),
    )
    resumed = Scheduler(
        session_factory=object(), worktree_pool=fake_worktree_pool(), event_sink=EventBus(),
    )
    resumed.register_lead(restored)
    try:
        assert await asyncio.wait_for(resumed.run("new request"), 2) == "answer-2"
        assert restored.state.pending_external_user_turn is None
        assert len(restored_provider.calls) == 2
        assert any(
            message.get("content", "").startswith("old accepted request")
            for message in restored_provider.calls[0]
        )
        assert not any(message.get("content", "").startswith("new request") for message in restored_provider.calls[0])
        assert [
            message["content"].split("\n\n[Budget:", 1)[0]
            for message in restored.messages if message["role"] == "user"
        ] == [
            "old accepted request", "new request",
        ]
        assert restored.phase is SessionPhase.DONE
        assert await restored.run_loop() == "answer-2"
        assert len(restored_provider.calls) == 2
    finally:
        await resumed.cleanup()


@pytest.mark.parametrize("publication", ["before", "in-flight", "published"])
async def test_cancelled_turn_admission_preserves_restored_export_order(tmp_path, monkeypatch, publication):
    source, exported, working_path, backup, early_backup = (
        str(tmp_path / f"{name}.json") for name in ("source", "export", "working", "backup", "early")
    )
    store = SessionStore()
    options = {"agent": Agent(name="lead", system_prompt="Keep each accepted request."), "store": store}

    async def settle(session):
        while session.pending_cleanup_tasks:
            await asyncio.gather(*session.pending_cleanup_tasks)

    observed = asyncio.Event()

    async def hold_admission(event):
        if event.type == "user_message_appended":
            observed.set()
            await asyncio.Future()

    async with AsyncExitStack() as resources:
        environment = LocalEnvironment(str(tmp_path))
        resources.push_async_callback(environment.cleanup)
        original = build_session(**options, env=environment, llm=_Provider(), auto_save_path=source, aid=0)
        resources.push_async_callback(original.aclose)
        await original.add_user_message("Earlier accepted request.")
        assert await original.run_loop() == "answer-1"
        original.save(exported)

        provider = _Provider()
        working = load_session(
            exported, **options, env=environment, llm=provider, auto_save_path=working_path,
            event_sink=hold_admission if publication == "before" else None,
        )
        resources.push_async_callback(working.aclose)
        if publication != "before":
            working.event_bus.subscribe(hold_admission)
        scheduler = Scheduler(session_factory=object(), worktree_pool=fake_worktree_pool(), event_sink=EventBus())
        scheduler.register_lead(working)
        resources.push_async_callback(scheduler.cleanup)

        await original.add_user_message("Newer request on the source.")
        await settle(original)
        latest_source = OpenCollab.read_session_snapshot(source)

        started, release = threading.Event(), threading.Event()
        append = store.append_snapshot_delta

        def hold_first_working_write(path, **kwargs):
            if path == working_path and not started.is_set():
                started.set()
                assert release.wait(3)
            return append(path, **kwargs)

        if publication == "in-flight":
            monkeypatch.setattr(store, "append_snapshot_delta", hold_first_working_write)

        async def wait_for_rollback():
            while working.state.pending_external_user_turn is not None:
                await asyncio.sleep(0)

        turn = asyncio.create_task(scheduler.run("Cancelled request on the copy."))
        try:
            await asyncio.wait_for(observed.wait(), 3)
            if publication == "in-flight":
                assert await asyncio.to_thread(started.wait, 3)
            elif publication == "published":
                await working.enqueue_auto_save()
                provisional = OpenCollab.read_session_snapshot(working_path)
                assert provisional["session_state"]["pending_external_user_turn"]["content"] == (
                    "Cancelled request on the copy."
                )
            turn.cancel()
            if publication == "in-flight":
                await asyncio.wait_for(wait_for_rollback(), 3)
                working.save(early_backup)
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await turn
        finally:
            release.set()
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

        await settle(working)
        assert working.state.pending_external_user_turn is None
        assert working.messages[-1]["content"] == "answer-1"
        assert provider.calls == []
        working.save(backup)
        copies = (early_backup, backup) if publication == "in-flight" else (backup,)
        for copy_path in copies:
            with pytest.raises(SnapshotSequenceConflict):
                load_session(copy_path, **options, env=environment, llm=_Provider(), auto_save_path=source)
            assert OpenCollab.read_session_snapshot(source) == latest_source


@pytest.mark.parametrize("restore_sender", [False, True])
@pytest.mark.parametrize("completed_driver", [False, True])
async def test_active_restore_rejects_missing_sender_without_blocking_new_turn(
    tmp_path, restore_sender, completed_driver,
):
    path = tmp_path / "active.json"
    provider = _Provider(hold=True)
    lead = build_session(
        agent=Agent(name="lead", system_prompt="lead"), llm=provider,
        env=LocalEnvironment(str(tmp_path)),
    )
    scheduler = Scheduler(
        session_factory=_Factory(_Provider()), worktree_pool=fake_worktree_pool(), event_sink=EventBus(),
    )
    scheduler.register_lead(lead)
    sender = await scheduler.spawn(0, "worker", "prepare sender")
    active = asyncio.create_task(scheduler.run("old request"))
    try:
        await asyncio.wait_for(provider.started.wait(), 2)
        assert "queued" in await scheduler.send_message(sender, 0, "accepted work", "QUEUED_WORK")
        message = copy.deepcopy(lead.state.pending_user_messages[0])
        lead.save(str(path))
        snapshot = json.loads(path.read_text())
        assert snapshot["session_state"]["phase"] == SessionPhase.CALLING_LLM.value
        assert snapshot["pending_messages"] == [message]
    finally:
        await scheduler.cleanup()
        provider.release.set()
        await asyncio.gather(active, return_exceptions=True)

    continuation = _Provider(hold=restore_sender)
    restored = load_session(
        str(path), agent=Agent(name="lead", system_prompt="lead"), llm=continuation,
        env=LocalEnvironment(str(tmp_path)),
    )
    events = []
    resumed = Scheduler(
        session_factory=_Factory(_Provider()), worktree_pool=fake_worktree_pool(),
        event_sink=EventBus(events.append), prebuild_team=restore_sender, roles=("lead", "worker"),
    )
    resumed.register_lead(restored)
    if completed_driver:
        previous = asyncio.create_task(asyncio.sleep(0))
        await previous
        resumed._tasks[0] = previous
    first = asyncio.create_task(resumed.run("new request"))
    expected_calls = 2 if restore_sender else 1
    try:
        if restore_sender:
            await asyncio.wait_for(continuation.started.wait(), 2)
            assert "QUEUED_WORK" in str(continuation.calls[0])
            assert "new request" not in str(continuation.calls[0])
            assert not first.done()
            continuation.release.set()
        assert await asyncio.wait_for(first, 2) == f"answer-{expected_calls}"
        assert len(continuation.calls) == expected_calls
        assert continuation.calls[-1][-1]["content"].startswith("new request")
        assert await resumed.run("second request") == f"answer-{expected_calls + 1}"
        deliveries = [message for message in restored.messages
                      if message["role"] == "user" and "QUEUED_WORK" in message["content"]]
        assert len(deliveries) == int(restore_sender)
        rejections = [event for event in events if event.type == "agent_message_rejected_on_restore"]
        assert len(rejections) == int(not restore_sender)
        if restore_sender:
            assert restored.state.pending_user_messages == []
        else:
            rejected = restored.state.pending_user_messages[0]
            assert rejected["message_id"] == message["message_id"]
            assert rejected["from_aid"] == sender
            assert rejected["from_role"] == "worker"
            assert rejected["delivery_status"] == "rejected"
            assert "no longer exists" in rejected["rejection_reason"]
        assert restored.phase is SessionPhase.DONE
        assert restored.state.pending_external_user_turn is None
        assert not resumed._message_inbox.get(0)
    finally:
        continuation.release.set()
        await resumed.cleanup()
        await asyncio.gather(first, return_exceptions=True)
