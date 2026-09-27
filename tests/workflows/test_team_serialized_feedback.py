"""Serialized team feedback distinguishes waiting turns from running turns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencollab.adapters.tools.message import _DELIVERY_NOTE, MessageAgentTool, TeamStatusTool
from opencollab.adapters.tools.submit import SubmitTool
from opencollab.domain.session import SessionPhase
from tests.support.scheduler_awaiting_test_support import ScriptedSession, build_scheduler, terminal


@pytest.mark.parametrize("serialized", [True, False])
async def test_status_reports_only_drivers_waiting_for_serialized_turn(serialized):
    observed = []

    async def lead_turn(session):
        await session.scheduler.spawn(0, "coder", "please work")
        for _ in range(3):
            await asyncio.sleep(0)
        observed.extend(session.scheduler.team_status_rows())
        output = await TeamStatusTool(session.scheduler).execute_with_runtime({}, runtime=None)
        if serialized:
            assert "coder (child of 0) — queued" in output
        session.state.set_phase(SessionPhase.DONE)
        return "lead answer"

    lead = ScriptedSession("lead", [lead_turn])
    coder = ScriptedSession("coder", [terminal("coder answer")])
    scheduler, _ = build_scheduler(lead, [coder], serialize_turns=serialized)

    await scheduler.run("start")

    by_role = {row["role"]: row for row in observed}
    assert by_role["lead"]["turn_queued"] is False
    assert by_role["coder"]["turn_queued"] is serialized
    assert all("turn_queued" not in row for row in scheduler.team_snapshot())


async def test_driver_finishing_after_run_loop_is_not_queued(monkeypatch):
    lead = ScriptedSession("lead", [terminal("answer")])
    scheduler, _ = build_scheduler(lead, [], serialize_turns=True)
    observed = []
    original_deliver = scheduler._deliver_to_parent

    async def observe_finalizer(*args, **kwargs):
        observed.extend(scheduler.team_status_rows())
        await original_deliver(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_deliver_to_parent", observe_finalizer)

    await scheduler.run("start")

    assert observed[0]["busy"] is True
    assert observed[0]["turn_queued"] is False


async def test_cancelled_driver_is_removed_from_waiting_turns():
    lead = ScriptedSession("lead", [])
    scheduler, _ = build_scheduler(lead, [], serialize_turns=True)
    async with scheduler._turn_gate():
        driver = scheduler._start_agent_task(0, lead)
        await asyncio.sleep(0)
        assert scheduler.team_status_rows()[0]["turn_queued"] is True
        driver.cancel()
        with pytest.raises(asyncio.CancelledError):
            await driver
    assert scheduler.team_status_rows()[0]["turn_queued"] is False


@pytest.mark.parametrize("serialized", [True, False, None])
async def test_message_feedback_matches_serialized_mode(serialized):
    class Team:
        def team_snapshot(self):
            return [{"aid": 1, "role": "coder"}]

        async def send_message(self, *args):
            return "Message queued to aid 1."

    team = Team()
    if serialized is not None:
        team.turns_serialized = serialized
    output = await MessageAgentTool(team).execute_with_runtime(
        {"to_role": "coder", "summary": "work", "content": "please work"},
        runtime=SimpleNamespace(aid=0),
    )

    if serialized:
        assert "cannot start until you end your turn" in output
        assert "call submit now" in output.lower()
        assert "reopens your turn" in output
    else:
        assert output == f"Message queued to aid 1. {_DELIVERY_NOTE}"


def test_scheduler_exposes_the_existing_serialized_mode():
    assert build_scheduler(ScriptedSession("lead", []), [], serialize_turns=True)[0].turns_serialized is True
    assert build_scheduler(ScriptedSession("lead", []), [], serialize_turns=False)[0].turns_serialized is False


def test_submit_description_explains_waiting_for_a_reply():
    assert "finished" in SubmitTool.description
    assert "waiting for a teammate" in SubmitTool.description
    assert "reopens your turn" in SubmitTool.description
