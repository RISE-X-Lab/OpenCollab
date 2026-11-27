"""Exported conversation snapshots can resume their original autosave target."""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import AsyncExitStack

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.storage import SessionStore
from opencollab.application.ports import SnapshotSequenceConflict
from opencollab.bootstrap import build_session, load_session
from opencollab.domain import session as session_module
from opencollab.domain.agent import Agent
from opencollab.tools import builtin_tools
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.session_run_test_support import FakeLLM, llm_response, tool_call


def model(turns=1):
    return FakeLLM([
        llm_response(content=f"Completed request {index}.", total_tokens=7, input_tokens=5)
        for index in range(turns)
    ])

async def converse(session, turns):
    for index in range(turns):
        await session.add_user_message(f"Remember requirement {index}.")
        await session.run_loop()

def transcript(snapshot):
    return [{key: value for key, value in row.items() if key != "timestamp"}
            for row in snapshot["messages"]]


@pytest.mark.parametrize(("borrow_env", "borrow_llm", "close_fails"), [
    (False, False, False), (False, True, False),
    (True, False, False), (True, True, False),
    (False, False, True),
])
async def test_failed_load_releases_only_owned_resources(tmp_path, monkeypatch, borrow_env, borrow_llm, close_fails):
    monkeypatch.chdir(tmp_path)
    source = str(tmp_path / "export.json")
    target = str(tmp_path / "autosave.json")
    store = SessionStore()
    store.checkpoint_snapshot(source, [{"role": "user", "content": "old request"}], meta={}, sequence=2)
    store.checkpoint_snapshot(target, [{"role": "user", "content": "new request"}], meta={}, sequence=9)
    (tmp_path / "sample.txt").write_text("17")
    env = LocalEnvironment(str(tmp_path)) if borrow_env else None
    llm = LLMClient(model="offline", provider="openai", api_key=__name__) if borrow_llm else None
    agent = Agent(name="reader", system_prompt="Read the sample.", model="offline",
                  provider="openai", api_key=__name__)
    original_cleanup = LocalEnvironment.cleanup
    original_close = LLMClient.close
    cleaned, closed = [], []

    async def cleanup(environment):
        await original_cleanup(environment)
        cleaned.append(environment)

    async def close(client):
        release = store.try_autosave_lock(target)
        assert release is not None
        release()
        await original_close(client)
        closed.append(client)
        if close_fails:
            raise OSError("model close failed")

    monkeypatch.setattr(LocalEnvironment, "cleanup", cleanup)
    monkeypatch.setattr(LLMClient, "close", close)
    try:
        with pytest.raises(SnapshotSequenceConflict, match="refusing stale checkpoint sequence") as caught:
            load_session(source, agent, env=env, llm=llm, store=store, auto_save_path=target)
        assert len(cleaned) == int(not borrow_env)
        assert len(closed) == int(not borrow_llm)
        assert store.load_snapshot(target, agent.system_prompt)["messages"][-1]["content"] == "new request"
        if env is not None:
            assert await env.read_file("sample.txt") == "17"
        if close_fails:
            assert any("model close failed" in note for note in caught.value.__notes__)
    finally:
        if env is not None:
            await original_cleanup(env)
        if llm is not None:
            await original_close(llm)


@pytest.mark.parametrize("turns", [1, 4])
@pytest.mark.parametrize("entry", ["restore", "load_session"])
@pytest.mark.parametrize("save_twice", [False, True])
@pytest.mark.parametrize("store_kind", ["journal", "snapshot"])
async def test_manual_export_resumes_original_autosave_and_continues(
    tmp_path, turns, entry, save_twice, store_kind,
):
    automatic = str(tmp_path / "automatic.json")
    exported = str(tmp_path / "exported.json")
    agent = Agent(name="reviewer", system_prompt="Complete the user's requests.")
    store = SessionStore() if store_kind == "journal" else SnapshotOnlyStore()
    original = build_session(agent=agent, llm=model(turns), auto_save_path=automatic, store=store)
    try:
        if save_twice:
            original.save(exported)
        await converse(original, turns)
        original.save(exported)
        messages = original.messages.copy()
        tokens = original.used_tokens
    finally:
        await original.aclose()
        await original.env.cleanup()

    continuation = model()
    restored = None
    try:
        if entry == "load_session":
            restored = load_session(
                exported, agent=agent, llm=continuation, auto_save_path=automatic, store=store,
            )
        else:
            restored = build_session(
                agent=agent, llm=continuation, auto_save_path=automatic, store=store,
            )
            restored.restore(exported)
        assert restored.messages == messages
        assert restored.used_tokens == tokens
        await restored.add_user_message("Continue using the remembered requirements.")
        await restored.run_loop()
        assert continuation.calls[0]["messages"][:len(messages)] == messages
        saved = SessionStore().load_snapshot(automatic, agent.system_prompt)
        assert transcript(saved) == restored.messages
        assert saved["session_state"]["used_tokens"] == tokens + 7
        assert restored.persistence_errors == ()
    finally:
        if restored is not None:
            await restored.aclose()
            await restored.env.cleanup()

async def test_export_preserves_source_cursor_when_overwriting_an_old_autosave(tmp_path):
    automatic = str(tmp_path / "automatic.json")
    exported = str(tmp_path / "exported.json")
    agent = Agent(name="reviewer", system_prompt="Remember each request.")
    store = SessionStore()
    older_messages = [{"role": "user", "content": "Earlier unrelated draft."}]
    store.checkpoint_snapshot(exported, older_messages, meta={}, sequence=1)
    store.append_snapshot_delta(
        exported, sequence=2, replace_from=1, messages=[{"role": "assistant", "content": "Draft saved."}],
        meta={},
    )

    original = build_session(agent=agent, llm=model(4), auto_save_path=automatic, store=store)
    try:
        await converse(original, 4)
        source = store.load_snapshot(automatic, agent.system_prompt)
        assert source["_autosave_sequence"] > 3
        original.save(exported)
        assert store.load_snapshot(exported, agent.system_prompt) == source
    finally:
        await original.aclose()
        await original.env.cleanup()

    restored = load_session(exported, agent=agent, llm=model(), auto_save_path=automatic, store=store)
    try:
        assert restored.messages == transcript(source)
        await restored.add_user_message("Continue using the remembered requirements.")
        await restored.run_loop()
        resumed = store.load_snapshot(automatic, agent.system_prompt)
        assert resumed["_autosave_sequence"] > source["_autosave_sequence"]
        assert transcript(resumed) == restored.messages
        assert restored.persistence_errors == ()
    finally:
        await restored.aclose()
        await restored.env.cleanup()


@pytest.mark.parametrize("copy_mode", ["fresh", "reopened", "legacy", "read-answer"])
@pytest.mark.parametrize("source_advances", [False, True])
async def test_writable_restore_copy_reexports_the_original_source_order(
    tmp_path, local_environment_factory, copy_mode, source_advances,
):
    original_path, first_export, copy_path, backup_path = (
        str(tmp_path / f"{name}.json") for name in ("original", "first", "copy", "backup")
    )
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    store = SessionStore()
    options = {"agent": agent, "env": local_environment_factory(str(tmp_path)), "store": store}

    def inspect(path):
        return store.load_snapshot(path, agent.system_prompt)

    async with AsyncExitStack() as sessions:
        original = build_session(**options, llm=model(), auto_save_path=original_path)
        sessions.push_async_callback(original.aclose)
        await converse(original, 1)
        original.save(first_export)
        source = inspect(first_export)

        copied = load_session(first_export, **options, llm=model(), auto_save_path=copy_path)
        sessions.push_async_callback(copied.aclose)
        if copy_mode == "legacy":
            legacy = inspect(copy_path)
            del legacy["_autosave_export_source_sequence"]
            (tmp_path / "copy.json").write_text(json.dumps(legacy), encoding="utf-8")
            copied = load_session(first_export, **options, llm=model(), auto_save_path=copy_path)
            sessions.push_async_callback(copied.aclose)
        elif copy_mode == "reopened":
            copied = load_session(copy_path, **options, llm=model(), auto_save_path=copy_path)
            sessions.push_async_callback(copied.aclose)
        elif copy_mode == "read-answer":
            assert await copied.run_loop() == "Completed request 0."
            copied = load_session(copy_path, **options, llm=model(), auto_save_path=copy_path)
            sessions.push_async_callback(copied.aclose)
        copied.save(backup_path)
        assert inspect(backup_path)["_autosave_sequence"] == source["_autosave_sequence"]

        if source_advances:
            await original.add_user_message("Remember the new requirement.")
            while original.pending_cleanup_tasks:
                await asyncio.gather(*original.pending_cleanup_tasks)
            newer = inspect(original_path)
            assert newer["_autosave_sequence"] == source["_autosave_sequence"] + 1
            assert newer["messages"][-1]["content"] == "Remember the new requirement."
            with pytest.raises(SnapshotSequenceConflict):
                load_session(backup_path, **options, llm=model(), auto_save_path=original_path)
            assert inspect(original_path) == newer
        else:
            restored = load_session(backup_path, **options, llm=model(), auto_save_path=original_path)
            sessions.push_async_callback(restored.aclose)
            assert restored.messages == transcript(source)
            assert restored.used_tokens == 7


async def test_writable_restore_copy_can_publish_new_work(tmp_path, local_environment_factory):
    original_path, first_export, copy_path, backup_path = (
        str(tmp_path / f"{name}.json") for name in ("original", "first", "copy", "backup")
    )
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    store = SessionStore()
    options = {"agent": agent, "env": local_environment_factory(str(tmp_path)), "store": store}
    async with AsyncExitStack() as sessions:
        original = build_session(**options, llm=model(), auto_save_path=original_path)
        sessions.push_async_callback(original.aclose)
        await converse(original, 1)
        original.save(first_export)

        copied = load_session(first_export, **options, llm=model(), auto_save_path=copy_path)
        sessions.push_async_callback(copied.aclose)
        await copied.add_user_message("Remember what changed on the copy.")
        await copied.run_loop()
        copied.save(backup_path)

        restored = load_session(backup_path, **options, llm=model(), auto_save_path=original_path)
        sessions.push_async_callback(restored.aclose)
        assert restored.messages == copied.messages
        assert restored.used_tokens == 14


async def test_export_keeps_original_autosave_bytes_unchanged(tmp_path):
    automatic = tmp_path / "automatic.json"
    exported = tmp_path / "exported.json"
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    original = build_session(agent=agent, llm=model(4), auto_save_path=str(automatic))
    try:
        await converse(original, 4)
        source_paths = [automatic, tmp_path / "automatic.json.journal"]
        before = {path: path.read_bytes() for path in source_paths}
        original.save(str(exported))
        assert {path: path.read_bytes() for path in source_paths} == before
        store = SessionStore()
        source = store.load_snapshot(str(automatic), agent.system_prompt)
        saved = store.load_snapshot(str(exported), agent.system_prompt)
        assert saved["messages"] == source["messages"]
        assert saved["session_state"] == source["session_state"]
    finally:
        await original.aclose()
        await original.env.cleanup()

@pytest.mark.parametrize("copy_mode", ["direct", "read-only", "writable"])
@pytest.mark.parametrize("source_advances", [False, True])
async def test_reusing_export_path_does_not_promote_source_history(
    tmp_path, local_environment_factory, copy_mode, source_advances,
):
    source, backup, copy_path = (tmp_path / name for name in ("source.json", "backup.json", "copy.json"))
    store = SessionStore()
    store.checkpoint_snapshot(
        str(backup), [{"role": "user", "content": "Previous task."}], meta={}, sequence=20,
    )
    store.append_snapshot_delta(
        str(backup), sequence=21, replace_from=1,
        messages=[{"role": "assistant", "content": "Saved the previous task."}], meta={},
    )
    agent = Agent(name="reviewer", system_prompt="Remember the original requirements.")
    options = {"agent": agent, "store": store, "env": local_environment_factory(str(tmp_path))}
    original = build_session(**options, llm=model(3), auto_save_path=str(source))
    reader = restored = None
    try:
        await converse(original, 1)
        first = store.load_snapshot(str(source), agent.system_prompt)
        original.save(str(backup))
        exported = store.load_snapshot(str(backup), agent.system_prompt)
        assert exported["_autosave_sequence"] > first["_autosave_sequence"]
        if copy_mode != "direct":
            reader = load_session(
                str(backup), **options, llm=model(),
                auto_save_path=str(backup) if copy_mode == "writable" else None,
            )
            if copy_mode == "writable":
                reader.save(str(backup))
                assert store.load_snapshot(str(backup), agent.system_prompt)["_autosave_sequence"] > (
                    exported["_autosave_sequence"]
                )
            reader.save(str(copy_path))
            backup = copy_path
        if source_advances:
            await converse(original, 2)
            before = store.load_snapshot(str(source), agent.system_prompt)
            files = [source, source.with_suffix(".json.journal")]
            original_bytes = [path.read_bytes() for path in files]
            with pytest.raises(SnapshotSequenceConflict, match="refusing stale checkpoint"):
                load_session(str(backup), **options, llm=model(), auto_save_path=str(source))
            assert store.load_snapshot(str(source), agent.system_prompt) == before
            assert [path.read_bytes() for path in files] == original_bytes
        else:
            restored = load_session(str(backup), **options, llm=model(), auto_save_path=str(source))
            resumed = store.load_snapshot(str(source), agent.system_prompt)
            assert resumed["_autosave_sequence"] == first["_autosave_sequence"] + 1
            assert resumed["messages"] == first["messages"]
            await converse(restored, 1)
            assert store.load_snapshot(str(source), agent.system_prompt)["session_state"]["used_tokens"] == 14
    finally:
        for session in (original, reader, restored):
            if session is not None:
                await session.aclose()


@pytest.mark.parametrize("target_update", ["unchanged", "checkpoint", "accounting", "concurrent"])
async def test_reexport_restores_its_immediate_autosave_only_if_equivalent(
    tmp_path, local_environment_factory, monkeypatch, target_update,
):
    original_path, working_path, backup_path = (
        str(tmp_path / name) for name in ("original.json", "working.json", "backup.json")
    )
    agent = Agent(name="reviewer", system_prompt="Remember the current draft.")
    store = SessionStore()
    options = {"agent": agent, "store": store, "env": local_environment_factory(str(tmp_path))}

    def inspect(path):
        return store.load_snapshot(path, agent.system_prompt)

    async with AsyncExitStack() as sessions:
        for path, message in ((original_path, "Current draft."), (working_path, "Earlier draft.")):
            session = build_session(**options, llm=model(), auto_save_path=path)
            sessions.push_async_callback(session.aclose)
            await session.add_user_message(message)
            await session.run_loop()
            await session.aclose()
        original = inspect(original_path)
        exporter = load_session(original_path, **options, llm=model(), auto_save_path=original_path)
        sessions.push_async_callback(exporter.aclose)
        exporter.save(working_path)
        working = load_session(working_path, **options, llm=model(), auto_save_path=working_path)
        sessions.push_async_callback(working.aclose)
        if target_update in {"checkpoint", "concurrent"}:
            working.save(working_path)
        working.save(backup_path)

        if target_update == "accounting":
            working.used_tokens += 9
            working.save(working_path)
        current = inspect(working_path)
        if target_update == "concurrent":
            checkpoint = store.checkpoint_snapshot
            peer = SessionStore()
            local_sequence = current["_autosave_sequence"]

            def publish_before_retry(path, messages, *, meta, sequence):
                if sequence == local_sequence:
                    peer_meta = {**meta, "session_state": {**meta["session_state"], "used_tokens": 31}}
                    peer.checkpoint_snapshot(path, messages, meta=peer_meta, sequence=local_sequence + 1)
                return checkpoint(path, messages, meta=meta, sequence=sequence)

            monkeypatch.setattr(store, "checkpoint_snapshot", publish_before_retry)

        continuation = model()
        if target_update in {"accounting", "concurrent"}:
            with pytest.raises(SnapshotSequenceConflict):
                load_session(backup_path, **options, llm=continuation, auto_save_path=working_path)
            expected = copy.deepcopy(current)
            if target_update == "concurrent":
                expected["_autosave_sequence"] += 1
                expected["session_state"]["used_tokens"] = 31
            assert inspect(working_path) == expected
            assert continuation.calls == []
            return

        restored = load_session(backup_path, **options, llm=continuation, auto_save_path=working_path)
        sessions.push_async_callback(restored.aclose)
        assert inspect(working_path) == current
        assert restored.messages == transcript(original)
        assert restored.used_tokens == 7
        restored.save(backup_path)
        restored_original = load_session(backup_path, **options, llm=model(), auto_save_path=original_path)
        sessions.push_async_callback(restored_original.aclose)
        assert inspect(original_path)["_autosave_sequence"] == original["_autosave_sequence"] + 1
        await restored.add_user_message("Continue the review.")
        await restored.run_loop()
        assert continuation.calls[0]["messages"][:len(original["messages"])] == transcript(original)
        assert inspect(working_path)["session_state"]["used_tokens"] == 14


async def test_older_export_still_cannot_replace_a_newer_autosave(tmp_path):
    automatic = tmp_path / "automatic.json"
    exported = tmp_path / "older.json"
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    original = build_session(agent=agent, llm=model(5), auto_save_path=str(automatic))
    try:
        await converse(original, 1)
        original.save(str(exported))
        await converse(original, 4)
    finally:
        await original.aclose()
        await original.env.cleanup()
    source_paths = [automatic, tmp_path / "automatic.json.journal"]
    before = {path: path.read_bytes() for path in source_paths}
    restored = build_session(agent=agent, llm=model(), auto_save_path=str(automatic))
    try:
        with pytest.raises(ValueError, match="refusing stale checkpoint"):
            restored.restore(str(exported))
        assert {path: path.read_bytes() for path in source_paths} == before
    finally:
        await restored.aclose()
        await restored.env.cleanup()

async def test_restored_export_can_be_exported_again_before_resuming(tmp_path):
    automatic = str(tmp_path / "automatic.json")
    first = str(tmp_path / "first.json")
    second = str(tmp_path / "second.json")
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    original = build_session(agent=agent, llm=model(4), auto_save_path=automatic)
    try:
        await converse(original, 4)
        original.save(first)
        messages = original.messages.copy()
    finally:
        await original.aclose()
        await original.env.cleanup()
    reader = load_session(first, agent=agent, llm=model())
    try:
        reader.save(second)
    finally:
        await reader.aclose()
        await reader.env.cleanup()
    restored = build_session(agent=agent, llm=model(), auto_save_path=automatic)
    try:
        restored.restore(second)
        assert restored.messages == messages
    finally:
        await restored.aclose()
        await restored.env.cleanup()

@pytest.mark.parametrize("target_kind", ["same-file", "new-target"])
async def test_export_without_original_autosave_can_start_autosaving(tmp_path, target_kind):
    exported = str(tmp_path / "exported.json")
    target = exported if target_kind == "same-file" else str(tmp_path / "new.json")
    agent = Agent(name="reviewer", system_prompt="Remember the requests.")
    original = build_session(agent=agent, llm=model(2))
    try:
        await converse(original, 2)
        original.save(exported)
        messages = original.messages.copy()
    finally:
        await original.aclose()
        await original.env.cleanup()
    restored = load_session(exported, agent=agent, llm=model(), auto_save_path=target)
    try:
        assert restored.messages == messages
        await restored.add_user_message("Continue.")
        await restored.run_loop()
        assert transcript(SessionStore().load_snapshot(target, agent.system_prompt)) == restored.messages
    finally:
        await restored.aclose()
        await restored.env.cleanup()

async def test_old_anthropic_export_can_resume_an_existing_autosave_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    install_sdk_transport(monkeypatch, "anthropic", completion_http_response)
    exported = tmp_path / "exported.json"
    target = tmp_path / "automatic.json"
    agent = Agent(name="reviewer", system_prompt="Remember the prior conversation.", model="claude-sonnet-5-5",
                  provider="anthropic", thinking=False, max_tokens_per_step=1024)
    environment = LocalEnvironment(str(tmp_path))
    try:
        async with LLMClient(model=agent.model, provider="anthropic", max_retries=0,
                             api_key=__name__, base_url="https://example.invalid/v1") as client:
            options = {"agent": agent, "llm": client, "env": environment}
            original = build_session(**options)
            try:
                await original.add_user_message("Remember the review deadline.")
                assert await original.run_loop() == "ok"
                original.save(str(exported))
                messages = original.messages.copy()
            finally:
                await original.aclose()

            old_export = json.loads(exported.read_text())
            history = old_export["session_state"]["anthropic_history"]
            assert any(type(row) is int for row in history["messages"])
            current_inspection = OpenCollab.read_session_snapshot(exported)
            assert current_inspection["session_state"]["anthropic_history"]["messages"] == [
                messages[row] if type(row) is int else row for row in history["messages"]
            ]
            current_inspection["messages"][1]["content"] = "Changing the inspection data."
            assert current_inspection["session_state"]["anthropic_history"]["messages"][1]["content"] == (
                "Remember the review deadline."
            )
            assert json.loads(exported.read_text()) == old_export

            history["messages"] = [messages[row] if type(row) is int else row for row in history["messages"]]
            exported.write_text(json.dumps(old_export))
            assert OpenCollab.read_session_snapshot(exported)["session_state"]["anthropic_history"] == history
            first = load_session(str(exported), **options, auto_save_path=str(target))
            await first.aclose()

            old_target = json.loads(target.read_text())
            assert old_target["_autosave_sequence"] == 1
            assert "timestamp" in old_target["messages"][1]
            old_target["session_state"]["anthropic_history"] = history
            target.write_text(json.dumps(old_target))

            resumed = load_session(str(exported), **options, auto_save_path=str(target))
            try:
                assert resumed.messages == messages
                assert resumed.state.anthropic_history["messages"] == history["messages"]
                assert json.loads(target.read_text())["_autosave_sequence"] == 1
            finally:
                await resumed.aclose()
    finally:
        await environment.cleanup()


@pytest.mark.parametrize("conflict", [None, "source-timestamp", "recovered-content"])
async def test_interrupted_tool_export_retries_the_same_autosave(tmp_path, monkeypatch, conflict):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    exported, target = tmp_path / "interrupted.json", tmp_path / "automatic.json"
    (tmp_path / "archive.txt").write_text("The archive opened on April 18.")
    model = "claude-sonnet-5-5"
    agent = Agent(name="reviewer", system_prompt="Read the archive.", model=model,
                  provider="anthropic", tools=builtin_tools("file_read", headless=False))
    env = LocalEnvironment(str(tmp_path))
    requests = []

    def peer(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1:
            return completion_http_response(request, text="Done")
        return http_response(request, 200, json={
            "id": "msg_read", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "tool_use", "id": "read-archive", "name": "file_read",
                         "input": {"path": "archive.txt"}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        })

    async def export_at_tool_start(event):
        if event.type == "tool_start":
            original.save(str(exported))

    install_sdk_transport(monkeypatch, "anthropic", peer)
    try:
        async with LLMClient(
            model=model, provider="anthropic", max_retries=0,
            api_key="test-placeholder", base_url="https://provider.invalid/v1",  # pragma: allowlist secret
        ) as client:
            options = {"agent": agent, "llm": client, "env": env}
            original = build_session(**options, event_sink=export_at_tool_start)
            try:
                await original.add_user_message("Look up the date.")
                assert await original.run_loop() == "Done"
            finally:
                await original.aclose()

            first_stamp = "2026-01-01T00:00:00+00:00"
            monkeypatch.setattr(session_module, "_now_iso", lambda: first_stamp)
            first = load_session(str(exported), **options, auto_save_path=str(target))
            await first.aclose()
            stored = json.loads(target.read_text())
            assert stored["_autosave_sequence"] == 1
            assert stored["messages"][-1]["timestamp"] == first_stamp
            assert stored["messages"][-1]["content"] == "Tool execution interrupted by session restore."

            if conflict == "source-timestamp":
                stored["messages"][1]["timestamp"] = "2026-01-01T00:00:02+00:00"
            elif conflict == "recovered-content":
                stored["messages"][-1]["content"] = "A different result was confirmed."
            target.write_text(json.dumps(stored))
            monkeypatch.setattr(session_module, "_now_iso", lambda: "2026-01-01T00:00:01+00:00")

            if conflict:
                with pytest.raises(SnapshotSequenceConflict, match="conflicts with persisted snapshot state"):
                    load_session(str(exported), **options, auto_save_path=str(target))
            else:
                resumed = load_session(str(exported), **options, auto_save_path=str(target))
                try:
                    assert resumed.state.enriched_messages() == stored["messages"]
                    assert resumed.state.anthropic_history is not None
                finally:
                    await resumed.aclose()
            assert json.loads(target.read_text()) == stored
            assert len(requests) == 2
    finally:
        await env.cleanup()


class SnapshotOnlyStore:
    """Exercise full-snapshot persistence without journal support."""

    def __init__(self):
        self.delegate = SessionStore()

    def save(self, path, messages, *, meta=None):
        self.delegate.save(path, messages, meta=meta)

    def load_messages(self, path, system_prompt):
        return self.delegate.load_messages(path, system_prompt)

    def load_snapshot(self, path, system_prompt):
        return self.delegate.load_snapshot(path, system_prompt)

    def save_manifest(self, path, manifest):
        self.delegate.save_manifest(path, manifest)

class CurrentColorModel(FakeLLM):
    async def complete(self, messages, **kwargs):
        requests = [row["content"] for row in messages if row["role"] == "user"]
        if requests[-1] == "Write the current color." and messages[-1]["role"] != "tool":
            color = [text.split("=", 1)[1] for text in requests if text.startswith("color=")][-1]
            response = llm_response(tool_calls=[tool_call(
                name="file_write", arguments=json.dumps({
                    "path": "chosen-color.txt", "content": color, "mode": "create",
                }),
            )], finish_reason="tool_calls")
        else:
            response = llm_response(content="Remembered.")
        self.responses.append(response)
        return await super().complete(messages, **kwargs)

def autosave_tracking(session):
    return copy.deepcopy({name: getattr(session, name) for name in (
        "_auto_save_sequence", "_auto_save_reserved_sequence", "_auto_save_message_count", "_auto_save_messages",
        "_auto_save_rewrite_from", "_auto_save_rewrite_revision", "_auto_save_pending_checkpoints",
        "_auto_save_seen_result_hashes", "_next_auto_save_checkpoint", "_loop_checkpoint_results",
        "_loop_checkpoint_prefix", "_loop_checkpoint_step",
    )})

@pytest.mark.parametrize("snapshot_kind", ["current", "legacy"])
@pytest.mark.parametrize("failure", ["stale", "journal-write", "snapshot-write"])
async def test_rejected_restore_preserves_live_conversation_and_continues(
    tmp_path, monkeypatch, snapshot_kind, failure,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    automatic = tmp_path / "automatic.json"
    exported = tmp_path / "older.json"
    store = SnapshotOnlyStore() if failure == "snapshot-write" else SessionStore()
    llm = CurrentColorModel()
    session = build_session(
        agent=Agent(name="writer", system_prompt="Use current requirements.",
                    tools=builtin_tools("file_write", headless=False)),
        llm=llm, env=LocalEnvironment(str(tmp_path)),
        store=store, auto_save_path=str(automatic),
    )

    async def turn(text):
        await session.add_user_message(text)
        await session.run_loop()

    try:
        await turn("color=blue")
        if snapshot_kind == "legacy":
            exported.write_text(json.dumps(session.state.enriched_messages()), encoding="utf-8")
        else:
            session.save(str(exported))
        await turn("color=green")
        await turn("Remember the current color.")
        state = session.state
        before_state = copy.deepcopy({**state.__dict__, "pending_events": state.pending_events.rows})
        pending_events = state.pending_events
        before_tracking = autosave_tracking(session)
        turn_start = session.runner._turn_start_message_index
        paths = [automatic, exported]
        journal = tmp_path / "automatic.json.journal"
        if journal.exists():
            paths.append(journal)
        before_files = {path: path.read_bytes() for path in paths}

        def fail_write(*args, **kwargs):
            raise OSError("restore target unavailable")

        with monkeypatch.context() as patch:
            if failure == "stale":
                error, message = ValueError, "refusing stale checkpoint"
            else:
                method = "save" if failure == "snapshot-write" else "checkpoint_snapshot"
                patch.setattr(store, method, fail_write)
                error, message = OSError, "restore target unavailable"
            with pytest.raises(error, match=message):
                session.restore(str(exported))

        assert session.state is state
        assert session.runner.state is state
        assert state.pending_events is pending_events
        assert {**state.__dict__, "pending_events": state.pending_events.rows} == before_state
        assert session.runner._turn_start_message_index == turn_start
        assert autosave_tracking(session) == before_tracking
        assert {path: path.read_bytes() for path in paths} == before_files
        await turn("Write the current color.")
        await turn("Acknowledge once.")
        await turn("Acknowledge twice.")
        assert (tmp_path / "chosen-color.txt").read_text(encoding="utf-8") == "green"
        saved = store.load_snapshot(str(automatic), session.agent.system_prompt)
        assert transcript(saved) == session.messages
        assert {"role": "user", "content": "color=green"} in session.messages
        assert session.persistence_errors == ()
        assert exported.read_bytes() == before_files[exported]
    finally:
        await session.aclose()
        await session.env.cleanup()
