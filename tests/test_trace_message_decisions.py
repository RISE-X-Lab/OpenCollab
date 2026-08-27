"""Every ``send_message`` decision has to leave a record on disk.

``Scheduler.send_message`` is the only way one agent reaches another, and it has
eight rules that can stop a message. Each used to end the same way: a sentence
handed back to the model, and nothing written down. Two of them decide findings
on their own.

``topology_forbidden`` is the direct observation the role-boundary axis is
measured on — a model trying to contact a role its team's topology does not let
it contact. Invisible, that attempt simply never happened.

``inbox_message_limit`` / ``inbox_byte_limit`` drop a collaboration message
because the recipient is backed up. Invisible, the run reads downstream as "this
agent did not communicate", which is the opposite of what took place.

These tests pin ``message_refused`` (one row per rule, carrying an enumerated
``reason``) and ``message_sent`` (one row per queued message, same fields). They
also pin the BEHAVIOUR beside each record — the same error string, the same
queue state — because this change adds observation only, and a test that proved
the row landed but not that the rule still bit would let a semantics change ride
along unnoticed.

They drive the real :class:`~opencollab.adapters.trace.Tracer` and read the JSONL
back off disk, so a field that never reaches the file fails here.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from opencollab.adapters.trace import Tracer
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application._scheduler_constants import (
    MAX_TEAMMATE_INBOX_BYTES,
    MAX_TEAMMATE_INBOX_MESSAGES,
    MAX_TEAMMATE_MESSAGE_BYTES,
    MESSAGE_TRACE_COMMIT_REFS,
    MESSAGE_TRACE_SUMMARY_CHARS,
)
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionState
from opencollab.domain.team import Topology


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    def __init__(self, role: str):
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()
        self.added: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return ""


class FakeFactory:
    def build_spawn_session(self, **kwargs):
        return FakeSession("coder")


def _payloads(path: str, step_type: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record["payload"] for record in records if record["type"] == step_type]


def _scheduler(tracer, *, topology=None):
    """Agent 0 ``lead`` plus one live teammate aid 1 ``coder``."""

    async def sink(event):
        return None

    scheduler = Scheduler(
        session_factory=FakeFactory(),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        tracer=tracer,
        topology=topology,
        roles=("lead", "coder"),
    )
    scheduler.register_lead(FakeSession("lead"))
    child = FakeSession("coder")
    child.state.aid = 1
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=child.agent, state=child.state)
    )
    scheduler._sessions[1] = child
    return scheduler, child


def _hold_target_busy(scheduler, aid: int) -> asyncio.Task:
    """Park a never-finishing driver on ``aid`` so its inbox cannot drain."""
    task = asyncio.get_running_loop().create_future()
    scheduler._tasks[aid] = task
    return task


@pytest.fixture
def tracer(tmp_path):
    tracer = Tracer(run_id="messaging", output_dir=str(tmp_path))
    try:
        yield tracer
    finally:
        tracer.flush()
        tracer.close()


def test_a_message_the_topology_forbids_is_recorded_in_full(tracer):
    """The role-boundary observation: who tried to reach whom, and was stopped."""
    closed = Topology(edges={"lead": frozenset(), "coder": frozenset()})
    scheduler, child = _scheduler(tracer, topology=closed)

    error = run(scheduler.send_message(0, 1, "please review", "the patch is ready"))
    tracer.flush()

    # Enforcement unchanged: same refusal string, and nothing was queued.
    assert error == (
        "Error: role 'lead' is not permitted to message 'coder' "
        "under the team topology."
    )
    assert child.added == []
    assert scheduler._message_inbox.get(1) is None

    payloads = _payloads(tracer.path, "message_refused")
    assert payloads == [
        {
            "reason": "topology_forbidden",
            "from_aid": 0,
            "from_role": "lead",
            "to_aid": 1,
            "to_role": "coder",
            "summary": "please review",
            "summary_chars": 13,
            "content_chars": 18,
            "content_bytes": 18,
            # Nothing in this message is shaped like a git object id, and the
            # count says so rather than the list being merely empty.
            "commit_refs": [],
            "commit_refs_found": 0,
            # No envelope is built on this branch, and a zero would read as an
            # empty message rather than as "not measured here".
            "message_bytes": None,
            "message_id": None,
            "inbox_messages": 0,
            "inbox_bytes": 0,
            "max_message_bytes": MAX_TEAMMATE_MESSAGE_BYTES,
            "max_inbox_messages": MAX_TEAMMATE_INBOX_MESSAGES,
            "max_inbox_bytes": MAX_TEAMMATE_INBOX_BYTES,
        }
    ]
    assert _payloads(tracer.path, "message_sent") == []


def test_every_refusal_branch_records_its_own_reason(tracer):
    """One row per rule, and the rule is named by an enumerated token."""

    async def scenario():
        seen: list[tuple[str, str]] = []

        scheduler, _ = _scheduler(tracer)
        seen.append(("self_message", await scheduler.send_message(0, 0, "s", "c")))

        scheduler, _ = _scheduler(tracer)
        scheduler._shutting_down = True
        seen.append(
            ("scheduler_shutting_down", await scheduler.send_message(0, 1, "s", "c"))
        )

        scheduler, _ = _scheduler(tracer)
        seen.append(("unknown_sender", await scheduler.send_message(99, 1, "s", "c")))

        scheduler, _ = _scheduler(tracer)
        seen.append(("unknown_recipient", await scheduler.send_message(0, 99, "s", "c")))

        closed = Topology(edges={"lead": frozenset(), "coder": frozenset()})
        scheduler, _ = _scheduler(tracer, topology=closed)
        seen.append(("topology_forbidden", await scheduler.send_message(0, 1, "s", "c")))

        scheduler, _ = _scheduler(tracer)
        oversized = "x" * (MAX_TEAMMATE_MESSAGE_BYTES + 1)
        seen.append(
            ("message_too_large", await scheduler.send_message(0, 1, "s", oversized))
        )

        scheduler, _ = _scheduler(tracer)
        _hold_target_busy(scheduler, 1)
        for index in range(MAX_TEAMMATE_INBOX_MESSAGES):
            await scheduler.send_message(0, 1, f"fill {index}", "small")
        seen.append(
            ("inbox_message_limit", await scheduler.send_message(0, 1, "s", "small"))
        )

        scheduler, _ = _scheduler(tracer)
        _hold_target_busy(scheduler, 1)
        bulk = "y" * (MAX_TEAMMATE_MESSAGE_BYTES - 1200)
        while scheduler._inbox_size(scheduler._message_inbox.get(1, [])) < (
            MAX_TEAMMATE_INBOX_BYTES - len(bulk)
        ):
            await scheduler.send_message(0, 1, "bulk", bulk)
        seen.append(("inbox_byte_limit", await scheduler.send_message(0, 1, "s", bulk)))
        return seen

    seen = run(scenario())
    tracer.flush()

    # Enforcement unchanged: every branch still refused.
    assert all(error.startswith("Error: ") for _, error in seen)

    refusals = _payloads(tracer.path, "message_refused")
    assert [payload["reason"] for payload in refusals] == [reason for reason, _ in seen]
    # Two aids and two roles on every row: that pair is what attributes a
    # refusal to a declared topology edge.
    for payload in refusals:
        assert {"from_aid", "from_role", "to_aid", "to_role"} <= set(payload)


def test_a_recipient_that_does_not_exist_has_a_null_role_not_a_question_mark(tracer):
    """``_role_of`` answers ``"?"`` for the model; the record must not store it."""
    scheduler, _ = _scheduler(tracer)

    error = run(scheduler.send_message(0, 99, "s", "c"))
    tracer.flush()

    assert error == "Error: no agent with aid 99."
    payload = _payloads(tracer.path, "message_refused")[0]
    assert payload["to_aid"] == 99
    assert payload["to_role"] is None


def test_backpressure_records_the_queue_the_decision_was_taken_against(tracer):
    """A dropped collaboration message must not read as "never sent one"."""

    async def scenario():
        scheduler, child = _scheduler(tracer)
        _hold_target_busy(scheduler, 1)
        for index in range(MAX_TEAMMATE_INBOX_MESSAGES):
            assert await scheduler.send_message(0, 1, f"fill {index}", "small") == (
                "Message queued to aid 1."
            )
        error = await scheduler.send_message(0, 1, "one too many", "small")
        return scheduler, child, error

    scheduler, child, error = run(scenario())
    tracer.flush()

    # Enforcement unchanged: refused, and the inbox stayed at its ceiling.
    assert error == "Error: teammate inbox for aid 1 is full (backpressure)."
    assert len(scheduler._message_inbox[1]) == MAX_TEAMMATE_INBOX_MESSAGES
    assert child.added == []

    refusals = _payloads(tracer.path, "message_refused")
    assert len(refusals) == 1
    assert refusals[0]["reason"] == "inbox_message_limit"
    assert refusals[0]["summary"] == "one too many"
    assert refusals[0]["inbox_messages"] == MAX_TEAMMATE_INBOX_MESSAGES
    assert refusals[0]["message_bytes"] > 0

    # The denominator: the 32 that did get through are countable in the same
    # file, on the same fields, so "how much traffic did this edge carry" and
    # "how much was refused" are one query.
    accepted = _payloads(tracer.path, "message_sent")
    assert len(accepted) == MAX_TEAMMATE_INBOX_MESSAGES
    assert [payload["inbox_messages"] for payload in accepted] == list(
        range(MAX_TEAMMATE_INBOX_MESSAGES)
    )
    assert all("reason" not in payload for payload in accepted)
    assert {payload["from_role"] for payload in accepted} == {"lead"}
    assert {payload["to_role"] for payload in accepted} == {"coder"}


def test_a_queued_message_is_recorded_with_the_id_it_was_queued_under(tracer):
    """``message_id`` joins the record to the durable sidecar entry."""

    async def scenario():
        scheduler, child = _scheduler(tracer)
        _hold_target_busy(scheduler, 1)
        ack = await scheduler.send_message(0, 1, "handoff", "sha 4d1f")
        return scheduler, child, ack

    scheduler, child, ack = run(scenario())
    tracer.flush()

    assert ack == "Message queued to aid 1."
    queued = child.state.pending_user_messages[-1]
    payloads = _payloads(tracer.path, "message_sent")
    assert len(payloads) == 1
    assert payloads[0]["message_id"] == queued["message_id"]
    assert payloads[0]["content_chars"] == 8
    assert payloads[0]["summary"] == "handoff"


def test_a_long_summary_is_capped_but_still_measures_as_long(tracer):
    scheduler, _ = _scheduler(tracer)
    summary = "s" * (MESSAGE_TRACE_SUMMARY_CHARS + 500)

    run(scheduler.send_message(0, 0, summary, "c"))
    tracer.flush()

    payload = _payloads(tracer.path, "message_refused")[0]
    assert len(payload["summary"]) == MESSAGE_TRACE_SUMMARY_CHARS
    assert payload["summary_chars"] == MESSAGE_TRACE_SUMMARY_CHARS + 500


def test_a_tracer_that_fails_does_not_change_any_decision(tmp_path):
    """Observation is non-authoritative: a broken recorder must not rewrite a rule."""

    class BrokenTracer:
        path = str(tmp_path / "unused.jsonl")

        def log_step(self, **kwargs):
            raise RuntimeError("tracer is down")

    closed = Topology(edges={"lead": frozenset(), "coder": frozenset()})
    scheduler, child = _scheduler(BrokenTracer(), topology=closed)

    refused = run(scheduler.send_message(0, 1, "s", "c"))
    assert refused == (
        "Error: role 'lead' is not permitted to message 'coder' "
        "under the team topology."
    )

    scheduler, child = _scheduler(BrokenTracer())

    async def scenario():
        _hold_target_busy(scheduler, 1)
        return await scheduler.send_message(0, 1, "s", "c")

    assert run(scenario()) == "Message queued to aid 1."
    assert len(scheduler._message_inbox[1]) == 1


def test_no_tracer_means_no_records_and_no_failure(tmp_path):
    scheduler, _ = _scheduler(None)
    assert run(scheduler.send_message(0, 0, "s", "c")) == (
        "Error: an agent cannot message itself."
    )


def test_the_git_object_ids_a_message_carries_are_listed(tracer):
    """The payload of a git handoff is a sha, and a size cannot stand in for it.

    A team that hands work over commits and sends the id. Two worktrees of one
    repository can already see each other's commits (``git worktree list`` names
    them), so a tester standing on its teammate's commit is not by itself
    evidence that the teammate sent it there. What settles that is whether the
    id crossed the edge, which is a fact about this message.
    """
    scheduler, _ = _scheduler(tracer)
    sha = "8f14e45fceea167a5a36dedd4bea2543b7dbabcd"  # pragma: allowlist secret

    async def scenario():
        _hold_target_busy(scheduler, 1)
        return await scheduler.send_message(
            0,
            1,
            "handoff",
            f"Fixed it and committed as {sha}. Short form is {sha[:9]}.",
        )

    assert run(scenario()) == "Message queued to aid 1."
    tracer.flush()

    payload = _payloads(tracer.path, "message_sent")[0]
    # Both writings of the same commit, deduplicated and in the order written.
    assert payload["commit_refs"] == [sha, sha[:9]]
    assert payload["commit_refs_found"] == 2
    # The body itself is not copied here: it is already in the recipient's
    # transcript, and a trajectory file is for counting.
    assert "content" not in payload


def test_a_refused_handoff_records_the_id_it_was_carrying(tracer):
    """A refusal is the observation; dropping its payload would empty it."""
    closed = Topology(edges={"lead": frozenset(), "coder": frozenset()})
    scheduler, _ = _scheduler(tracer, topology=closed)
    sha = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret

    run(scheduler.send_message(0, 1, "handoff", f"check out {sha}"))
    tracer.flush()

    payload = _payloads(tracer.path, "message_refused")[0]
    assert payload["reason"] == "topology_forbidden"
    assert payload["commit_refs"] == [sha]


def test_hex_that_is_not_object_shaped_is_not_listed(tracer):
    """A candidate list is only useful if it does not accept everything."""
    scheduler, _ = _scheduler(tracer)
    content = (
        "colour #ff00cc, six chars abc123, sha256 "
        + "a" * 64
        + ", capitalised DEADBEEF12, glued xdeadbeef1x"
    )

    run(scheduler.send_message(0, 0, "s", content))
    tracer.flush()

    payload = _payloads(tracer.path, "message_refused")[0]
    assert payload["commit_refs"] == []
    assert payload["commit_refs_found"] == 0


def test_more_object_ids_than_are_listed_still_measure_as_more(tracer):
    scheduler, _ = _scheduler(tracer)
    refs = [f"{index:040x}" for index in range(1, MESSAGE_TRACE_COMMIT_REFS + 5)]

    run(scheduler.send_message(0, 0, "s", " ".join(refs)))
    tracer.flush()

    payload = _payloads(tracer.path, "message_refused")[0]
    assert payload["commit_refs"] == refs[:MESSAGE_TRACE_COMMIT_REFS]
    assert payload["commit_refs_found"] == len(refs)
