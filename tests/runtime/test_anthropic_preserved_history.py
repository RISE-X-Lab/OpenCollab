"""Native SDK sessions retain valid thinking and retire edited-prefix blocks."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil

import pytest

from opencollab import OpenCollab
from opencollab.adapters import storage as storage_module
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool
from opencollab.application._anthropic_history import _anthropic_history_input, _remember_anthropic_history
from opencollab.application.session_snapshot import _restore_anthropic_history
from opencollab.application.shaping import AutoCompactShaper, PerToolResultBudgetShaper, ToolOutputClearShaper
from opencollab.application.steering import READS_NUDGE_HARD
from opencollab.bootstrap import build_session, load_session
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState
from tests.runtime.test_manual_autosave_ordering import PausedAppendStore
from tests.support.provider_sdk_http import http_response, install_sdk_transport

_THINKING = {"thinking", "redacted_thinking"}

def _blocks(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else content

def _plain_messages(messages):
    result = []
    for message in messages:
        blocks = _blocks(message["content"])
        kept = [block for block in blocks if block["type"] not in _THINKING]
        if not kept:
            continue
        if result and result[-1]["role"] == message["role"]:
            result[-1]["content"].extend(copy.deepcopy(kept))
        else:
            result.append({"role": message["role"], "content": copy.deepcopy(kept)})
    return result

def _prefix(body, messages):
    return {"system": body.get("system"), "tools": body.get("tools"), "messages": _plain_messages(messages)}

def _signatures(body):
    return [block.get("signature") or block.get("data") for message in body["messages"]
            for block in _blocks(message["content"]) if block["type"] in _THINKING]

class _BoundThinkingProvider:
    """Compare the documented prefixes through the real Anthropic SDK wire."""

    def __init__(self, reads=2, *, thinking=True, correction=False):
        self.reads = reads
        self.thinking = thinking
        self.correction = correction
        self.requests = []
        self.origins = {}
        self.retired = set()

    def __call__(self, request):
        body = json.loads(request.content)
        signatures = _signatures(body)
        assert not self.retired.intersection(signatures)
        previous = None
        preceding = []
        for message in body["messages"]:
            blocks = []
            for block in _blocks(message["content"]):
                if block["type"] in _THINKING:
                    key = block.get("signature") or block.get("data")
                    origin = self.origins[key]
                    prefix = preceding + ([{"role": message["role"], "content": blocks}] if blocks else [])
                    assert block == origin["block"]
                    assert _prefix(body, prefix) == origin["prefix"]
                    if previous is not None:
                        assert origin["previous"] == previous
                    previous = key
                blocks.append(block)
            preceding.append(message)
        self.retired.update(set(self.origins) - set(signatures))
        self.requests.append(body)
        number = len(self.requests)
        if self.correction and body.get("tool_choice", {}).get("type") == "tool":
            return http_response(request, 400, json={"type": "error", "error": {
                "type": "invalid_request_error",
                "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
            }})
        content = []
        if self.thinking:
            content = [
                {"type": "thinking", "thinking": "", "signature": f"signed-{number}"},
                {"type": "redacted_thinking", "data": f"redacted-{number}"},
            ]
        if number <= self.reads:
            content.append({"type": "tool_use", "id": f"read-{number}", "name": "file_read",
                            "input": {"path": "source.txt"}})
        elif self.correction and [tool["name"] for tool in body.get("tools", [])] == ["structured_output"]:
            content.append({"type": "tool_use", "id": "capture", "name": "structured_output", "input": {"answer": 42}})
        else:
            content.append({"type": "text", "text": "done"})
        for index, block in enumerate(content):
            if block["type"] in _THINKING:
                key = block.get("signature") or block.get("data")
                prefix = body["messages"] + ([{"role": "assistant", "content": content[:index]}] if index else [])
                self.origins[key] = {"block": copy.deepcopy(block), "prefix": _prefix(body, prefix),
                                     "previous": previous}
                previous = key
        return http_response(request, 200, json={
            "id": f"message-{number}", "type": "message", "role": "assistant", "model": body["model"],
            "content": content, "stop_reason": "tool_use" if content[-1]["type"] == "tool_use" else "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 40},
        })

def _agent(*, thinking=True, model="claude-opus-5-5"):
    return Agent(name="reader", system_prompt="Read files and answer.", tools=[FileReadTool()],
                 model=model, provider="anthropic", thinking=thinking,
                 thinking_params={"thinking": {"type": "adaptive"}}, max_tokens_per_step=1024)

def _client(model="claude-opus-5-5"):
    return LLMClient(model=model, provider="anthropic",
                     api_key="controlled-test",  # pragma: allowlist secret
                     base_url="https://controlled.invalid/v1", max_retries=0)

@pytest.fixture(autouse=True)
def _local_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "source.txt").write_text("file evidence\n" * 100)

@pytest.mark.parametrize("restore", [False, True], ids=["same-session", "restore"])
@pytest.mark.parametrize("thinking", [False, True], ids=["plain", "thinking"])
async def test_native_steering_prefix_survives_tool_rounds_and_new_user_turns(monkeypatch, tmp_path, restore, thinking):
    model = "claude-opus-5-5" if thinking else "claude-sonnet-5-5"
    peer = _BoundThinkingProvider(thinking=thinking)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    async with _client(model) as client:
        session = build_session(agent=_agent(thinking=thinking, model=model), llm=client,
                                env=LocalEnvironment(str(tmp_path)),
                                max_budget_tokens=100_000, max_steps=20)
        try:
            await session.add_user_message("Read twice and answer")
            assert await session.run_loop() == "done"
            original = copy.deepcopy(session.messages)
            session.save(str(tmp_path / "session.json"))
            if restore:
                await session.aclose()
                await session.env.cleanup()
                session = load_session(str(tmp_path / "session.json"),
                                       agent=_agent(thinking=thinking, model=model), llm=client,
                                       env=LocalEnvironment(str(tmp_path)), max_budget_tokens=100_000, max_steps=20)
                assert session.messages == original
            await session.add_user_message("Continue with the same evidence")
            assert await session.run_loop() == "done"
        finally:
            await session.aclose()
            await session.env.cleanup()
    assert len(peer.requests) == 4
    assert all(request["thinking"] == {"type": "adaptive" if thinking else "between_tools"}
               for request in peer.requests)
    assert peer.retired == set()
    if thinking:
        assert _signatures(peer.requests[2]) == ["signed-1", "redacted-1", "signed-2", "redacted-2"]

@pytest.mark.parametrize("mode", ["clear", "summary", "first-trim"])
@pytest.mark.parametrize("snapshot_format", ["current", "legacy", "source-copy"])
async def test_compacted_native_view_survives_restore_and_disabling_the_shaper(
    monkeypatch, tmp_path, mode, snapshot_format,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=4)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    if mode == "clear":
        shaper = ToolOutputClearShaper(trigger_tokens=2, target_tokens=1, keep_recent=1)
    elif mode == "summary":
        shaper = AutoCompactShaper(trigger_tokens=2, target_tokens=1, keep_recent_groups=0,
                                  summarizer=lambda _: "Earlier work")
    else:
        shaper = PerToolResultBudgetShaper(max_chars=200)
    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, env=LocalEnvironment(str(tmp_path)), shaper=shaper,
                                max_budget_tokens=100_000, max_steps=20)
        try:
            await session.add_user_message("Inspect four files")
            assert await session.run_loop() == "done"
            raw = copy.deepcopy(session.messages)
            assert len([message for message in raw if message["role"] == "tool"]) == 4
            assert all("file evidence" in message["content"] for message in raw if message["role"] == "tool")
            before = copy.deepcopy(session.state.anthropic_history)
            path = tmp_path / "session.json"
            session.save(str(path))
            saved = json.loads(path.read_text())
            native_view = saved["session_state"]["anthropic_history"]
            assert "source_messages" not in native_view
            assert native_view["messages"][0] == 0
            if snapshot_format != "current":
                legacy = dict(before)
                if snapshot_format == "legacy":
                    del legacy["source_messages"]
                saved["session_state"]["anthropic_history"] = legacy
                path.write_text(json.dumps(saved))
            await session.aclose()
            await session.env.cleanup()
            session = load_session(str(path), agent=_agent(), llm=client,
                                   env=LocalEnvironment(str(tmp_path)), max_budget_tokens=100_000, max_steps=20)
            assert session.state.anthropic_history == before
            assert session.messages == raw
            session.runner.shaper = None
            await session.add_user_message("Continue using the saved context")
            assert await session.run_loop() == "done"
        finally:
            await session.aclose()
            await session.env.cleanup()
    assert len(peer.requests) == 6
    if mode == "first-trim":
        assert peer.retired == set()
        assert "signed-1" in _signatures(peer.requests[-1])
    else:
        assert peer.retired
    assert _signatures(peer.requests[-1])[-2:] == ["signed-5", "redacted-5"]

async def test_appended_system_and_changed_tools_retire_old_native_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, env=LocalEnvironment(str(tmp_path)),
                                max_budget_tokens=100_000, max_steps=20)
        try:
            await session.add_user_message("Answer")
            assert await session.run_loop() == "done"
            session.state.append_message({"role": "system", "content": "A new system instruction"})
            await session.add_user_message("Continue")
            assert await session.run_loop() == "done"
            assert _signatures(peer.requests[-1]) == []
            await session.add_user_message("Finish")
            await session.runner.call_llm(None)
            assert _signatures(peer.requests[-1]) == []
            assert peer.requests[-1].get("tools") is None
        finally:
            await session.aclose()
            await session.env.cleanup()

async def test_chronological_compaction_marker_retains_earlier_valid_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)

    class SummarizeLastUser:
        def shape(self, messages):
            return [*messages[:-1], {"role": "system", "compacted": True, "content": "Earlier requirements"}]

    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, max_budget_tokens=100_000)
        try:
            await session.add_user_message("Answer the first question")
            assert await session.run_loop() == "done"
            session.runner.shaper = SummarizeLastUser()
            await session.add_user_message("The next lengthy requirements " * 200)
            assert await session.run_loop() == "done"
            assert _signatures(peer.requests[-1]) == ["signed-1", "redacted-1"]
            assert peer.requests[-1]["system"] == peer.requests[0]["system"]
            session.runner.shaper = None
            await session.add_user_message("Continue")
            assert await session.run_loop() == "done"
            assert _signatures(peer.requests[-1]) == ["signed-1", "redacted-1", "signed-2", "redacted-2"]
        finally:
            await session.aclose()
            await session.env.cleanup()

async def test_hard_steering_restricts_tools_and_removes_old_prefix_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "on")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    agent = _agent()
    agent.tools.append(FileWriteTool())
    async with _client() as client:
        session = build_session(agent=agent, llm=client, env=LocalEnvironment(str(tmp_path)),
                                max_budget_tokens=100_000, max_steps=20)
        try:
            await session.add_user_message("Answer")
            assert await session.run_loop() == "done"
            await session.add_user_message("Make the requested edit")
            session.state.turn.reads_since_last_edit = READS_NUDGE_HARD
            await session.runner.call_llm(session.runner.build_tool_schemas())
            assert [tool["name"] for tool in peer.requests[-1]["tools"]] == ["file_write"]
            assert _signatures(peer.requests[-1]) == []
            assert session.runner._pending_tool_allowlist == frozenset({"file_write", "apply_patch"})
            assert any(message.get("provider_state") for message in session.messages)
        finally:
            await session.aclose()
            await session.env.cleanup()

async def test_auxiliary_summary_omits_bound_thinking_and_preserves_the_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    async with _client("claude-fable-5-1") as client:
        session = build_session(agent=_agent(model="claude-fable-5-1"), llm=client, max_budget_tokens=100_000)
        try:
            await session.add_user_message("Retain this evidence")
            assert await session.run_loop() == "done"
            original = copy.deepcopy(session.messages)
            await session._context_summarizer.asummarize(session.messages[1:])
            assert session.messages == original
        finally:
            await session.aclose()
            await session.env.cleanup()
    assert _signatures(peer.requests[-1]) == []
    assert "Retain this evidence" in json.dumps(peer.requests[-1])
    assert peer.requests[-1].get("system") is None
    assert peer.requests[-1].get("tools") is None

async def test_structured_correction_keeps_evidence_and_capture_only_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(correction=True)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    app = OpenCollab(tmp_path, model="claude-fable-5-1", provider="anthropic",
                     api_key="controlled-test", base_url="https://controlled.invalid/v1",  # pragma: allowlist secret
                     config={"thinking": True, "thinking_params": {"thinking": {"type": "adaptive"}},
                             "max_output_tokens": 4096, "llm_max_retries": 0})

    async def flow(ctx, _args):
        return await ctx.agent("Read twice and return the sum", tools=[FileReadTool()], budget=80_000,
                               schema={"type": "object", "properties": {"answer": {"type": "integer"}},
                                       "required": ["answer"]})

    result = await app.workflow(flow, budget=100_000, max_steps=20, timeout=20, concurrency=1,
                                trace=False, artifacts=tmp_path / "artifacts")
    assert result.ok
    assert result.output == {"answer": 42}
    assert len(peer.requests) == 5
    assert {tool["name"] for tool in peer.requests[2]["tools"]} == {"file_read", "structured_output"}
    for body in peer.requests[3:]:
        assert [tool["name"] for tool in body["tools"]] == ["structured_output"]
        assert _signatures(body) == []
        assert "file evidence" in json.dumps(body)
    assert peer.requests[-1]["tool_choice"] == {"type": "auto"}

@pytest.mark.parametrize("model,provider,expected", [
    ("claude-fable-5-1", "anthropic", True),
    ("cloud/claude-opus-5-5-20261001", " Anthropic ", True),
    ("claude-sonnet-5-5", "anthropic", True),
    ("claude-haiku-5-5", "anthropic", True),
    ("claude-sonnet-4-6", "anthropic", False),
    ("claude-fable-5", "anthropic", False),
    ("claude-mythos-5-1", "anthropic", False),
    ("deepseek-v4-pro", "anthropic", False),
    ("claude-opus-5-5", "openai", False),
])
async def test_prefix_constraint_uses_native_transport_and_documented_models(model, provider, expected):
    async with LLMClient(model=model, provider=provider, api_key="controlled-test",  # pragma: allowlist secret
                         base_url="https://controlled.invalid/v1") as client:
        assert client.requires_thinking_prefix is expected


@pytest.mark.parametrize("moment", ["live", "before-save", "before-autosave", "after-load"])
@pytest.mark.parametrize("edit", ["text", "nested", "replacement", "deletion", "tail-deletion", "system"])
async def test_source_edits_reach_native_requests_and_retire_only_invalid_thinking(
    monkeypatch, tmp_path, moment, edit,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    path = tmp_path / "session.json"

    def apply_edit(session):
        if edit == "text":
            session.messages[3]["content"] = "Choose green"
        elif edit == "nested":
            session.messages[3]["content"][0]["text"] = "Choose green"
        elif edit == "replacement":
            session.messages[3] = {"role": "user", "content": "Choose green"}
        elif edit == "deletion":
            del session.messages[3]
        elif edit == "tail-deletion":
            del session.messages[3:]
        else:
            session.messages[0]["content"] = "Follow the revised instructions"

    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, env=LocalEnvironment(str(tmp_path)),
                                max_budget_tokens=100_000, max_steps=20,
                                auto_save_path=str(path) if moment == "before-autosave" else None)
        try:
            await session.add_user_message("Answer the first question")
            assert await session.run_loop() == "done"
            await session.add_user_message("Choose red")
            session.messages[-1]["content"] = [{"type": "text", "text": "Choose red"}]
            assert await session.run_loop() == "done"
            if moment == "before-autosave":
                session.save(str(path))
            if moment != "after-load":
                apply_edit(session)
            if moment != "live":
                if moment == "before-autosave":
                    edited = copy.deepcopy(session.messages)
                    await session.enqueue_auto_save()
                    assert not session.persistence_errors
                else:
                    session.save(str(path))
                await session.aclose()
                await session.env.cleanup()
                session = load_session(str(path), agent=_agent(), llm=client,
                                       env=LocalEnvironment(str(tmp_path)), max_budget_tokens=100_000, max_steps=20)
                if moment == "before-autosave":
                    assert session.messages == edited
            if moment == "after-load":
                apply_edit(session)
            edited = copy.deepcopy(session.messages)
            await session.add_user_message("Continue using the current instructions")
            assert await session.run_loop() == "done"
            assert session.messages[:len(edited)] == edited
            await session.add_user_message("Continue once more")
            assert await session.run_loop() == "done"
        finally:
            await session.aclose()
            await session.env.cleanup()

    assert len(peer.requests) == 4
    body = peer.requests[2]
    if edit == "system":
        assert body["system"] == "Follow the revised instructions"
        assert _signatures(body) == []
    else:
        assert _signatures(body) == ["signed-1", "redacted-1"]
        assert "Choose red" not in json.dumps(body)
        if edit not in {"deletion", "tail-deletion"}:
            assert "Choose green" in json.dumps(body)
    assert _signatures(peer.requests[-1]) == [*_signatures(body), "signed-3", "redacted-3"]


@pytest.mark.parametrize("legacy", [False, True])
def test_restored_unchanged_prefix_preserves_projection_and_tracks_future_nested_edits(legacy):
    state = SessionState(messages=[
        {"role": "system", "content": "Identity"},
        {"role": "user", "content": [{"type": "text", "text": "Old requirement"}]},
        {"role": "assistant", "content": "Large old result"},
    ])
    view = [state.messages[0], {"role": "system", "compacted": True, "content": "Earlier context"}]
    assert _remember_anthropic_history(state, view, None) == view
    stored = copy.deepcopy(state.anthropic_history)
    if legacy:
        del stored["source_messages"]
    state.anthropic_history = _restore_anthropic_history(stored, state.messages)
    assert _anthropic_history_input(state) == view
    state.append_message({"role": "user", "content": "New request"})
    assert _anthropic_history_input(state) == [*view, state.messages[-1]]
    assert "Large old result" not in json.dumps(_anthropic_history_input(state))
    state.messages[1]["content"][0]["text"] = "Revised requirement"
    assert _anthropic_history_input(state) == state.messages
    assert state.anthropic_history["source_messages"][1]["content"][0]["text"] == "Old requirement"


@pytest.mark.parametrize("checkpoint", [False, True], ids=["journal", "checkpoint"])
async def test_first_tool_step_autosaves_an_edit_already_used_in_the_request(monkeypatch, tmp_path, checkpoint):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    held_request, resume = asyncio.Event(), asyncio.Event()
    hold_after_read = False

    async def provider(request):
        if hold_after_read and len(peer.requests) == peer.reads:
            held_request.set()
            await resume.wait()
        return peer(request)

    install_sdk_transport(monkeypatch, "anthropic", provider)
    path = tmp_path / "session.json"

    async with _client() as client:
        environment = LocalEnvironment(str(tmp_path))
        session = build_session(agent=_agent(), llm=client, env=environment,
                                auto_save_path=str(path), max_budget_tokens=100_000, max_steps=20)
        running = restored = None
        try:
            for index in range(3):
                await session.add_user_message(f"Choose red for turn {index}")
                assert await session.run_loop() == "done"
            await session.add_user_message("Read the current evidence")
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            until_checkpoint = session._next_auto_save_checkpoint - session._auto_save_sequence
            extra_saves = until_checkpoint - 1 if checkpoint else int(until_checkpoint == 1)
            for _ in range(extra_saves):
                await session.enqueue_auto_save()
            saved = session.store.load_snapshot(str(path), session.agent.system_prompt)
            assert saved["messages"][1]["content"] == "Choose red for turn 0"

            session.messages[1]["content"] = "Choose green for turn 0"
            peer.reads = len(peer.requests) + 1
            hold_after_read = True
            running = asyncio.create_task(session.run_loop())
            await asyncio.wait_for(held_request.wait(), 5)
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            assert not running.done()
            assert "Choose green for turn 0" in json.dumps(peer.requests[-1])
            assert not session.persistence_errors

            saved = session.store.load_snapshot(str(path), session.agent.system_prompt)
            base = json.loads(path.read_text())
            assert (base["_autosave_sequence"] == saved["_autosave_sequence"]) is checkpoint
            restored = load_session(str(path), agent=_agent(), llm=client, env=environment)
            assert restored.messages[1]["content"] == "Choose green for turn 0"
            assert restored.state.anthropic_history["messages"][1]["content"] == "Choose green for turn 0"
            assert sum(message["role"] == "tool" for message in restored.messages) == 1

            resume.set()
            assert await running == "done"
        finally:
            resume.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            if restored is not None:
                await restored.aclose()
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize(("first_save", "revert"), [
    ("journal", True), ("manual", True), ("periodic", True), ("manual-after-freeze", True),
    ("journal", False),
])
async def test_native_history_delta_uses_the_last_saved_transcript(monkeypatch, tmp_path, first_save, revert):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    peer = _BoundThinkingProvider(reads=0)
    install_sdk_transport(monkeypatch, "anthropic", peer)
    path = tmp_path / "session.json"
    journal = path.with_suffix(".json.journal")
    resumed_path = tmp_path / "resumed.json"
    red, green = "Use the red option.", "Use the green option."
    store = PausedAppendStore()
    paused_append = first_save == "manual-after-freeze"

    async with _client() as client:
        environment = LocalEnvironment(str(tmp_path))
        session = build_session(
            agent=_agent(), llm=client, env=environment, store=store, auto_save_path=str(path),
            seed_user_messages=[{"role": "user", "content": _blocks(red)}],
        )
        restored = None
        try:
            for index in range(3):
                await session.add_user_message(f"Warmup {index}")
                assert await session.run_loop() == "done"
            until_checkpoint = session._next_auto_save_checkpoint - session._auto_save_sequence
            if first_save == "periodic":
                for _ in range(until_checkpoint - 1):
                    await session.enqueue_auto_save()
            elif until_checkpoint <= (3 if paused_append else 2):
                for _ in range(until_checkpoint):
                    await session.enqueue_auto_save()
            if paused_append:
                store.pause_next = True
                session.enqueue_auto_save()
                assert await asyncio.to_thread(store.started.wait, 1)

            source = session.messages[1]["content"][0]
            source["text"] = green
            if first_save.startswith("manual"):
                session.save(str(path))
            else:
                await session.enqueue_auto_save()
            saved_edit = session.store.load_snapshot(str(path), session.agent.system_prompt)
            assert saved_edit["messages"][1]["content"] == _blocks(green)
            base_sequence = json.loads(path.read_text())["_autosave_sequence"]
            assert (base_sequence == saved_edit["_autosave_sequence"]) is (first_save != "journal")

            expected = red if revert else green
            source["text"] = expected
            await session.add_user_message("Continue with the current instructions.")
            store.release.set()
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            delta = json.loads(journal.read_text().splitlines()[-1])
            assert delta["replace_from"] == (1 if revert else len(session.messages) - 2)
            native_view = delta["meta"]["session_state"]["anthropic_history"]
            assert native_view["messages"][1] == 1
            assert "source_messages" not in native_view

            shutil.copyfile(path, resumed_path)
            shutil.copyfile(journal, resumed_path.with_suffix(".json.journal"))
            restored = load_session(str(resumed_path), agent=_agent(), llm=client, env=environment,
                                    auto_save_path=str(resumed_path))
            assert restored.messages == session.messages
            assert restored.state.anthropic_history["messages"][1]["content"] == _blocks(expected)
            assert await restored.run_loop() == "done"
            assert await session.run_loop() == "done"
            assert _plain_messages(peer.requests[-2]["messages"]) == _plain_messages(peer.requests[-1]["messages"])
            assert expected in json.dumps(peer.requests[-1]["messages"])
            assert not session.persistence_errors and not restored.persistence_errors
        finally:
            store.release.set()
            if restored is not None:
                await restored.aclose()
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("previous_save", ["journal", "reopen-journal", "manual-covers-append"])
async def test_autosave_native_view_after_large_transcript_replacement(monkeypatch, tmp_path, previous_save):
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", 256 * 1024)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    archived = "A" * (140 * 1024)
    path = tmp_path / "session.json"
    journal = path.with_suffix(".json.journal")
    held_request, resume = asyncio.Event(), asyncio.Event()
    peer = _BoundThinkingProvider(reads=0, thinking=False)
    store = PausedAppendStore()

    class ArchiveShaper:
        def shape(self, messages):
            return [
                {**message, "content": "[Archived context]"} if message.get("content") == archived else message
                for message in messages
            ]

    async def provider(request):
        response = peer(request)
        if len(peer.requests) == 3:
            held_request.set()
            await resume.wait()
        return response

    install_sdk_transport(monkeypatch, "anthropic", provider)
    model = "claude-sonnet-5-5"
    async with _client(model) as client:
        environment = LocalEnvironment(str(tmp_path))
        options = {
            "agent": _agent(thinking=False, model=model), "llm": client, "env": environment,
            "auto_save_path": str(path), "shaper": ArchiveShaper(), "store": store,
        }
        session = build_session(**options)
        running = None
        try:
            await session.add_user_message("Warm up the conversation")
            assert await session.run_loop() == "done"
            assert session.state.anthropic_history is not None
            while session._next_auto_save_checkpoint - session._auto_save_sequence <= 3:
                await session.enqueue_auto_save()
            session.messages = [*session.messages, {"role": "user", "content": archived}]
            store.pause_next = previous_save == "manual-covers-append"
            await session.add_user_message("Read source.txt before answering")
            if previous_save == "manual-covers-append":
                assert await asyncio.to_thread(store.started.wait, 1)
                session.save(str(path))
                store.release.set()
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            before_bytes = journal.stat().st_size
            before = session.store.load_snapshot(str(path), session.agent.system_prompt)
            assert before_bytes > len(archived)
            assert before["session_state"]["anthropic_history"] is None
            assert before["messages"][-2]["content"] == archived

            if previous_save == "reopen-journal":
                await session.aclose()
                session = load_session(str(path), **options)
            peer.reads = 2
            running = asyncio.create_task(session.run_loop())
            await asyncio.wait_for(held_request.wait(), 5)
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            assert not running.done()
            assert "file evidence" in json.dumps(peer.requests[-1]["messages"])
            assert not session.persistence_errors

            saved = session.store.load_snapshot(str(path), session.agent.system_prompt)
            assert saved["_autosave_sequence"] > before["_autosave_sequence"]
            assert any("file evidence" in message["content"] for message in saved["messages"]
                       if message["role"] == "tool")
            history = saved["session_state"]["anthropic_history"]
            assert "source_messages" not in history
            assert any(type(message) is int for message in history["messages"])
            assert journal.stat().st_size < before_bytes + 8 * 1024

            resume.set()
            assert await running == "done"
        finally:
            resume.set()
            store.release.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await session.aclose()
            await environment.cleanup()


async def test_autosave_without_native_history_uses_only_session_state(tmp_path, monkeypatch):
    path = tmp_path / "session.json"
    environment = LocalEnvironment(str(tmp_path))
    async with _client() as client:
        session = build_session(agent=_agent(), llm=client, env=environment, auto_save_path=str(path))

        def unavailable():
            raise RuntimeError("model metadata unavailable during snapshot")

        monkeypatch.setattr(session.runner, "_requires_thinking_prefix", unavailable)
        try:
            await session.add_user_message("Save this prompt before the first request.")
            await session.enqueue_auto_save()
            assert not session.persistence_errors
            session.save(str(path))
            snapshot = session.store.load_snapshot(str(path), session.agent.system_prompt)
            assert snapshot["messages"][-1]["content"] == "Save this prompt before the first request."
            assert snapshot["session_state"]["anthropic_history"] is None
        finally:
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("source", ["invalid", [], [{}, {}], [False]])
def test_restore_rejects_malformed_optional_source_prefix(source):
    message = {"role": "user", "content": "Request"}
    assert _restore_anthropic_history({
        "messages": [message], "source_count": 1, "tools": None, "source_messages": source,
    }, [message]) is None


@pytest.mark.parametrize("reference", [True, -1, 1, "0"])
def test_restore_rejects_invalid_native_message_references(reference):
    assert _restore_anthropic_history({
        "messages": [reference], "source_count": 1, "tools": None,
    }, [{"role": "user", "content": "Request"}]) is None


@pytest.mark.parametrize("count", [0, 2])
def test_legacy_native_history_restores_without_an_optional_source_prefix(count):
    messages = [{"role": "user", "content": "First"}, {"role": "assistant", "content": "Answer"}]
    view = [{"role": "system", "compacted": True, "content": "Earlier context"}]
    restored = _restore_anthropic_history({"messages": view, "source_count": count, "tools": None}, messages)
    assert restored == {"messages": view, "source_count": count, "tools": None, "source_messages": messages[:count]}
    messages[0]["content"] = "Changed"
    if count:
        assert restored["source_messages"][0]["content"] == "First"
