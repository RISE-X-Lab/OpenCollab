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
    assert result == "Message queued to aid 2."
    assert sched.sent == [(0, 2, "quick check", "hi there")]


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
