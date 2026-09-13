"""One turn at a time, when the run asks for it.

By default the scheduler runs independent agents concurrently: ``_run_locks``
orders repeat turns for one aid and nothing orders turns across the team. Under
``serialize_turns`` a single gate in ``_drive_agent`` holds the team to one turn
at a time.

What the flag does *not* touch is who may talk to whom. The topology keeps every
declared edge and ``message_agent`` stays voluntary — only the timing changes —
so whether the agents hand work to each other is still theirs to decide. The
tests here pin both halves, and the one property that makes the gate safe: a
parent suspended on ``AWAITING_EVENTS`` has already released it, so a child can
run.
"""

from __future__ import annotations

import asyncio

import pytest
from scheduler_awaiting_test_support import (
    ScriptedSession,
    build_scheduler,
    resume_done,
    suspend_spawning,
    terminal,
)

from opencollab.domain.pending import PendingRow, RowKind, RowStatus
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase

# A turn that deadlocks the gate would otherwise hang the suite. Every scenario
# here is in-process and yields only to the event loop, so anything past this is
# a hang rather than a slow machine.
GATE_TIMEOUT_SECONDS = 30.0

# How long one teammate waits to see the other start. Serialized, this always
# expires — the other agent cannot run at all until this turn ends — so the value
# only has to be long enough that a *concurrent* teammate is reliably seen
# starting within it on a loaded machine.
HANDSHAKE_TIMEOUT_SECONDS = 1.0


def _handshake_turn(log, entered, role: str, other: str):
    """A turn that reports whether the other teammate ran beside it.

    Waiting on the other agent, rather than sleeping and hoping the schedules
    interleave, is what makes both directions assertable: concurrently the wait
    returns as soon as the other starts, and serialized it always expires,
    because the other agent cannot start until this turn releases the gate.
    """

    async def step(sess: ScriptedSession) -> str:
        log.append(f"enter:{role}")
        entered[role].set()
        try:
            await asyncio.wait_for(
                entered[other].wait(), HANDSHAKE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            log.append(f"alone:{role}")
        log.append(f"exit:{role}")
        sess.state.set_phase(SessionPhase.DONE)
        sess.state.append_message({"role": "assistant", "content": role})
        return role

    return step


def _overlaps(log: list[str]) -> bool:
    """True when any turn started while another was still running."""
    running = 0
    for entry in log:
        if entry.startswith("enter:"):
            if running:
                return True
            running += 1
        elif entry.startswith("exit:"):
            running -= 1
    return False


def _two_teammate_run(*, serialize_turns: bool) -> list[str]:
    log: list[str] = []
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1"), ("tester", "check", "tc-2")]),
            resume_done(lambda results: "final: " + ", ".join(results)),
        ],
    )

    async def scenario() -> str:
        entered = {"coder": asyncio.Event(), "tester": asyncio.Event()}
        children = [
            ScriptedSession("coder", [_handshake_turn(log, entered, "coder", "tester")]),
            ScriptedSession(
                "tester", [_handshake_turn(log, entered, "tester", "coder")]
            ),
        ]
        scheduler, _ = build_scheduler(lead, children, serialize_turns=serialize_turns)
        return await asyncio.wait_for(
            scheduler.run("please delegate"), GATE_TIMEOUT_SECONDS
        )

    assert asyncio.run(scenario()) == "final: coder, tester"
    return log


def test_turns_overlap_when_serialization_is_off():
    """The default is unchanged: two teammates make progress at the same time.

    Pinned because it is the thing ``serialize_turns`` exists to switch off. It
    is also what makes the shared budget pool oversubscribable, since a lease
    under a declared roster sets a ceiling without taking tokens out of the pool.
    """
    log = _two_teammate_run(serialize_turns=False)

    assert _overlaps(log)
    assert [entry for entry in log if entry.startswith("alone:")] == []


def test_serializing_turns_lets_only_one_agent_run_at_a_time():
    log = _two_teammate_run(serialize_turns=True)

    assert not _overlaps(log)
    # The first teammate waited out the whole handshake without the second one
    # appearing: it was not merely scheduled first, it was alone.
    assert [entry for entry in log if entry.startswith("alone:")] == ["alone:coder"]


def test_a_suspended_parent_does_not_hold_the_gate_against_its_child():
    """The reason the gate cannot deadlock, pinned as behaviour.

    The lead spawns from inside its own turn, so its child's driver is created
    while the lead holds the gate. ``run_loop`` returns when the lead suspends on
    ``AWAITING_EVENTS`` instead of blocking on the child, which releases the gate
    and lets the child run. A gate held across the suspension would hang here.
    """
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [terminal("child output")])
    scheduler, _ = build_scheduler(lead, [child], serialize_turns=True)

    async def scenario() -> str:
        return await asyncio.wait_for(
            scheduler.run("please delegate"), GATE_TIMEOUT_SECONDS
        )

    assert asyncio.run(scenario()) == "final: child output"
    assert lead.state.phase is SessionPhase.DONE


def test_a_queued_message_still_wakes_its_target_under_serialization():
    """Serializing changes when a woken agent runs, never whether it runs."""
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [], serialize_turns=True)

    target = ScriptedSession(
        "coder",
        [
            resume_done(lambda results: f"finished {results[0]}"),
            terminal("message handled"),
        ],
    )
    target.state.aid = 1
    target.scheduler = scheduler
    target.state.set_phase(SessionPhase.AWAITING_EVENTS)
    target.state.pending_events.add(
        PendingRow(tool_call_id="tc-1", kind=RowKind.CHILD_AGENT, order=0, ref=7)
    )
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=target.agent, state=target.state)
    )
    scheduler._sessions[1] = target

    async def scenario():
        await scheduler.send_message(0, 1, "follow up", "are you there?")
        await scheduler._wake(1, "tc-1", "child done", RowStatus.DONE)
        for _ in range(3):
            task = scheduler._tasks.get(1)
            if task is None:
                break
            await task
            if scheduler._tasks.get(1) is task:
                break

    asyncio.run(asyncio.wait_for(scenario(), GATE_TIMEOUT_SECONDS))

    assert len(target.added) == 1
    assert "are you there?\n</teammate-message>" in target.added[0]
    assert scheduler.table.get(1).result == "message handled"


@pytest.mark.parametrize("serialize_turns", [False, True])
def test_the_gate_is_the_only_thing_the_flag_changes(serialize_turns):
    """Same roster, same results, whichever way the flag is set."""
    lead = ScriptedSession(
        "lead",
        [
            suspend_spawning([("coder", "do it", "tc-1")]),
            resume_done(lambda results: f"final: {results[0]}"),
        ],
    )
    child = ScriptedSession("coder", [terminal("child output")])
    scheduler, _ = build_scheduler(lead, [child], serialize_turns=serialize_turns)

    result = asyncio.run(
        asyncio.wait_for(scheduler.run("please delegate"), GATE_TIMEOUT_SECONDS)
    )

    assert result == "final: child output"
    assert [scb.agent.name for scb in scheduler.table.entries.values()] == [
        "lead",
        "coder",
    ]
