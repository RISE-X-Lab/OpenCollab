"""Unit tests for the message_agent / team_status tools."""

from __future__ import annotations

import asyncio

from opencollab.adapters.tools.message import MessageAgentTool, TeamStatusTool
from opencollab.application.tool_execution import ToolRuntime


def run(coro):
    return asyncio.run(coro)


class FakeScheduler:
    def __init__(self, snapshot=None):
        self.sent = []
        self._snapshot = snapshot or []

    async def send_message(self, from_aid, to_aid, summary, content):
        self.sent.append((from_aid, to_aid, summary, content))
        return f"Message queued to aid {to_aid}."

    def team_snapshot(self):
        return self._snapshot


def _runtime(aid=0):
    return ToolRuntime(environment=None, safety_policy=None, permission_policy=None, aid=aid)


def test_message_agent_tool_forwards_runtime_aid_and_returns_ack():
    sched = FakeScheduler()
    tool = MessageAgentTool(sched)
    result = run(
        tool.execute_with_runtime(
            {"to_aid": 2, "summary": "quick check", "content": "hi there"},
            _runtime(aid=0),
        )
    )
    assert result.startswith("Message queued to aid 2.")
    assert sched.sent == [(0, 2, "quick check", "hi there")]
    # The ack alone reads like the end of an exchange. A sender that expects an
    # answer on this call and gets a queue receipt cannot tell "still working"
    # from "never arrived", so the reply says both what happens next and that
    # nothing comes back here.
    assert "runs on its next turn" in result
    assert "nothing about its work comes back through this call" in result


def test_message_agent_tool_addresses_a_teammate_by_role():
    """An aid is not knowable from a role card, so requiring one cost a turn.

    Every first message had to be preceded by a team_status call purely to
    translate a name the role card already gave into a number. On a team whose
    roles are declared before the run, the role is the stable name and the aid
    is an accident of this run.
    """
    sched = FakeScheduler(
        snapshot=[
            {"aid": 0, "role": "analyst", "parent_aid": None, "phase": "idle"},
            {"aid": 1, "role": "coder", "parent_aid": None, "phase": "idle"},
        ]
    )
    result = run(
        MessageAgentTool(sched).execute_with_runtime(
            {"to_role": "Coder", "summary": "s", "content": "c"},
            _runtime(aid=0),
        )
    )

    assert sched.sent == [(0, 1, "s", "c")]
    assert result.startswith("Message queued to aid 1.")


def test_message_agent_tool_refuses_an_ambiguous_or_unknown_role():
    """Guessing which of two would be worse than saying so."""
    sched = FakeScheduler(
        snapshot=[
            {"aid": 1, "role": "coder", "parent_aid": None, "phase": "idle"},
            {"aid": 2, "role": "coder", "parent_aid": None, "phase": "idle"},
            {"aid": 3, "role": "tester", "parent_aid": None, "phase": "idle"},
        ]
    )
    tool = MessageAgentTool(sched)

    ambiguous = run(
        tool.execute_with_runtime(
            {"to_role": "coder", "summary": "s", "content": "c"}, _runtime()
        )
    )
    unknown = run(
        tool.execute_with_runtime(
            {"to_role": "reviewer", "summary": "s", "content": "c"}, _runtime()
        )
    )

    assert "aids 1, 2" in ambiguous
    # The recoverable move is named, not just the refusal.
    assert "to_aid" in ambiguous
    assert "coder, tester" in unknown
    assert sched.sent == []


def test_message_agent_tool_requires_exactly_one_address():
    tool = MessageAgentTool(FakeScheduler())
    for params in (
        {"summary": "s", "content": "c"},
        {"to_aid": 1, "to_role": "coder", "summary": "s", "content": "c"},
    ):
        result = run(tool.execute_with_runtime(params, _runtime()))
        assert result == "Error: give exactly one of to_role or to_aid."


def test_team_status_tool_formats_roster():
    sched = FakeScheduler(
        snapshot=[
            {"aid": 0, "role": "lead", "parent_aid": None, "phase": "idle", "busy": False},
            {"aid": 1, "role": "coder", "parent_aid": 0, "phase": "done", "busy": True},
            {"aid": 2, "role": "reviewer", "parent_aid": 0, "phase": "done", "busy": False},
        ]
    )
    out = run(TeamStatusTool(sched).execute_with_runtime({}, _runtime()))
    assert "aid 0: lead (root)" in out
    assert "aid 1: coder (child of 0)" in out
    assert "aid 2: reviewer (child of 0) — idle" in out
    assert "busy" in out
    assert "done" not in out


def test_team_status_tool_handles_empty_team():
    out = run(TeamStatusTool(FakeScheduler()).execute_with_runtime({}, _runtime()))
    assert "No agents" in out
