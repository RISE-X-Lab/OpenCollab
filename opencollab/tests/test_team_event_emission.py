"""Characterization tests for Team event emission.

Locks the event sequence emitted by Team.delegate and
Team.delegate_with_review. After Step12 the delegation/review lifecycle
flows through TeamEvent rather than synthetic SessionEvent tool_* events.
"""

from __future__ import annotations

import asyncio
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.domain.events import TeamEvent
from opencollab.application.team import Team


def run(coro):
    return asyncio.run(coro)


class _FakeTeammateSession:
    """Minimal session stand-in: records messages and returns a canned result."""

    def __init__(self, result: str, tokens: int = 0):
        self._result = result
        self.used_tokens = tokens
        self.added: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return self._result


class _FakeLeadSession:
    """Stand-in for the Lead session; never run in these tests."""

    def __init__(self):
        self.used_tokens = 0
        self.env = None
        self.tool_execution = type("_TP", (), {"safety_policy": None, "env": None})()
        self.runner = type("_R", (), {"max_steps": 100})()
        self.max_steps = 100

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return ""


class _FakeSessionFactory:
    """Drives delegate()/delegate_with_review() with canned teammate sessions."""

    def __init__(self, role_results: dict[str, list[str]]):
        # Map of role -> queue of canned results.
        self._queues = {role: list(results) for role, results in role_results.items()}
        self.built: list[tuple[str, int]] = []

    def build_lead_session(self, **kwargs):
        return _FakeLeadSession()

    def build_teammate_session(self, *, role, env, budget, max_steps=50):
        self.built.append((role, budget))
        queue = self._queues.get(role, [])
        result = queue.pop(0) if queue else ""
        return _FakeTeammateSession(result)


def _build_team(monkeypatch, role_results: dict[str, list[str]]) -> tuple[Team, list[Any]]:
    captured: list[Any] = []

    async def sink(event):
        captured.append(event)

    factory = _FakeSessionFactory(role_results)
    team = Team(
        workspace=".",
        model="fake-model",
        provider="fake-provider",
        api_key="fake-key",
        use_worktrees=False,
        event_sink=sink,
        lead_env=LocalEnvironment("."),
        lead_tools=[],
        worktree_pool=WorktreePool(".", use_worktrees=False),
        session_factory=factory,
    )
    return team, captured


def _team_events(events: list[Any]) -> list[TeamEvent]:
    return [e for e in events if isinstance(e, TeamEvent)]


def test_delegate_emits_delegation_started_then_completed(monkeypatch):
    team, events = _build_team(monkeypatch, {"coder": ["coder did it"]})

    result = run(team.delegate("coder", "do the thing"))

    assert result == "coder did it"
    seq = _team_events(events)
    assert [e.type for e in seq] == ["delegation_started", "delegation_completed"]

    start = seq[0]
    assert start.data["tool"] == "delegate"
    assert start.data["role"] == "coder"
    assert start.data["task"] == "do the thing"

    end = seq[1]
    assert end.data["tool"] == "delegate"
    assert end.data["role"] == "coder"
    assert "latency" in end.data
    assert isinstance(end.data["latency"], float)
    assert end.data["result_len"] == len("coder did it")


def test_delegate_trims_task_field_to_100_chars(monkeypatch):
    team, events = _build_team(monkeypatch, {"coder": [""]})
    long_task = "x" * 250

    run(team.delegate("coder", long_task))

    seq = _team_events(events)
    assert seq[0].data["task"] == "x" * 100


def test_delegate_with_review_emits_review_lifecycle_around_delegations(monkeypatch):
    team, events = _build_team(
        monkeypatch,
        {
            "coder": ["implemented X"],
            "reviewer": ["Looks good.\nVERDICT: PASS"],
        },
    )

    result = run(team.delegate_with_review("write a function", max_iterations=3))

    assert "PASSED after 1 iteration" in result
    assert "implemented X" in result

    seq = _team_events(events)
    # review_started → coder delegation pair → reviewer delegation pair → review_completed
    assert [e.type for e in seq] == [
        "review_started",
        "delegation_started",
        "delegation_completed",
        "delegation_started",
        "delegation_completed",
        "review_completed",
    ]

    review_start = seq[0]
    assert review_start.data["tool"] == "review_loop"
    assert review_start.data["iteration"] == 1
    assert review_start.data["max"] == 3

    coder_start = seq[1]
    assert coder_start.data["role"] == "coder"

    reviewer_start = seq[3]
    assert reviewer_start.data["role"] == "reviewer"

    review_end = seq[5]
    assert review_end.data["tool"] == "review_loop"
    assert review_end.data["iteration"] == 1
    assert review_end.data["verdict"] == "PASS"


def test_delegate_with_review_iterates_when_reviewer_fails(monkeypatch):
    team, events = _build_team(
        monkeypatch,
        {
            "coder": ["v1 impl", "v2 impl"],
            "reviewer": [
                "Issues found.\nVERDICT: FAIL",
                "All good.\nVERDICT: PASS",
            ],
        },
    )

    result = run(team.delegate_with_review("write fn", max_iterations=3))

    assert "PASSED after 2 iteration" in result
    assert "v2 impl" in result

    seq = _team_events(events)
    review_loops = [e for e in seq if e.data.get("tool") == "review_loop"]
    # 2 iterations × (review_started + review_completed)
    assert [e.type for e in review_loops] == [
        "review_started",
        "review_completed",
        "review_started",
        "review_completed",
    ]
    assert review_loops[0].data["iteration"] == 1
    assert review_loops[1].data["verdict"] == "FAIL"
    assert review_loops[2].data["iteration"] == 2
    assert review_loops[3].data["verdict"] == "PASS"


def test_delegate_does_not_emit_session_tool_events(monkeypatch):
    """Team.delegate must not re-use session_runtime tool_start/tool_end semantics."""
    team, events = _build_team(monkeypatch, {"coder": ["ok"]})

    run(team.delegate("coder", "x"))

    # Every event from team orchestration must be a TeamEvent now.
    types = [getattr(e, "type", None) for e in events]
    assert "tool_start" not in types
    assert "tool_end" not in types
