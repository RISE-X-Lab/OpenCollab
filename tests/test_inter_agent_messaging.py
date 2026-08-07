"""Integration tests for Scheduler topology enforcement + inter-agent messaging."""

from __future__ import annotations

import asyncio
import copy
import json
import xml.etree.ElementTree as ET

import pytest

from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application._scheduler_constants import (
    MAX_TEAMMATE_DELIVERY_BYTES,
    MAX_TEAMMATE_INBOX_BYTES,
    MAX_TEAMMATE_INBOX_MESSAGES,
    MAX_TEAMMATE_MESSAGE_BYTES,
)
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState
from opencollab.domain.team import Topology


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    """Session stand-in: run_loop returns canned results from a queue."""

    def __init__(self, results, role):
        self._results = list(results)
        self.added: list[str] = []
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return self._results.pop(0) if self._results else ""


class PersistingFakeSession(FakeSession):
    def __init__(self, results, role, auto_save_path):
        super().__init__(results, role)
        self.auto_save_path = str(auto_save_path)

    def save(self, path: str) -> None:
        obj = {"messages": self.state.enriched_messages()}
        if self.state.pending_user_messages:
            obj["pending_messages"] = self.state.enriched_pending_user_messages()
        with open(path, "w") as f:
            json.dump(obj, f)


class RespondingFakeSession(FakeSession):
    """Fake that records a complete user/assistant turn in SessionState."""

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)
        self.state.append_message({"role": "user", "content": content})
        self.state.reset_for_user_turn()

    async def run_loop(self) -> str:
        result = self._results.pop(0) if self._results else ""
        self.state.append_message({"role": "assistant", "content": result})
        self.state.mark_done()
        return result


class FakeFactory:
    def __init__(self, teammate):
        self._teammate = teammate

    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        return self._teammate


def _build_scheduler(teammate, topology=None, roles=()):
    captured: list = []

    async def sink(event):
        captured.append(event)

    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        topology=topology,
        roles=roles,
    )
    lead = FakeSession([], role="lead")
    scheduler.register_lead(lead)
    return scheduler, captured


def _scheduler_events(events):
    return [e for e in events if isinstance(e, SchedulerEvent)]


def _assert_teammate_envelope(delivery, *, sender, summary, content):
    root = ET.fromstring(delivery)
    assert root.tag == "teammate-message"
    assert root.attrib["teammate_id"] == sender
    assert root.attrib["summary"] == summary
    assert len(root.attrib["message_id"]) == 32
    assert root.text == f"\n{content}\n"
    return root


def _register_child(scheduler, session, *, aid: int = 1) -> None:
    session.state.aid = aid
    scheduler.table.add(
        SessionControlBlock(
            aid=aid,
            parent_aid=0,
            agent=session.agent,
            state=session.state,
        )
    )
    scheduler._sessions[aid] = session


async def _wait_agent_idle(scheduler, aid: int) -> None:
    for _ in range(5):
        task = scheduler._tasks.get(aid)
        if task is None:
            return
        await task
        if scheduler._tasks.get(aid) is task and not scheduler._message_inbox.get(aid):
            return


def test_run_turn_routes_raw_user_message_to_selected_child():
    teammate = RespondingFakeSession(["direct answer"], role="coder")
    teammate.state.mark_done()
    topology = Topology(edges={"lead": frozenset()})
    scheduler, _ = _build_scheduler(teammate, topology=topology)
    _register_child(scheduler, teammate)

    result = run(scheduler.run_turn(1, "message from the user"))

    assert result == "direct answer"
    assert teammate.added == ["message from the user"]
    assert "<teammate-message" not in teammate.added[0]
    assert scheduler._sessions[0].added == []
    assert scheduler.table.get(1).result == "direct answer"


def test_run_turn_rejects_unknown_or_non_addressable_aid():
    scheduler, _ = _build_scheduler(FakeSession([], role="coder"))

    with pytest.raises(ValueError, match="no agent with aid 7"):
        run(scheduler.run_turn(7, "hello"))
    with pytest.raises(ValueError, match="non-negative integer"):
        run(scheduler.run_turn(-1, "hello"))


def test_run_turn_rejects_busy_target_without_mutating_history():
    teammate = RespondingFakeSession(["unused"], role="coder")
    teammate.state.mark_done()
    scheduler, _ = _build_scheduler(teammate)
    _register_child(scheduler, teammate)

    async def scenario():
        blocker = asyncio.Event()
        task = asyncio.create_task(blocker.wait())
        scheduler._tasks[1] = task
        try:
            with pytest.raises(RuntimeError, match="agent is still running"):
                await scheduler.run_turn(1, "must not append")
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(scenario())

    assert teammate.added == []


def test_agent_step_count_reads_selected_session():
    teammate = FakeSession([], role="coder")
    teammate.state.set_step_count(7)
    scheduler, _ = _build_scheduler(teammate)
    _register_child(scheduler, teammate)

    assert scheduler.agent_step_count(1) == 7


def test_send_message_queues_xml_and_returns_ack():
    teammate = FakeSession(["spawn result", "message result"], role="coder")
    scheduler, events = _build_scheduler(teammate)

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "do the thing")
        ack = await scheduler.send_message(0, aid, "follow up", "check <this> & report")
        await _wait_agent_idle(scheduler, aid)
        return aid, ack

    aid, ack = run(scenario())

    assert ack == f"Message queued to aid {aid}."
    _assert_teammate_envelope(
        teammate.added[-1],
        sender="A0",
        summary="follow up",
        content="check <this> & report",
    )
    assert scheduler.table.get(aid).result == "message result"

    types = [e.type for e in _scheduler_events(events)]
    assert "agent_message_sent" in types
    assert "agent_message_delivered" in types


def test_send_message_to_idle_target_schedules_background_run():
    teammate = FakeSession(["message result"], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate
    async def scenario():
        ack = await scheduler.send_message(0, 1, "hello", "please review")
        await scheduler.wait_until_terminal(1)
        return ack

    ack = run(scenario())

    assert ack == "Message queued to aid 1."
    assert len(teammate.added) == 1
    assert scheduler.table.get(1).result == "message result"


def test_message_events_cannot_strand_a_durable_delivery():
    class FailingPublisher:
        async def emit(self, event):
            if event.type in {"agent_message_sent", "agent_message_delivered"}:
                raise RuntimeError("observer down")

    teammate = FakeSession(["message result"], role="coder")
    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=FailingPublisher(),
    )
    scheduler.register_lead(FakeSession([], role="lead"))
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    async def scenario():
        ack = await scheduler.send_message(0, 1, "hello", "please review")
        await scheduler.wait_until_terminal(1)
        return ack

    assert run(scenario()) == "Message queued to aid 1."
    assert scheduler.table.get(1).result == "message result"
    assert scheduler._message_inbox.get(1) == []
    assert teammate.state.pending_user_messages == []


def test_message_sent_event_can_reenter_same_target_without_locking_up():
    teammate = FakeSession(["first handled", "second handled"], role="coder")
    holder = {}

    class ReentrantPublisher:
        def __init__(self):
            self.reentered = False

        async def emit(self, event):
            if event.type == "agent_message_sent" and not self.reentered:
                self.reentered = True
                await holder["scheduler"].send_message(
                    0, 1, "nested", "second message"
                )

    publisher = ReentrantPublisher()
    scheduler = Scheduler(
        session_factory=FakeFactory(teammate),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=publisher,
    )
    holder["scheduler"] = scheduler
    scheduler.register_lead(FakeSession([], role="lead"))
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    async def scenario():
        await asyncio.wait_for(
            scheduler.send_message(0, 1, "outer", "first message"), timeout=0.5
        )
        await _wait_agent_idle(scheduler, 1)

    run(scenario())

    assert publisher.reentered is True
    assert len(teammate.added) == 2
    assert "first message" in teammate.added[0]
    assert "second message" in teammate.added[1]


def test_send_message_to_busy_target_autosaves_pending_xml(tmp_path):
    path = tmp_path / "agent_1_coder.json"
    teammate = PersistingFakeSession([], role="coder", auto_save_path=path)
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    async def scenario():
        result = await scheduler.send_message(0, 1, "follow up", "check <this> & report")
        await asyncio.gather(*scheduler._fallback_autosavers[1].pending_tasks)
        return result

    result = run(scenario())

    assert result == "Message queued to aid 1."
    assert teammate.added == []
    with open(path) as f:
        saved = json.load(f)
    assert saved["messages"] == []
    root = _assert_teammate_envelope(
        saved["pending_messages"][0]["content"],
        sender="A0",
        summary="follow up",
        content="check <this> & report",
    )
    assert saved["pending_messages"][0]["message_id"] == root.attrib["message_id"]


def test_delivery_final_autosave_commits_message_and_removes_pending_sidecar(tmp_path):
    class AutosavingOnAdd(PersistingFakeSession):
        def __init__(self, path):
            super().__init__(["handled"], role="coder", auto_save_path=path)
            self.snapshots = []

        def save(self, path):
            obj = {"messages": self.state.enriched_messages()}
            if self.state.pending_user_messages:
                obj["pending_messages"] = self.state.enriched_pending_user_messages()
            self.snapshots.append(obj)

        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            self.save(self.auto_save_path)

    teammate = AutosavingOnAdd(tmp_path / "agent.json")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    async def scenario():
        await scheduler.send_message(0, 1, "once", "deliver exactly once")
        driver = scheduler._tasks.get(1)
        if driver is not None:
            await driver
        await asyncio.gather(
            *scheduler._fallback_autosavers[1].pending_tasks,
            return_exceptions=True,
        )

    run(scenario())

    delivery_snapshots = [
        snapshot
        for snapshot in teammate.snapshots
        if any("deliver exactly once" in str(message.get("content")) for message in snapshot["messages"])
    ]
    assert delivery_snapshots
    assert "pending_messages" not in delivery_snapshots[-1]
    assert teammate.state.pending_user_messages == []


def test_shutdown_after_append_dequeues_and_restore_skips_committed_message():
    class ShutdownAfterAppend(RespondingFakeSession):
        def __init__(self):
            super().__init__(["must not run"], role="coder")
            self.scheduler = None
            self.stale_pending: list[dict] = []

        async def add_user_message(self, content):
            await super().add_user_message(content)
            self.stale_pending = copy.deepcopy(self.state.pending_user_messages)
            self.scheduler._shutting_down = True

    teammate = ShutdownAfterAppend()
    scheduler, _ = _build_scheduler(teammate)
    teammate.scheduler = scheduler
    teammate.state.set_phase(SessionPhase.DONE)
    _register_child(scheduler, teammate)

    async def scenario():
        assert "queued" in await scheduler.send_message(0, 1, "once", "do this once")

    run(scenario())

    assert len(teammate.state.messages) == 1
    assert teammate.state.pending_user_messages == []
    assert scheduler._message_inbox.get(1) == []
    assert teammate.stale_pending

    resumed = RespondingFakeSession(["must not run"], role="coder")
    resumed.state.messages = copy.deepcopy(teammate.state.messages)
    resumed.state.pending_user_messages = teammate.stale_pending
    resumed.state.set_phase(SessionPhase.DONE)
    resumed_scheduler, _ = _build_scheduler(resumed)
    _register_child(resumed_scheduler, resumed)

    resumed_scheduler._restore_message_inbox(1, resumed.state)

    assert resumed.state.pending_user_messages == []
    assert resumed_scheduler._message_inbox.get(1) in (None, [])
    assert resumed.added == []


def test_add_user_message_failure_rolls_back_partial_state_and_restores_budget():
    class FailingAdd(FakeSession):
        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            raise RuntimeError("append hook failed")

    teammate = FailingAdd([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate

    allocation_before = scheduler.allocated_tokens

    with pytest.raises(RuntimeError, match="append hook failed"):
        run(scheduler.send_message(0, 1, "retry", "keep durable"))

    assert teammate.state.phase is SessionPhase.DONE
    assert teammate.state.messages == []
    assert len(teammate.state.pending_user_messages) == 1
    assert len(scheduler._message_inbox[1]) == 1
    assert 1 not in scheduler._tasks
    assert scheduler.allocated_tokens == allocation_before
    assert 1 not in scheduler._child_lease


def test_run_lead_add_user_message_failure_rolls_back_turn_and_restores_lease():
    """A-path (lead, aid=0) twin of the message-path rollback net above.

    ``_run_turn_exclusive`` reserves a fresh lead turn lease, checkpoints, then calls
    ``add_user_message``. If that raises, the same checkpoint -> try ->
    except-restore -> finally-pop transaction must leave the lead turn
    byte-identical and the lease released-then-restored — the exact contract
    S2 single-sources into ``_append_user_turn_txn``. The message path (aid!=0)
    is netted above; this locks the lead path so the extraction cannot silently
    drift the lead's preamble contract.
    """

    class FailingAdd(FakeSession):
        async def add_user_message(self, content):
            self.state.append_message({"role": "user", "content": content})
            self.state.reset_for_user_turn()
            raise RuntimeError("lead append hook failed")

    lead = FailingAdd([], role="lead")
    captured: list = []

    async def sink(event):
        captured.append(event)

    scheduler = Scheduler(
        session_factory=FakeFactory(lead),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
    )
    scheduler.register_lead(lead)

    prior_lease = scheduler._lead_lease
    allocation_before = scheduler.allocated_tokens

    with pytest.raises(RuntimeError, match="lead append hook failed"):
        run(scheduler.run("kick off the team"))

    # user-turn append rolled back byte-identical (restore_user_turn)
    assert lead.state.messages == []
    assert lead.state.phase is SessionPhase.IDLE
    # lease released-then-restored: neither leaked nor left None
    assert scheduler._lead_lease == prior_lease
    assert scheduler.allocated_tokens == allocation_before
    # finally-pop cleared the delivery task; no drive task was ever created
    assert 0 not in scheduler._message_delivery_tasks
    assert 0 not in scheduler._tasks


def test_multiple_queued_messages_are_delivered_as_one_timestamped_user_turn():
    teammate = FakeSession(["message result"], role="coder")
    scheduler, events = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=teammate.agent, state=teammate.state)
    )
    scheduler._sessions[1] = teammate
    second_sender = FakeSession([], role="reviewer")
    scheduler.table.add(
        SessionControlBlock(
            aid=2,
            parent_aid=0,
            agent=second_sender.agent,
            state=second_sender.state,
        )
    )
    scheduler._sessions[2] = second_sender

    async def scenario():
        await scheduler.send_message(0, 1, "first", "alpha <one>")
        await scheduler.send_message(2, 1, "second", "beta & two")
        sent_at = [message.sent_at for message in scheduler._message_inbox[1]]
        teammate.state.set_phase(SessionPhase.DONE)
        await scheduler._drain_message_inbox(1)
        await scheduler.wait_until_terminal(1)
        return sent_at

    sent_at = run(scenario())

    assert len(teammate.added) == 1
    delivery = teammate.added[0]
    assert delivery.startswith('<teammate-messages count="2">')
    assert f'teammate_id="A0" summary="first" sent_at="{sent_at[0]}"' in delivery
    assert f'teammate_id="A2" summary="second" sent_at="{sent_at[1]}"' in delivery
    assert "alpha &lt;one&gt;" in delivery
    assert "beta &amp; two" in delivery
    delivered = [
        event for event in _scheduler_events(events) if event.type == "agent_message_delivered"
    ]
    assert len(delivered) == 2


def test_busy_target_applies_count_backpressure_before_persisting_message():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    _register_child(scheduler, teammate)

    async def scenario():
        acknowledgements = [
            await scheduler.send_message(0, 1, f"message-{index}", "small")
            for index in range(MAX_TEAMMATE_INBOX_MESSAGES + 1)
        ]
        return acknowledgements

    acknowledgements = run(scenario())

    assert all("queued" in ack for ack in acknowledgements[:-1])
    assert "backpressure" in acknowledgements[-1]
    assert len(scheduler._message_inbox[1]) == MAX_TEAMMATE_INBOX_MESSAGES
    assert len(teammate.state.pending_user_messages) == MAX_TEAMMATE_INBOX_MESSAGES


def test_busy_target_applies_byte_backpressure_and_rejects_oversized_message():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    _register_child(scheduler, teammate)
    large_body = "x" * (MAX_TEAMMATE_MESSAGE_BYTES - 256)

    async def scenario():
        responses = []
        for index in range(MAX_TEAMMATE_INBOX_MESSAGES):
            response = await scheduler.send_message(0, 1, f"large-{index}", large_body)
            responses.append(response)
            if "backpressure" in response:
                break
        oversized = await scheduler.send_message(
            0,
            1,
            "oversized",
            "x" * MAX_TEAMMATE_MESSAGE_BYTES,
        )
        return responses, oversized

    responses, oversized = run(scenario())

    assert "backpressure" in responses[-1]
    assert "byte limit" in oversized
    assert sum(len(message.xml.encode("utf-8")) for message in scheduler._message_inbox[1]) <= (
        MAX_TEAMMATE_INBOX_BYTES
    )
    assert len(teammate.state.pending_user_messages) == len(scheduler._message_inbox[1])


def test_message_inbox_drains_fifo_batches_under_delivery_byte_limit():
    teammate = FakeSession(["first batch", "second batch"], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    teammate.state.set_phase(SessionPhase.AWAITING_EVENTS)
    _register_child(scheduler, teammate)
    body = "x" * (MAX_TEAMMATE_DELIVERY_BYTES // 2 - 1024)

    async def scenario():
        for summary in ("first", "second", "third"):
            assert "queued" in await scheduler.send_message(0, 1, summary, body)
        teammate.state.set_phase(SessionPhase.DONE)
        await scheduler._drain_message_inbox(1)
        seen: set[asyncio.Task] = set()
        while (task := scheduler._tasks.get(1)) is not None and task not in seen:
            seen.add(task)
            await task

    run(scenario())

    assert len(teammate.added) == 2
    assert all(len(delivery.encode("utf-8")) <= MAX_TEAMMATE_DELIVERY_BYTES for delivery in teammate.added)
    assert "summary=\"first\"" in teammate.added[0]
    assert "summary=\"second\"" in teammate.added[0]
    assert "summary=\"third\"" not in teammate.added[0]
    assert "summary=\"third\"" in teammate.added[1]
    assert teammate.state.pending_user_messages == []
    assert scheduler._message_inbox.get(1) == []


def test_send_message_to_self_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 0, "hi", "there"))
    assert "itself" in result


def test_send_message_to_unknown_aid_is_rejected():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)
    result = run(scheduler.send_message(0, 99, "hi", "there"))
    assert "no agent with aid 99" in result


def test_send_message_from_unknown_aid_is_rejected_before_mutation():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)

    result = run(scheduler.send_message(99, 0, "hi", "there"))

    assert "no sending agent with aid 99" in result
    assert scheduler._message_inbox.get(0) in (None, [])


def test_spawn_from_unknown_parent_is_rejected_even_without_topology_rules():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(teammate)

    with pytest.raises(ValueError, match="no parent with aid 99"):
        run(scheduler.spawn(99, "coder", "task"))

    assert scheduler.table.get(1) is None


def test_spawn_denied_by_topology_raises_permission_error():
    teammate = FakeSession([], role="coder")
    # lead may only spawn coder, not reviewer.
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)
    with pytest.raises(PermissionError, match="not permitted to spawn 'reviewer'"):
        run(scheduler.spawn(0, "reviewer", "review it"))


def test_send_message_denied_by_topology_returns_error():
    teammate = FakeSession([], role="coder")
    topo = Topology(edges={"lead": frozenset({"coder"})})
    scheduler, _ = _build_scheduler(teammate, topology=topo)

    # Manually register a reviewer the lead is not allowed to message.
    reviewer = FakeSession([], role="reviewer")
    reviewer.state.set_phase(SessionPhase.DONE)
    scheduler.table.add(
        SessionControlBlock(aid=2, parent_aid=0, agent=reviewer.agent, state=reviewer.state)
    )
    scheduler._sessions[2] = reviewer

    result = run(scheduler.send_message(0, 2, "hello", "there"))
    assert "not permitted to message 'reviewer'" in result
    assert reviewer.added == []  # never delivered


def test_team_snapshot_lists_lead_and_spawned():
    teammate = FakeSession(["done"], role="coder")
    scheduler, _ = _build_scheduler(teammate)

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await scheduler._tasks[aid]
        return scheduler.team_snapshot()

    snapshot = run(scenario())
    by_aid = {e["aid"]: e for e in snapshot}
    assert by_aid[0]["role"] == "lead"
    assert by_aid[0]["parent_aid"] is None
    assert by_aid[1]["role"] == "coder"
    assert by_aid[1]["parent_aid"] == 0
    assert by_aid[1]["busy"] is False


def test_team_roster_surfaces_configured_unspawned_roles():
    teammate = FakeSession([], role="coder")
    scheduler, _ = _build_scheduler(
        teammate, roles=("lead", "analyst", "coder", "reviewer")
    )

    roster = scheduler.team_roster()
    # Lead is live (aid 0); the rest are configured-but-unspawned.
    assert roster[0]["aid"] == 0
    assert roster[0]["role"] == "lead"
    available = [e for e in roster if e["aid"] is None]
    assert [e["role"] for e in available] == ["analyst", "coder", "reviewer"]
    assert all(e["phase"] == "available" and e["busy"] is False for e in available)
    # team_snapshot stays live-only.
    assert [e["aid"] for e in scheduler.team_snapshot()] == [0]


def test_team_roster_drops_role_once_spawned():
    teammate = FakeSession(["done"], role="coder")
    scheduler, _ = _build_scheduler(
        teammate, roles=("lead", "analyst", "coder", "reviewer")
    )

    async def scenario():
        aid = await scheduler.spawn(0, "coder", "task")
        await scheduler._tasks[aid]
        return scheduler.team_roster()

    roster = run(scenario())
    # coder is now live (by aid), so it is not also listed as available.
    available_roles = [e["role"] for e in roster if e["aid"] is None]
    assert "coder" not in available_roles
    assert available_roles == ["analyst", "reviewer"]
    assert any(e["aid"] == 1 and e["role"] == "coder" for e in roster)
