"""Black-box persistence checks for sessions, run directories, and manifests."""

from __future__ import annotations

import concurrent.futures
import json
import os

import pytest

from opencollab.adapters import storage as storage_module
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
from opencollab.domain.identity import role_storage_slug
from opencollab.domain.session import SessionState


def test_store_round_trip_preserves_metadata_and_messages(tmp_path) -> None:
    path = tmp_path / "agent.json"
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    store = SessionStore()
    store.save(str(path), messages, meta={"aid": 1, "role": "coder"})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["aid"] == 1
    assert document["role"] == "coder"
    assert store.load_messages(str(path), "fallback") == messages


def test_store_save_at_exact_utf8_limit_remains_loadable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "boundary.json"
    messages = [{"role": "user", "content": "€" * 37}]
    encoded = json.dumps(
        {"snapshot_version": 1, "messages": messages},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", len(encoded))
    SessionStore().save(str(path), messages, meta={"snapshot_version": 1})
    assert path.read_bytes() == encoded
    assert SessionStore().load_snapshot(str(path), "fallback")["messages"] == messages


def test_oversized_save_preserves_previous_snapshot(tmp_path, monkeypatch) -> None:
    path = tmp_path / "state.json"
    store = SessionStore()
    store.save(str(path), [{"role": "system", "content": "old"}])
    original = path.read_bytes()
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", 512)
    with pytest.raises(ValueError, match="exceeds"):
        store.save(str(path), [{"role": "user", "content": "€" * 1000}])
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".state.json.tmp-*"))


def test_store_loads_legacy_jsonl_and_empty_fallback(tmp_path) -> None:
    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        '{"role":"system","content":"sys"}\n{"role":"user","content":"hi"}\n',
        encoding="utf-8",
    )
    assert SessionStore().load_messages(str(legacy), "fallback")[-1]["content"] == "hi"
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert SessionStore().load_messages(str(empty), "prompt") == [
        {"role": "system", "content": "prompt"}
    ]


def test_store_replays_durable_journal_and_ignores_partial_tail(tmp_path) -> None:
    path = tmp_path / "session.json"
    journal = tmp_path / "session.json.journal"
    store = SessionStore()
    base = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "step 1"},
    ]
    store.checkpoint_snapshot(
        str(path),
        base,
        meta={"session_state": {"step_count": 1}},
        sequence=1,
    )
    store.append_snapshot_delta(
        str(path),
        sequence=2,
        replace_from=2,
        messages=[{"role": "assistant", "content": "step 2"}],
        meta={"session_state": {"step_count": 2}},
    )
    with journal.open("ab") as handle:
        handle.write(b'{"journal_version":1,"sequence":3')

    restored = store.load_snapshot(str(path), "fallback")

    assert restored["session_state"]["step_count"] == 2
    assert [message["content"] for message in restored["messages"]] == [
        "sys",
        "step 1",
        "step 2",
    ]

    store.append_snapshot_delta(
        str(path),
        sequence=3,
        replace_from=3,
        messages=[{"role": "assistant", "content": "step 3"}],
        meta={"session_state": {"step_count": 3}},
    )
    resumed = store.load_snapshot(str(path), "fallback")
    assert resumed["session_state"]["step_count"] == 3
    assert resumed["messages"][-1]["content"] == "step 3"


def test_store_replay_ignores_journal_records_covered_by_atomic_base(tmp_path) -> None:
    path = tmp_path / "session.json"
    store = SessionStore()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "step 1"},
        {"role": "assistant", "content": "step 2"},
    ]
    store.checkpoint_snapshot(
        str(path),
        messages,
        meta={"session_state": {"step_count": 2}},
        sequence=2,
    )
    # Models a crash after the base rename but before journal compaction:
    # the covered absolute record must not duplicate its message suffix.
    store.append_snapshot_delta(
        str(path),
        sequence=2,
        replace_from=2,
        messages=[messages[-1]],
        meta={"session_state": {"step_count": 2}},
    )
    store.append_snapshot_delta(
        str(path),
        sequence=3,
        replace_from=3,
        messages=[{"role": "assistant", "content": "step 3"}],
        meta={"session_state": {"step_count": 3}},
    )

    restored = store.load_snapshot(str(path), "fallback")

    assert restored["session_state"]["step_count"] == 3
    assert [message["content"] for message in restored["messages"]] == [
        "sys",
        "step 1",
        "step 2",
        "step 3",
    ]


def test_store_rejects_complete_invalid_journal_record(tmp_path) -> None:
    path = tmp_path / "session.json"
    store = SessionStore()
    store.checkpoint_snapshot(
        str(path),
        [{"role": "system", "content": "sys"}],
        meta={},
        sequence=0,
    )
    (tmp_path / "session.json.journal").write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="journal record at line 1"):
        store.load_snapshot(str(path), "fallback")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_store_rejects_fifo_without_blocking(tmp_path) -> None:
    path = tmp_path / "snapshot.fifo"
    os.mkfifo(path)
    with pytest.raises(OSError, match="regular file"):
        SessionStore().load_snapshot(str(path), "fallback")


def test_store_rejects_symlink_and_oversized_snapshot(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("[]", encoding="utf-8")
    link = tmp_path / "snapshot.json"
    link.symlink_to(target)
    with pytest.raises(OSError):
        SessionStore().load_snapshot(str(link), "fallback")
    huge = tmp_path / "huge.json"
    with huge.open("wb") as handle:
        handle.truncate(storage_module.MAX_SESSION_SNAPSHOT_BYTES + 1)
    with pytest.raises(ValueError, match="exceeds"):
        SessionStore().load_snapshot(str(huge), "fallback")


@pytest.mark.parametrize("method", ["save", "save_manifest"])
def test_serialization_failure_preserves_previous_file(tmp_path, monkeypatch, method) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"run_id":"old"}', encoding="utf-8")

    def fail_dump(*_args, **_kwargs):
        raise TypeError("serialization failed")

    monkeypatch.setattr(storage_module.json, "dump", fail_dump)
    with pytest.raises(TypeError, match="serialization failed"):
        if method == "save":
            SessionStore().save(str(path), [{"role": "user", "content": "new"}])
        else:
            SessionStore().save_manifest(str(path), {"run_id": "new"})
    assert path.read_text(encoding="utf-8") == '{"run_id":"old"}'


def test_store_rejects_nonregular_target_and_symlink_parent(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "sessions"
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="real directory"):
        SessionStore().save_manifest(str(parent / "state.json"), {"run_id": "new"})
    assert list(outside.iterdir()) == []


def test_agent_save_path_and_run_directory_are_collision_safe(tmp_path) -> None:
    assert agent_save_path(str(tmp_path), 2, "reviewer") == os.path.join(
        str(tmp_path), f"agent_2_{role_storage_slug('reviewer')}.json"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(lambda _index: make_run_dir(str(tmp_path)), range(16)))
    assert len(set(paths)) == 16
    assert all(os.path.isdir(path) for path in paths)


@pytest.mark.parametrize("role", ["../../../escaped", "/absolute", "bad\\role", "line\nbreak", ".."])
def test_agent_save_path_rejects_unsafe_role(tmp_path, role) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with pytest.raises(ValueError, match="role"):
        agent_save_path(str(run_dir), 1, role)
    assert list(tmp_path.iterdir()) == [run_dir]


def test_make_run_dir_rejects_symlinked_state_parent(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".opencollab").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        make_run_dir(str(tmp_path))
    assert list(outside.iterdir()) == []


def test_message_timestamp_sidecar_keeps_runtime_messages_clean() -> None:
    state = SessionState(messages=[{"role": "system", "content": "sys"}])
    state.append_message({"role": "user", "content": "hi"})
    assert all("timestamp" not in message for message in state.messages)
    assert all("timestamp" in message for message in state.enriched_messages())


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


def test_factory_assigns_structured_child_save_path(tmp_path) -> None:
    run_dir = str(tmp_path / "run")
    session = DefaultSessionFactory(_spawn_cfg(), save_dir=run_dir).build_spawn_session(
        role="coder",
        env=LocalEnvironment(str(tmp_path)),
        budget=1000,
        aid=2,
    )
    expected = agent_save_path(run_dir, 2, "coder")
    assert session.auto_save_path == expected
    session.save(expected)
    saved = json.loads(open(expected, encoding="utf-8").read())
    assert saved["aid"] == 2
    assert saved["role"] == "coder"


class _FakeSession:
    def __init__(self, role: str):
        self.used_tokens = 0
        self.state = SessionState(messages=[])
        self.agent = type("Agent", (), {"name": role})()

    async def add_user_message(self, _content: str) -> None:
        return None

    async def run_loop(self) -> str:
        return "done"


class _FakeLead(_FakeSession):
    def __init__(self):
        super().__init__("lead")
        self.tool_execution = type("Tools", (), {"safety_policy": None, "env": None})()
        self.runner = type("Runner", (), {"max_steps": 100})()
        self.max_steps = 100
        self.env = None


class _FakeFactory:
    def build_spawn_session(self, *, role, **_kwargs):
        return _FakeSession(role)


async def test_manifest_records_parent_child_relationship(tmp_path) -> None:
    path = tmp_path / "run" / "team.json"
    path.parent.mkdir()
    scheduler = Scheduler(
        session_factory=_FakeFactory(),
        worktree_pool=WorktreePool(".", use_worktrees=False),
        event_sink=EventBus(None),
    )
    def write_manifest() -> None:
        SessionStore().save_manifest(
            str(path), {"run_id": "run", "agents": scheduler.team_snapshot()}
        )

    scheduler.set_manifest_writer(write_manifest)
    scheduler.register_lead(_FakeLead())
    child = await scheduler.spawn(0, "coder", "do it")
    task = scheduler._tasks.get(child)
    if task is not None:
        await task
    write_manifest()
    agents = {entry["aid"]: entry for entry in json.loads(path.read_text())["agents"]}
    assert agents[0]["parent_aid"] is None
    assert agents[child]["parent_aid"] == 0
