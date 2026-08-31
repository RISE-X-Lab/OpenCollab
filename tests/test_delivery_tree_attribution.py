"""Which seat was working when a delivered line arrived in the graded tree.

A team run is graded on one directory: the repository agent 0 was given. Every
teammate works in a worktree of its own, cut somewhere else entirely, so nothing
a teammate writes is in the graded tree unless agent 0 puts it there. The
per-agent ``worktree_changes`` rows therefore cannot answer "who wrote the
patch that was graded" -- they say what each seat produced in its own directory.

``record_delivery_tree`` records the graded tree itself, at the boundaries a
team run has: before each turn (under ``serialize_turns`` exactly one seat runs
at a time, so consecutive rows bracket one seat's working period) and at each
teammate message that was queued (the handoff, which is where "had the work
already been done before anyone was asked" is asked).

The end-to-end test below drives a real team over a real repository with a
scripted model, and pins the reading the paper depends on: the analyst's edit is
in the tree at the moment it hands off, and the coder's own write never reaches
the graded tree at all.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application._scheduler_delivery_tree import (
    DELIVERY_DIFF_SNAPSHOT_CHARS,
)
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap import container
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionState
from opencollab.domain.team import Topology
from opencollab.sdk import OpenCollab

ANALYST_AID, CODER_AID = 0, 1

ANALYST_LINE = "written-by-the-analyst"
CODER_LINE = "written-by-the-coder"


# --------------------------------------------------------------------------- #
# end to end: a real team, a real repository, a scripted model
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "tests@example.com")
    _git(path, "config", "user.name", "OpenCollab Tests")
    (path / "which.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "baseline")
    return path


def _team_file(path: Path) -> Path:
    path.write_text(
        """
entry: analyst
roles:
  analyst:
    prompt: You are the Analyst.
    tools: [bash, message_agent]
  coder:
    prompt: You are the Coder.
    tools: [bash, message_agent]
topology:
  analyst: [coder]
  coder: [analyst]
""".strip(),
        encoding="utf-8",
    )
    return path


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _scripted_llm():
    """Analyst: edit the graded tree, then hand off. Coder: edit its own tree."""

    class ScriptedLLM:
        def __init__(self, **_kwargs) -> None:
            self.role = "?"
            self.turn = 0

        def context_window(self) -> int:
            return 200_000

        async def close(self) -> None:
            return None

        async def complete(self, messages, **_kwargs) -> LLMResponse:
            system = str(messages[0].get("content") or "")
            for role in ("Analyst", "Coder"):
                if f"You are the {role}" in system:
                    self.role = role.lower()
                    break
            self.turn += 1
            usage = Usage(input_tokens=10, output_tokens=5)
            if self.role == "analyst":
                if self.turn == 1:
                    return LLMResponse(
                        tool_calls=[
                            _call(
                                "a1",
                                "bash",
                                {"command": f"echo {ANALYST_LINE} >> which.txt"},
                            )
                        ],
                        usage=usage,
                        finish_reason="tool_calls",
                    )
                if self.turn == 2:
                    return LLMResponse(
                        tool_calls=[
                            _call(
                                "a2",
                                "message_agent",
                                {
                                    "to_aid": CODER_AID,
                                    "summary": "take it from here",
                                    "content": "Finish the change.",
                                },
                            )
                        ],
                        usage=usage,
                        finish_reason="tool_calls",
                    )
            elif self.turn == 1:
                return LLMResponse(
                    tool_calls=[
                        _call(
                            "c1",
                            "bash",
                            {"command": f"echo {CODER_LINE} >> which.txt"},
                        )
                    ],
                    usage=usage,
                    finish_reason="tool_calls",
                )
            return LLMResponse(content="done", usage=usage, finish_reason="stop")

    return ScriptedLLM


@pytest.fixture
def run_team_in(tmp_path, monkeypatch):
    async def run(*, record_delivery_tree: bool) -> dict:
        repo = _repo(tmp_path / "repo")
        anchor = tmp_path / "anchor"
        anchor.mkdir()
        monkeypatch.setattr(container, "LLMClient", _scripted_llm())
        client = OpenCollab(
            anchor,
            config={
                "model": "gpt-4o",
                "provider": "openai",
                "api_key": "test-key",  # pragma: allowlist secret
                "base_url": None,
                "budget": 2_000_000,
            },
            environment=LocalEnvironment(str(repo)),
        )
        result = await client.team(
            "Fix it.",
            config=_team_file(tmp_path / "team.yaml"),
            artifacts=tmp_path / "artifacts",
            trace=True,
            use_worktrees=True,
            prebuild_team=True,
            allow_unisolated_shell=True,
            serialize_turns=True,
            record_delivery_tree=record_delivery_tree,
        )
        return {"result": result, "repo": repo}

    return run


async def test_a_team_run_says_which_seat_was_working_when_a_line_arrived(
    run_team_in,
):
    """The graded tree at each boundary, labelled with the seat that held it."""
    observed = await run_team_in(record_delivery_tree=True)
    snapshots = observed["result"].metrics["tree_snapshots"]

    # Every boundary names a seat, so no row is attributable to nobody.
    assert [(row["at"], row["aid"], row["role"]) for row in snapshots][:1] == [
        ("turn_start", ANALYST_AID, "analyst")
    ]
    assert all(row["role"] is not None for row in snapshots)

    # The analyst's turn opened on a tree nobody had written to.
    assert ANALYST_LINE not in (snapshots[0]["diff"] or "")

    # By the handoff the analyst had already made the change, and the row says
    # who handed to whom.
    handoffs = [row for row in snapshots if row["at"] == "message_sent"]
    assert [(row["aid"], row["to_aid"], row["to_role"]) for row in handoffs] == [
        (ANALYST_AID, CODER_AID, "coder")
    ]
    assert ANALYST_LINE in handoffs[0]["diff"]
    assert handoffs[0]["files"] == ["which.txt"]


async def test_a_teammates_own_write_never_reaches_the_graded_tree(run_team_in):
    """The finding the arm exists to measure, as a property of the record.

    The coder does edit a file -- in its own worktree. The graded tree is
    unchanged across the coder's whole turn, so the delivered patch contains
    nothing the coder wrote, and the record says so rather than leaving it to
    be inferred from an absence.
    """
    observed = await run_team_in(record_delivery_tree=True)
    snapshots = observed["result"].metrics["tree_snapshots"]

    coder_turns = [
        row
        for row in snapshots
        if row["at"] == "turn_start" and row["aid"] == CODER_AID
    ]
    assert coder_turns, "the coder never took a turn"
    # Repeats an already-recorded state: the text is stored once and pointed at.
    for row in coder_turns:
        assert "unchanged_since" in row
        assert "diff" not in row

    assert all(CODER_LINE not in (row.get("diff") or "") for row in snapshots)
    assert CODER_LINE not in (observed["repo"] / "which.txt").read_text(
        encoding="utf-8"
    )


async def test_a_team_run_that_was_not_asked_to_record_says_nothing(run_team_in):
    """The control: off, the key is absent -- not present and empty."""
    metrics = (await run_team_in(record_delivery_tree=False))["result"].metrics

    assert "tree_snapshots" not in metrics


# --------------------------------------------------------------------------- #
# unit: the recorder's own contract
# --------------------------------------------------------------------------- #


class _FakeSession:
    def __init__(self, role: str) -> None:
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()
        self.added: list[str] = []

    async def add_user_message(self, content: str) -> None:
        self.added.append(content)

    async def run_loop(self) -> str:
        return ""


class _FakeFactory:
    def build_spawn_session(self, **_kwargs):
        return _FakeSession("coder")


class _FakeProbe:
    """A working tree whose diff the test moves, or which refuses to answer."""

    def __init__(self, text: str = "", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.calls = 0

    async def diff(self) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("git said no")
        return self.text


def _scheduler(probe, *, topology=None):
    async def sink(event):
        return None

    scheduler = Scheduler(
        session_factory=_FakeFactory(),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(sink),
        topology=topology,
        roles=("lead", "coder"),
        delivery_tree_probe=probe,
    )
    scheduler.register_lead(_FakeSession("lead"))
    child = _FakeSession("coder")
    child.state.aid = 1
    scheduler.table.add(
        SessionControlBlock(aid=1, parent_aid=0, agent=child.agent, state=child.state)
    )
    scheduler._sessions[1] = child
    return scheduler


def _hold_busy(scheduler, aid: int):
    """Park a never-finishing driver on ``aid`` so its inbox cannot drain."""
    scheduler._tasks[aid] = asyncio.get_running_loop().create_future()


async def test_a_scheduler_without_a_probe_records_nothing_and_runs_no_git():
    """Default off: every existing caller keeps its exact previous behaviour."""
    scheduler = _scheduler(None)

    await scheduler.snapshot_delivery_tree("turn_start", aid=0)

    assert scheduler.delivery_tree_snapshots == ()


async def test_a_queued_message_is_recorded_and_a_refused_one_is_not():
    """A refusal carried nothing across, so it is not a boundary."""
    closed = Topology(edges={"lead": frozenset(), "coder": frozenset()})
    refusing = _scheduler(_FakeProbe("diff --git a/x b/x\n"), topology=closed)
    error = await refusing.send_message(0, 1, "review", "please")
    assert error.startswith("Error: role 'lead' is not permitted")
    assert refusing.delivery_tree_snapshots == ()

    open_team = _scheduler(_FakeProbe("diff --git a/x b/x\n"))
    _hold_busy(open_team, 1)
    assert await open_team.send_message(0, 1, "review", "please") == (
        "Message queued to aid 1."
    )
    assert [row["at"] for row in open_team.delivery_tree_snapshots] == ["message_sent"]
    assert open_team.delivery_tree_snapshots[0]["to_role"] == "coder"


async def test_a_probe_that_fails_leaves_a_hole_that_says_it_failed():
    """"The recorder broke" and "the tree did not change" must not look alike."""
    scheduler = _scheduler(_FakeProbe(fail=True))

    await scheduler.snapshot_delivery_tree("turn_start", aid=0)

    (row,) = scheduler.delivery_tree_snapshots
    assert row["diff"] is None
    assert row["probe_error"].startswith("RuntimeError:")
    assert "sha256" not in row


async def test_a_repeated_tree_state_is_stored_once_and_pointed_at():
    """Most turns cannot change the graded tree; storing each in full is waste."""
    probe = _FakeProbe("diff --git a/x b/x\n+one\n")
    scheduler = _scheduler(probe)

    await scheduler.snapshot_delivery_tree("turn_start", aid=0)
    await scheduler.snapshot_delivery_tree("turn_start", aid=1)
    probe.text = "diff --git a/x b/x\n+one\n+two\n"
    await scheduler.snapshot_delivery_tree("turn_start", aid=0)

    first, repeat, changed = scheduler.delivery_tree_snapshots
    assert "unchanged_since" not in first
    assert first["diff"] == "diff --git a/x b/x\n+one\n"
    assert repeat["unchanged_since"] == 0
    assert "diff" not in repeat
    assert repeat["sha256"] == first["sha256"]
    assert changed["diff"].endswith("+two\n")
    assert "unchanged_since" not in changed


async def test_a_very_large_tree_is_cut_and_says_it_was_cut():
    """One pathological tree must not write megabytes into every run record."""
    text = "diff --git a/x b/x\n" + "+padding\n" * DELIVERY_DIFF_SNAPSHOT_CHARS
    scheduler = _scheduler(_FakeProbe(text))

    await scheduler.snapshot_delivery_tree("turn_start", aid=0)

    (row,) = scheduler.delivery_tree_snapshots
    assert row["truncated"] is True
    assert len(row["diff"]) == DELIVERY_DIFF_SNAPSHOT_CHARS
    assert row["chars"] == len(text)
