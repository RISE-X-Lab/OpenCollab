"""Autosave persistence: structured per-agent JSON, run folder, manifest.

Covers the SessionStore on-disk format (structured JSON + legacy JSONL
fallback), per-message timestamps (kept as a clean in-memory sidecar, merged
on save), and the per-run folder + team.json manifest wiring.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os

import pytest

import opencollab.adapters.safe_files as safe_files_mod
import opencollab.adapters.storage as storage_mod
import opencollab.bootstrap.session_factory as session_factory_mod
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


def run(coro):
    return asyncio.run(coro)


async def _spawn_and_settle(scheduler, *args, **kwargs):
    """Spawn and await the resulting background task within one event loop.

    Splitting spawn and the task await across two ``asyncio.run`` calls binds the
    ``_drive_agent`` task to a different loop than the one that awaits it. Keeping
    both in one coroutine avoids the cross-loop ``ValueError``.
    """
    aid = await scheduler.spawn(*args, **kwargs)
    task = scheduler._tasks.get(aid)
    if task is not None:
        await asyncio.wait_for(task, timeout=1.0)
    return aid


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


def test_store_save_at_utf8_byte_limit_is_always_loadable(tmp_path, monkeypatch):
    store = SessionStore()
    path = str(tmp_path / "boundary.json")
    messages = [{"role": "user", "content": "€" * 37}]
    document = {"snapshot_version": 1, "messages": messages}
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    monkeypatch.setattr(storage_mod, "MAX_SESSION_SNAPSHOT_BYTES", len(encoded))

    store.save(path, messages, meta={"snapshot_version": 1})

    assert (tmp_path / "boundary.json").read_bytes() == encoded
    assert store.load_snapshot(path, "fallback")["messages"] == messages


def test_store_oversize_save_preserves_old_snapshot_and_removes_temp(
    tmp_path,
    monkeypatch,
):
    store = SessionStore()
    path = str(tmp_path / "state.json")
    old_messages = [{"role": "system", "content": "old"}]
    store.save(path, old_messages)
    old_payload = (tmp_path / "state.json").read_bytes()
    monkeypatch.setattr(storage_mod, "MAX_SESSION_SNAPSHOT_BYTES", 512)

    with pytest.raises(ValueError, match=r"exceeds 512 UTF-8 bytes"):
        store.save(path, [{"role": "user", "content": "€" * 1000}])

    assert (tmp_path / "state.json").read_bytes() == old_payload
    assert store.load_snapshot(path, "fallback")["messages"] == old_messages
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_store_rejects_fifo_snapshot_without_blocking(tmp_path):
    path = tmp_path / "snapshot.fifo"
    os.mkfifo(path)

    with pytest.raises(OSError, match="not a regular file"):
        SessionStore().load_snapshot(str(path), "fallback")


def test_store_rejects_final_symlink_snapshot(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("[]", encoding="utf-8")
    link = tmp_path / "snapshot.json"
    link.symlink_to(target)

    with pytest.raises(OSError):
        SessionStore().load_snapshot(str(link), "fallback")


def test_store_rejects_oversized_snapshot_before_read(tmp_path):
    path = tmp_path / "huge.json"
    with path.open("wb") as handle:
        handle.truncate(storage_mod.MAX_SESSION_SNAPSHOT_BYTES + 1)

    with pytest.raises(ValueError, match="exceeds"):
        SessionStore().load_snapshot(str(path), "fallback")


def test_store_save_manifest(tmp_path):
    store = SessionStore()
    path = str(tmp_path / "team.json")
    manifest = {"run_id": "r", "agents": [{"aid": 0, "role": "lead", "parent_aid": None}]}

    store.save_manifest(path, manifest)

    with open(path) as f:
        assert json.load(f) == manifest


@pytest.mark.parametrize("method", ["save", "save_manifest"])
def test_atomic_store_failure_preserves_previous_file(tmp_path, monkeypatch, method):
    store = SessionStore()
    path = str(tmp_path / "state.json")
    original = {"messages": [{"role": "system", "content": "valid"}]}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(original, handle)

    def partial_dump(value, handle, **kwargs):
        handle.write('{"partial": ')
        raise TypeError("synthetic serialization failure")

    monkeypatch.setattr(json, "dump", partial_dump)
    with pytest.raises(TypeError):
        if method == "save":
            store.save(path, [{"role": "user", "content": "new"}])
        else:
            store.save_manifest(path, {"run_id": "new"})

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == original
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_store_reports_directory_fsync_failure_after_replace(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "state.json")
    real_fsync = storage_mod.os.fsync
    calls = 0

    def fail_directory_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(storage_mod.os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        SessionStore().save_manifest(path, {"run_id": "new"})

    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8")) == {
        "run_id": "new"
    }
    assert calls == 2
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_store_parent_reopen_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "state.json")
    SessionStore().save_manifest(path, {"run_id": "old"})
    original = (tmp_path / "state.json").read_bytes()
    real_open_directory = storage_mod._open_directory_no_symlinks
    calls = 0

    def fail_directory_open(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory open failed")
        return real_open_directory(target)

    monkeypatch.setattr(storage_mod, "_open_directory_no_symlinks", fail_directory_open)

    with pytest.raises(OSError, match="directory open failed"):
        SessionStore().save_manifest(path, {"run_id": "new"})

    assert (tmp_path / "state.json").read_bytes() == original
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_atomic_store_rejects_nonregular_final_target(tmp_path, kind):
    if kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO requires POSIX")
    path = tmp_path / "state.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"run_id": "outside"}', encoding="utf-8")
    if kind == "symlink":
        path.symlink_to(outside)
    else:
        os.mkfifo(path)

    with pytest.raises(OSError, match="not a regular file"):
        SessionStore().save_manifest(str(path), {"run_id": "new"})

    assert outside.read_text(encoding="utf-8") == '{"run_id": "outside"}'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_store_rejects_symlinked_parent_without_outside_write(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "sessions"
    parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="not a real directory"):
        SessionStore().save_manifest(str(parent / "state.json"), {"run_id": "new"})

    assert list(outside.iterdir()) == []


def test_atomic_store_detects_parent_swap_before_replace(tmp_path, monkeypatch):
    parent = tmp_path / "sessions"
    parent.mkdir()
    path = parent / "state.json"
    path.write_text('{"run_id": "old"}', encoding="utf-8")
    real_dump = storage_mod.json.dump
    swapped = False

    def swapping_dump(value, handle, **kwargs):
        nonlocal swapped
        result = real_dump(value, handle, **kwargs)
        old_parent = tmp_path / "sessions-old"
        parent.rename(old_parent)
        parent.mkdir()
        swapped = True
        return result

    monkeypatch.setattr(storage_mod.json, "dump", swapping_dump)

    with pytest.raises(OSError, match="parent changed"):
        SessionStore().save_manifest(str(path), {"run_id": "new"})

    assert swapped is True
    assert list(parent.iterdir()) == []
    old_parent = tmp_path / "sessions-old"
    assert (old_parent / "state.json").read_text(encoding="utf-8") == '{"run_id": "old"}'
    assert list(old_parent.glob(".state.json.*.tmp")) == []


def test_atomic_store_detects_parent_swap_after_replace(tmp_path, monkeypatch):
    parent = tmp_path / "sessions"
    parent.mkdir()
    path = parent / "state.json"
    path.write_text('{"run_id": "old"}', encoding="utf-8")
    old_parent = tmp_path / "sessions-old"
    real_replace = storage_mod.os.replace
    swapped = False

    def swapping_replace(source, destination, *args, **kwargs):
        nonlocal swapped
        result = real_replace(source, destination, *args, **kwargs)
        parent.rename(old_parent)
        parent.mkdir()
        path.write_text('{"run_id": "visible"}', encoding="utf-8")
        swapped = True
        return result

    monkeypatch.setattr(storage_mod.os, "replace", swapping_replace)

    with pytest.raises(OSError, match="parent changed after atomic replace"):
        SessionStore().save_manifest(str(path), {"run_id": "new"})

    assert swapped is True
    assert path.read_text(encoding="utf-8") == '{"run_id": "visible"}'
    assert json.loads((old_parent / "state.json").read_text(encoding="utf-8")) == {
        "run_id": "new"
    }
    assert list(old_parent.glob(".state.json.*.tmp")) == []


def test_safe_atomic_write_preserves_primary_error_when_temp_unlink_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "state.bin"

    def fail_write(_fd, _payload):
        raise RuntimeError("primary write failed")

    def fail_unlink(_name, *args, **kwargs):
        raise OSError("cleanup unlink failed")

    monkeypatch.setattr(safe_files_mod.os, "write", fail_write)
    monkeypatch.setattr(safe_files_mod.os, "unlink", fail_unlink)

    with pytest.raises(RuntimeError, match="primary write failed") as raised:
        safe_files_mod.write_regular_bytes_atomic(path, b"payload")

    assert any(
        "temporary unlink" in note and "cleanup unlink failed" in note
        for note in raised.value.__notes__
    )


def test_safe_atomic_write_supports_name_max_destination(tmp_path):
    path = tmp_path / ("x" * 255)

    safe_files_mod.write_regular_bytes_atomic(path, b"payload")

    assert path.read_bytes() == b"payload"


def test_session_store_preserves_serialization_error_when_temp_unlink_fails(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "state.json")

    def fail_dump(*_args, **_kwargs):
        raise RuntimeError("primary serialization failed")

    def fail_unlink(_name, *args, **kwargs):
        raise OSError("cleanup unlink failed")

    monkeypatch.setattr(storage_mod.json, "dump", fail_dump)
    monkeypatch.setattr(storage_mod.os, "unlink", fail_unlink)

    with pytest.raises(RuntimeError, match="primary serialization failed") as raised:
        SessionStore().save_manifest(path, {"run_id": "new"})

    assert any(
        "temporary unlink" in note and "cleanup unlink failed" in note
        for note in raised.value.__notes__
    )


def test_session_store_preserves_primary_error_when_parent_close_fails(
    tmp_path,
    monkeypatch,
):
    path = str(tmp_path / "state.json")
    opened_parent_fds: list[int] = []
    real_open_directory = storage_mod._open_directory_no_symlinks
    real_close = storage_mod.os.close

    def tracking_open_directory(target):
        fd = real_open_directory(target)
        opened_parent_fds.append(fd)
        return fd

    def fail_parent_close(fd):
        if opened_parent_fds and fd == opened_parent_fds[0]:
            real_close(fd)
            raise OSError("parent close failed")
        return real_close(fd)

    def fail_dump(*_args, **_kwargs):
        raise RuntimeError("primary serialization failed")

    monkeypatch.setattr(storage_mod, "_open_directory_no_symlinks", tracking_open_directory)
    monkeypatch.setattr(storage_mod.json, "dump", fail_dump)
    monkeypatch.setattr(storage_mod.os, "close", fail_parent_close)

    with pytest.raises(RuntimeError, match="primary serialization failed") as raised:
        SessionStore().save_manifest(path, {"run_id": "new"})

    assert any(
        "parent directory fd close" in note and "parent close failed" in note
        for note in raised.value.__notes__
    )


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------


def test_agent_save_path_naming(tmp_path):
    assert agent_save_path(str(tmp_path), 2, "reviewer") == os.path.join(
        str(tmp_path), f"agent_2_{role_storage_slug('reviewer')}.json"
    )


def test_make_run_dir_is_timestamped_and_collision_safe(tmp_path):
    a = make_run_dir(str(tmp_path))
    base = os.path.join(str(tmp_path), ".opencollab", "sessions")
    assert a.startswith(base)
    assert os.path.isdir(a)
    b = make_run_dir(str(tmp_path))
    assert b != a  # same-second collision gets a suffix
    assert os.path.isdir(b)


def test_make_run_dir_concurrently_reserves_unique_directories(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        paths = list(executor.map(lambda _index: make_run_dir(str(tmp_path)), range(32)))

    assert len(set(paths)) == len(paths)
    assert all(os.path.isdir(path) for path in paths)


@pytest.mark.parametrize(
    "role",
    ["../../../escaped", "/absolute", "bad\\role", "line\nbreak", ".."],
)
def test_agent_save_path_rejects_unsafe_role_without_writing_outside(
    tmp_path,
    role,
):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="role"):
        agent_save_path(str(run_dir), 1, role)

    assert list(tmp_path.iterdir()) == [run_dir]


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


def test_make_run_dir_rejects_symlinked_state_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".opencollab").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="not a real directory"):
        make_run_dir(str(tmp_path))

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("prefix", ["../escape", "/absolute", "bad\\prefix", "x\n"])
def test_make_run_dir_rejects_unsafe_prefix(tmp_path, prefix):
    with pytest.raises(ValueError, match="prefix"):
        make_run_dir(str(tmp_path), prefix=prefix)


def test_make_run_dir_detects_parent_chain_swap_before_return(tmp_path, monkeypatch):
    real_mkdir = session_factory_mod.os.mkdir
    swapped = False

    def swapping_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        result = real_mkdir(path, mode, dir_fd=dir_fd)
        if (
            not swapped
            and dir_fd is not None
            and path not in {".opencollab", "sessions"}
        ):
            swapped = True
            state = tmp_path / ".opencollab"
            old = tmp_path / ".opencollab-old"
            os.rename(state, old)
            real_mkdir(state)
            real_mkdir(state / "sessions")
        return result

    monkeypatch.setattr(session_factory_mod.os, "mkdir", swapping_mkdir)

    with pytest.raises(OSError):
        make_run_dir(str(tmp_path))

    assert swapped is True
    assert list((tmp_path / ".opencollab-old" / "sessions").iterdir()) == []
    assert list((tmp_path / ".opencollab" / "sessions").iterdir()) == []


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

    child_aid = run(_spawn_and_settle(scheduler, 0, "coder", "do it"))

    with open(manifest_path) as f:
        manifest = json.load(f)
    agents = {a["aid"]: a for a in manifest["agents"]}
    assert agents[0]["role"] == "lead"
    assert agents[0]["parent_aid"] is None
    assert agents[child_aid]["role"] == "coder"
    assert agents[child_aid]["parent_aid"] == 0
