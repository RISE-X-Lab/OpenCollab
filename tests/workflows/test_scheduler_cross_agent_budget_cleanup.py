"""Accepted messages share progress with another agent's deferred budget owner."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler, SchedulerTurnError
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport


class _Factory:
    def __init__(self, client, timeout):
        self.client = client
        self.timeout = timeout

    def build_spawn_session(self, *, role, budget, aid, task, **kwargs):
        return build_session(
            agent=Agent(name=role, system_prompt=role), llm=self.client,
            env=kwargs["env"], aid=aid, max_budget_tokens=budget, llm_timeout=self.timeout,
            seed_user_messages=[{"role": "user", "content": task}],
        )


@pytest.mark.parametrize("owner_aid", [0, 2])
@pytest.mark.parametrize("ending", ["success", "cancelled", "late_success", "late_error"])
async def test_accepted_inbox_waits_for_another_agents_budget_owner(
    monkeypatch, tmp_path, owner_aid, ending,
):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    waiting_for_budget_owner = asyncio.Event()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        latest_user = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        if latest_user == "owner operation":
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                if ending == "cancelled":
                    raise
                await release.wait()
                if ending == "late_error":
                    raise RuntimeError("late provider failure")
            return completion_http_response(request, text="owner answer")
        if latest_user == "lead warmup":
            return completion_http_response(request, output_tokens=19_998, text="lead ready")
        if latest_user == "owner warmup":
            return completion_http_response(request, output_tokens=9_998, text="owner ready")
        return completion_http_response(request, text="recipient processed")

    install_sdk_transport(monkeypatch, "openai", handler)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", "/dev/null")
    async with LLMClient(
        model="gpt-4o", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        total = 400_000 if owner_aid == 0 else 40_000
        timeout = 1.0 if ending == "success" else 0.05
        scheduler = Scheduler(
            session_factory=_Factory(client, 1.0),
            worktree_pool=WorktreePool(str(tmp_path), use_worktrees=False),
            event_sink=EventBus(), max_budget_tokens=total,
        )
        lead = build_session(
            agent=Agent(name="lead", system_prompt="lead"), llm=client,
            max_budget_tokens=total, llm_timeout=timeout,
        )
        scheduler.register_lead(lead)
        if owner_aid:
            assert await scheduler.run("lead warmup") == "lead ready"
        recipient = await scheduler.spawn(0, "recipient", "recipient warmup")
        await scheduler._tasks[recipient]
        recipient_session = scheduler._sessions[recipient]
        assert recipient_session.phase is SessionPhase.DONE, recipient_session.state.terminal_reason
        if owner_aid:
            assert await scheduler.spawn(0, "owner", "owner warmup") == owner_aid
            await scheduler._tasks[owner_aid]
        owner = scheduler._sessions[owner_aid]
        owner.runner._per_call_timeout = timeout
        original_active = scheduler._active_scheduler_tasks

        def observe_progress():
            pending = original_active()
            if (
                scheduler._pending_provider_budget_leases
                and not any(not task.done() for task in scheduler._tasks.values())
                and any(task in pending for task in owner.runner.pending_cleanup_tasks)
            ):
                waiting_for_budget_owner.set()
            return pending

        monkeypatch.setattr(scheduler, "_active_scheduler_tasks", observe_progress)
        active = asyncio.create_task(scheduler.run_turn(owner_aid, "owner operation"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert scheduler.allocated_tokens == total
            assert await scheduler.send_message(0, recipient, "work", "accepted follow-up") == (
                "Message queued to aid 1."
            )
            assert len(scheduler._message_inbox[recipient]) == 1
            if ending == "success":
                release.set()
                assert await asyncio.wait_for(active, 2) == "owner answer"
            else:
                await asyncio.wait_for(cancelled.wait(), 2)
                if ending != "cancelled":
                    await asyncio.wait_for(waiting_for_budget_owner.wait(), 2)
                    assert not active.done()
                    assert scheduler.allocated_tokens == total
                    assert len(scheduler._message_inbox[recipient]) == 1
                release.set()
                with pytest.raises(SchedulerTurnError) as failure:
                    await asyncio.wait_for(active, 2)
                assert failure.value.aid == owner_aid
                assert failure.value.phase is SessionPhase.ERROR
                assert "GenerationTimeoutError" in failure.value.terminal_reason

            recipient_requests = [r for r in requests if r["messages"][0]["content"] == "recipient"]
            assert len(requests) == (3 if owner_aid == 0 else 5)
            assert len(recipient_requests) == 2
            assert "accepted follow-up" in str(recipient_requests[-1])
            assert not scheduler._message_inbox[recipient]
            assert scheduler._sessions[recipient].phase is SessionPhase.DONE
            assert not owner.runner.pending_cleanup_tasks
            assert scheduler._pending_provider_budget_leases == {}
            assert scheduler.allocated_tokens == scheduler.used_tokens
            assert scheduler.used_tokens == (
                (0 if owner_aid == 0 else 30_000)
                + 8 + (4 if ending in {"success", "late_success"} else 0)
            )
            assert owner.runner.late_provider_usage == ((4,) if ending == "late_success" else ())
            if ending.startswith("late"):
                assert "owner answer" not in str(owner.messages)
        finally:
            release.set()
            await scheduler.cleanup()
            await asyncio.gather(active, return_exceptions=True)


async def test_inbox_runs_after_first_of_two_deferred_grants_releases(monkeypatch, tmp_path):
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    both_deferred = asyncio.Event()
    delivered = asyncio.Event()
    recipient_requests = []

    async def handler(request):
        body = json.loads(request.content)
        text = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        if text in {"owner 0 operation", "owner 1 operation"}:
            index = int(text.split()[1])
            started[index].set()
            try:
                await release[index].wait()
            except asyncio.CancelledError:
                await release[index].wait()
        if "accepted follow-up" in text:
            recipient_requests.append(text)
            delivered.set()
        return completion_http_response(request, text="done", output_tokens=19_986 if text == "lead warmup" else 2)

    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    install_sdk_transport(monkeypatch, "openai", handler)
    async with LLMClient(
        model="gpt-4o", provider="openai",
        api_key="local-controlled",  # pragma: allowlist secret
        base_url="https://controlled.invalid/v1", max_retries=0,
    ) as client:
        scheduler = Scheduler(
            session_factory=_Factory(client, 1.0),
            worktree_pool=WorktreePool(str(tmp_path), use_worktrees=False),
            event_sink=EventBus(), max_budget_tokens=40_000,
        )
        lead = build_session(agent=Agent(name="lead", system_prompt="lead"), llm=client, max_budget_tokens=40_000)
        scheduler.register_lead(lead)
        assert await scheduler.run("lead warmup") == "done"
        for aid, role in enumerate(("recipient", "owner-one", "owner-two"), 1):
            assert await scheduler.spawn(0, role, f"{role} warmup") == aid
            await scheduler._tasks[aid]
        assert scheduler.used_tokens == 20_000

        original_active = scheduler._active_scheduler_tasks

        def observe():
            tasks = original_active()
            if len(scheduler._pending_provider_budget_leases) == 2:
                both_deferred.set()
            return tasks

        monkeypatch.setattr(scheduler, "_active_scheduler_tasks", observe)
        for aid in (2, 3):
            scheduler._sessions[aid].runner._per_call_timeout = 0.5
        work = [asyncio.create_task(scheduler.run_turn(aid, f"owner {aid - 2} operation")) for aid in (2, 3)]
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 3)
            assert scheduler.allocated_tokens == 40_000
            assert await scheduler.send_message(0, 1, "work", "accepted follow-up") == "Message queued to aid 1."
            await asyncio.wait_for(both_deferred.wait(), 3)
            release[0].set()
            await asyncio.wait_for(delivered.wait(), 3)

            assert not scheduler._message_inbox[1]
            assert {lease[0] for lease in scheduler._pending_provider_budget_leases.values()} == {3}
            assert not work[1].done()
            assert len(recipient_requests) == 1
            second_owner, = next(iter(scheduler._pending_provider_budget_leases.values()))[3]
            settled = asyncio.Event()
            second_owner.add_done_callback(lambda _: settled.set())
            release[1].set()
            results = await asyncio.wait_for(asyncio.gather(*work, return_exceptions=True), 3)
            assert all(isinstance(error, SchedulerTurnError) for error in results)
            await asyncio.wait_for(settled.wait(), 3)
            assert not scheduler._pending_provider_budget_leases
            assert scheduler.allocated_tokens == scheduler.used_tokens
        finally:
            for event in release:
                event.set()
            await scheduler.cleanup()
            await asyncio.gather(*work, return_exceptions=True)


def _budget_callback_scheduler(tmp_path, owner):
    state = SessionState(messages=[])
    state.set_phase(SessionPhase.DONE)
    lead = SimpleNamespace(
        agent=Agent(name="lead", system_prompt="lead"), state=state,
        runner=SimpleNamespace(pending_cleanup_tasks=(owner,)), used_tokens=0,
    )
    scheduler = Scheduler(
        session_factory=_Factory(None, 1.0),
        worktree_pool=WorktreePool(str(tmp_path), use_worktrees=False),
        event_sink=EventBus(), max_budget_tokens=100_000,
    )
    scheduler.register_lead(lead)
    scheduler._reserve_turn_lease(0)
    recipient_state = SessionState(messages=[])
    recipient_state.set_phase(SessionPhase.DONE)
    scheduler.table.add(SessionControlBlock(
        aid=1, parent_aid=0, agent=Agent(name="recipient", system_prompt="recipient"),
        state=recipient_state,
    ))
    return scheduler, state


async def test_done_provider_callback_releases_budget_before_the_next_inbox_drain(monkeypatch, tmp_path):
    async def completed_owner():
        return None

    owner = asyncio.create_task(completed_owner())
    await owner
    scheduler, state = _budget_callback_scheduler(tmp_path, owner)
    owner.add_done_callback(lambda _task: state.add_used_tokens(17))
    assert scheduler._defer_budget_lease_for_provider_cleanup(0)
    scheduler._message_inbox[1] = [object()]
    observations = []

    async def drain_after_budget_release():
        observations.append(scheduler.allocated_tokens)
        if scheduler.allocated_tokens < scheduler._max_budget_tokens:
            scheduler._message_inbox[1].clear()

    monkeypatch.setattr(scheduler, "_drain_ready_message_inboxes", drain_after_budget_release)
    assert not scheduler._active_scheduler_tasks()
    assert scheduler._pending_provider_budget_leases
    await scheduler._wait_for_scheduler_progress(0)
    assert observations == [100_000, 17]
    assert not scheduler._message_inbox[1]
    assert not scheduler._pending_provider_budget_leases
    assert scheduler.allocated_tokens == scheduler.used_tokens == 17


async def test_deferred_budget_owner_counts_as_progress_only_for_an_unheld_inbox(tmp_path):
    release = asyncio.Event()
    owner = asyncio.create_task(release.wait())
    scheduler, _state = _budget_callback_scheduler(tmp_path, owner)
    assert scheduler._defer_budget_lease_for_provider_cleanup(0)
    try:
        assert not scheduler._active_scheduler_tasks()
        scheduler._message_inbox[1] = [object()]
        scheduler._cancelled_turn_inbox_holds.add(1)
        assert not scheduler._active_scheduler_tasks()
        scheduler._cancelled_turn_inbox_holds.clear()
        assert scheduler._active_scheduler_tasks() == {owner}
    finally:
        release.set()
        await owner
