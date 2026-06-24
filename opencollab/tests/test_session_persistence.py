"""Autosave persistence: structured per-agent JSON, run folder, manifest.

Covers the SessionStore on-disk format (structured JSON + legacy JSONL
fallback), per-message timestamps (kept as a clean in-memory sidecar, merged
on save), and the per-run folder + team.json manifest wiring.
"""

from __future__ import annotations

import asyncio
import json
import os

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap.container import (
    DefaultSessionFactory,
    SpawnConfig,
    agent_save_path,
    make_run_dir,
)
from opencollab.domain.session import SessionState


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# SessionStore on-disk format
# --------------------------------------------------------------------------


def test_store_save_writes_structured_json_with_meta(tmp_path):
    store = SessionStore()
    path = str(tmp_path / "agent_1_coder.json")
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    store.save(path, messages, meta={"aid": 1, "role": "coder", "model": "gpt-4o"})

    with open(path) as f:
        obj = json.load(f)
    assert obj["aid"] == 1
    assert obj["role"] == "coder"
    assert obj["model"] == "gpt-4o"
    assert obj["messages"] == messages


def test_store_round_trip_structured_json(tmp_path):
    store = SessionStore()
    path = str(tmp_path / "s.json")
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

    store.save(path, messages, meta={"aid": 0})

    assert store.load_messages(path, "fallback") == messages


def test_store_loads_legacy_jsonl(tmp_path):
    path = tmp_path / "legacy.jsonl"
    path.write_text(
        '{"role": "system", "content": "sys"}\n'
        '{"role": "user", "content": "hi"}\n'
    )
    store = SessionStore()

    assert store.load_messages(str(path), "fallback") == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_store_empty_file_falls_back_to_system_prompt(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("")
    store = SessionStore()

    assert store.load_messages(str(path), "the-prompt") == [
        {"role": "system", "content": "the-prompt"}
    ]


def test_store_save_manifest(tmp_path):
    store = SessionStore()
    path = str(tmp_path / "team.json")
    manifest = {"run_id": "r", "agents": [{"aid": 0, "role": "lead", "parent_aid": None}]}

    store.save_manifest(path, manifest)

    with open(path) as f:
        assert json.load(f) == manifest


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------


def test_agent_save_path_naming(tmp_path):
    assert agent_save_path(str(tmp_path), 2, "reviewer") == os.path.join(
        str(tmp_path), "agent_2_reviewer.json"
    )


def test_make_run_dir_is_timestamped_and_collision_safe(tmp_path):
    a = make_run_dir(str(tmp_path))
    base = os.path.join(str(tmp_path), ".opencollab", "sessions")
    assert a.startswith(base)
    os.makedirs(a)
    b = make_run_dir(str(tmp_path))
    assert b != a  # same-second collision gets a suffix


def test_make_run_dir_prefix_distinguishes_workflow_from_team(tmp_path):
    from opencollab.bootstrap.session_factory import WORKFLOW_RUN_PREFIX

    base = os.path.join(str(tmp_path), ".opencollab", "sessions")
    team = make_run_dir(str(tmp_path))
    workflow = make_run_dir(str(tmp_path), prefix=WORKFLOW_RUN_PREFIX)

    # Both share the sessions/ parent, but the workflow folder name carries the
    # prefix so an ``ls`` tells the two apart at a glance.
    assert os.path.dirname(team) == base
    assert os.path.dirname(workflow) == base
    assert os.path.basename(workflow).startswith(WORKFLOW_RUN_PREFIX)
    assert not os.path.basename(team).startswith(WORKFLOW_RUN_PREFIX)


# --------------------------------------------------------------------------
# Per-message timestamps (clean in-memory sidecar, merged on save)
# --------------------------------------------------------------------------


def test_append_keeps_messages_clean_and_tracks_timestamps():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    state.append_message({"role": "user", "content": "hi"})

    # In-memory messages stay API-shaped (no timestamp key).
    assert state.messages == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert len(state.message_timestamps) == 2
    # ...but enrichment merges a timestamp into each persisted message.
    enriched = state.enriched_messages()
    assert all("timestamp" in m for m in enriched)


def test_replace_messages_preserves_embedded_and_object_timestamps():
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    kept = state.messages[0]  # same object preserved across replace
    kept_ts = state.message_timestamps[0]
    resumed = {"role": "user", "content": "old", "timestamp": "2020-01-01T00:00:00+00:00"}

    state.replace_messages([kept, resumed])

    # The preserved object keeps its original (construction-time) timestamp.
    assert state.message_timestamps[0] == kept_ts
    # An embedded timestamp is lifted out into the sidecar, message left clean.
    assert state.messages[1] == {"role": "user", "content": "old"}
    assert state.message_timestamps[1] == "2020-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------
# Factory threads per-agent save path to children
# --------------------------------------------------------------------------


def _spawn_cfg() -> SpawnConfig:
    return SpawnConfig(
        model="gpt-4o",
        provider="openai",
        api_key="test-key",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(),
        permission_policy=None,
    )


def test_factory_gives_child_its_own_structured_save_file(tmp_path):
    run_dir = str(tmp_path / "run")
    factory = DefaultSessionFactory(_spawn_cfg(), save_dir=run_dir)
    env = LocalEnvironment(str(tmp_path))

    session = factory.build_spawn_session(role="coder", env=env, budget=1000, aid=2)

    expected = agent_save_path(run_dir, 2, "coder")
    assert session.auto_save_path == expected

    session.save(expected)
    with open(expected) as f:
        saved = json.load(f)
    assert saved["aid"] == 2
    assert saved["role"] == "coder"
    assert "messages" in saved


def test_factory_without_save_dir_leaves_children_unsaved(tmp_path):
    factory = DefaultSessionFactory(_spawn_cfg())
    env = LocalEnvironment(str(tmp_path))

    session = factory.build_spawn_session(role="coder", env=env, budget=1000, aid=2)

    assert session.auto_save_path is None


# --------------------------------------------------------------------------
# Manifest captures the live roster incl. parent/child links
# --------------------------------------------------------------------------


class _FakeChildSession:
    def __init__(self, role: str):
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("_Agent", (), {"name": role})()

    async def add_user_message(self, content: str) -> None:
        pass

    async def run_loop(self) -> str:
        return "done"


class _FakeLeadSession(_FakeChildSession):
    def __init__(self):
        super().__init__("lead")
        self.tool_execution = type("_TP", (), {"safety_policy": None, "env": None})()
        self.runner = type("_R", (), {"max_steps": 100})()
        self.max_steps = 100
        self.env = None


class _FakeFactory:
    def build_spawn_session(self, *, role, env, budget, max_steps=50, aid=-1, scheduler=None, task=None, context=""):
        return _FakeChildSession(role)


def test_manifest_records_spawned_child_parent_link(tmp_path):
    run_dir = str(tmp_path / "run")
    store = SessionStore()
    manifest_path = os.path.join(run_dir, "team.json")

    scheduler = Scheduler(
        session_factory=_FakeFactory(),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )

    def _write_manifest():
        store.save_manifest(manifest_path, {
            "run_id": "run",
            "agents": scheduler.team_snapshot(),
        })

    scheduler.set_manifest_writer(_write_manifest)
    scheduler.register_lead(_FakeLeadSession())

    child_aid = run(scheduler.spawn(0, "coder", "do it"))
    if child_aid in scheduler._tasks:
        run(asyncio.wait_for(scheduler._tasks[child_aid], timeout=1.0))

    with open(manifest_path) as f:
        manifest = json.load(f)
    agents = {a["aid"]: a for a in manifest["agents"]}
    assert agents[0]["role"] == "lead"
    assert agents[0]["parent_aid"] is None
    assert agents[child_aid]["role"] == "coder"
    assert agents[child_aid]["parent_aid"] == 0
