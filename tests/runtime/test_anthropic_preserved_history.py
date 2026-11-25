"""Native SDK sessions retain valid thinking and retire edited-prefix blocks."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import threading

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

    def __init__(self, reads=2, *, thinking=True, correction=False, read_input=None):
        self.reads = reads
        self.thinking = thinking
        self.correction = correction
        self.read_input = read_input or {}
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
                            "input": {"path": "source.txt", **self.read_input}})
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


@pytest.mark.parametrize("mode", ["projected", "journal"])
async def test_native_snapshot_preserves_typed_tool_input(monkeypatch, tmp_path, mode):
    peer = _BoundThinkingProvider(reads=1, read_input={"marker": 1})
    install_sdk_transport(monkeypatch, "anthropic", peer)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")

    def read_input(blocks):
        return next(block["input"] for block in blocks if block["type"] == "tool_use")

    def native_input(message):
        return read_input(message["provider_state"]["anthropic_content"])

    def sent_input(request):
        return read_input([block for message in request["messages"] for block in _blocks(message["content"])])

    class TypedProjection:
        def shape(self, messages):
            projected = copy.deepcopy(messages)
            if len(projected) > 2:
                native_input(projected[2])["marker"] = True
            return projected

    path = tmp_path / "typed.json"
    environment = LocalEnvironment(str(tmp_path))
    async with _client() as client:
        session = build_session(
            agent=_agent(), llm=client, env=environment,
            shaper=TypedProjection() if mode == "projected" else None,
            auto_save_path=str(path) if mode == "journal" else None,
        )
        restored = None
        try:
            await session.add_user_message("Read the file")
            assert await session.run_loop() == "done"
            session.save(str(path))
            if mode == "projected":
                assert sent_input(peer.requests[-1])["marker"] is True
                assert type(native_input(session.messages[2])["marker"]) is int
            else:
                if session._auto_save_sequence + 1 == session._next_auto_save_checkpoint:
                    await session.enqueue_auto_save()
                native_input(session.messages[2])["marker"] = True
                await session.enqueue_auto_save()
                journal = path.with_suffix(".json.journal")
                delta = json.loads(journal.read_text().splitlines()[-1])
                assert delta["replace_from"] == 2
                assert native_input(delta["messages"][0])["marker"] is True

            restored = load_session(str(path), agent=_agent(), llm=client, env=environment)
            await restored.add_user_message("Continue with the recorded tool use")
            assert await restored.run_loop() == "done"
            assert sent_input(peer.requests[-1])["marker"] is True
            assert "signed-1" not in _signatures(peer.requests[-1])
        finally:
            if restored is not None:
                await restored.aclose()
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("moment", ["live", "before-export", "after-restore"])
@pytest.mark.parametrize("target", ["recent-user", "first-user", "both-users", "insert", "delete", "cleared-result"])
async def test_source_edits_keep_unrelated_native_tool_compaction(monkeypatch, tmp_path, moment, target):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    marker = "Catalogue entry from the old archive."
    clear_marker = "[Old tool result content cleared]"
    archive = tmp_path / "archive.txt"
    archive.write_text(f"{marker}\n" * 400)
    peer = _BoundThinkingProvider(reads=2, thinking=False)
    context_limit = 8_000
    resumed_requests = []
    resuming = False

    def transport(request):
        if resuming:
            resumed_requests.append(json.loads(request.content))
            if len(request.content) > context_limit:
                return http_response(request, 400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": "prompt is too long for the context window",
                }})
        peer.read_input = {"path": archive.name, "limit": 400} if not peer.requests else {}
        return peer(request)

    install_sdk_transport(monkeypatch, "anthropic", transport)
    first_user = "Inspect the archive"
    recent_user = "Use a formal tone"
    first_edit = (first_user, "Review the archive entries")
    recent_edit = (recent_user, "Use an informal tone")
    inserted = "Additional context from the editor"
    edits = {
        "recent-user": [recent_edit],
        "first-user": [first_edit],
        "both-users": [first_edit, recent_edit],
        "insert": [(None, inserted)],
        "delete": [(first_user, None)],
        "cleared-result": [(marker, "The entries in the old archive have been revised.")],
    }[target]

    def edit(session):
        if target == "cleared-result":
            result = next(message for message in session.messages if message["role"] == "tool")
            assert marker in result["content"]
            result["content"] = edits[0][1]
        elif target == "insert":
            session.messages.insert(1, {"role": "user", "content": inserted})
        elif target == "delete":
            del session.messages[1]
        else:
            for original, revised in edits:
                message = next(message for message in session.messages if message.get("content") == original)
                message["content"] = revised

    model = "claude-sonnet-5-5"
    env = LocalEnvironment(str(tmp_path))
    path = str(tmp_path / "compacted.json")
    async with _client(model) as client:
        options = {"agent": _agent(thinking=False, model=model), "llm": client, "env": env}
        session = build_session(
            **options, shaper=ToolOutputClearShaper(trigger_tokens=2, target_tokens=1, keep_recent=1),
        )
        try:
            await session.add_user_message(first_user)
            assert await session.run_loop() == "done"
            await session.add_user_message(recent_user)
            assert await session.run_loop() == "done"
            assert marker in json.dumps(peer.requests[1]["messages"])
            assert clear_marker in json.dumps(peer.requests[-1]["messages"])
            assert marker in json.dumps(session.messages)
            if moment != "after-restore":
                edit(session)
            if moment != "live":
                session.save(path)
                await session.aclose()
                session = load_session(path, **options)
                if moment == "after-restore":
                    edit(session)
            session.runner.shaper = None

            await session.add_user_message("Summarize the evidence")
            resuming = True
            assert await session.run_loop() == "done"
            assert len(resumed_requests) == 1
            continued = json.dumps(resumed_requests[0]["messages"])
            for original, revised in edits:
                if original is not None:
                    assert original not in continued
                if revised is not None:
                    assert revised in continued
            assert marker not in continued
            assert (clear_marker in continued) is (target != "cleared-result")
        finally:
            await session.aclose()
            await env.cleanup()


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


def test_edit_in_summary_invalidates_it_even_when_a_later_message_is_identical():
    state = SessionState(messages=[
        {"role": "system", "content": "System instructions"},
        {"role": "user", "content": "First request"},
        {"role": "assistant", "content": "Acknowledged"},
        {"role": "user", "content": "Second request"},
        {"role": "assistant", "content": "Acknowledged"},
    ])
    summary = {"role": "system", "compacted": True, "content": "The first request was acknowledged"}
    _remember_anthropic_history(state, [state.messages[0], summary, state.messages[4]], None)
    state.messages[2]["content"] = "The first request still needs a decision"
    assert _anthropic_history_input(state) == state.messages


@pytest.mark.parametrize("same_result", [False, True])
def test_disjoint_edits_reuse_a_bracketed_projection_when_the_raw_occurrence_is_unique(same_result):
    call = {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "read-again", "type": "function",
            "function": {"name": "file_read", "arguments": '{"path":"archive.txt"}'},
        }],
    }
    state = SessionState(messages=[
        {"role": "user", "content": "Read the old archive"},
        call, {"role": "tool", "tool_call_id": "read-again", "content": "Old file data"},
        {"role": "user", "content": "Check the archive again"},
        copy.deepcopy(call), {
            "role": "tool", "tool_call_id": "read-again",
            "content": "Old file data" if same_result else "Revised file data",
        },
        {"role": "user", "content": "Summarize what changed"},
    ])
    view = copy.deepcopy(state.messages)
    view[2]["content"] = "[Old tool result content cleared]"
    _remember_anthropic_history(state, view, None)
    state.messages[0]["content"] = "Read the archive entries"
    state.messages[-1]["content"] = "Summarize the latest entries"
    expected = copy.deepcopy(state.messages)
    if not same_result:
        expected[2] = view[2]
    assert _anthropic_history_input(state) == expected


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


@pytest.mark.parametrize("failure", ["capacity", "format", "before-publish", "after-publish"])
async def test_native_rewrite_recovers_tool_result_when_checkpoint_fails(monkeypatch, tmp_path, failure):
    limit = 128 * 1024
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", limit)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    size = (limit - 14_000) // 2 if failure in {"capacity", "format"} else 1_000
    archive = _blocks("\u00e9" * size) + _blocks("x") * 256
    path = tmp_path / "fallback.json"
    journal = path.with_suffix(".json.journal")
    held_request, resume = asyncio.Event(), asyncio.Event()
    peer = _BoundThinkingProvider(reads=0, thinking=False)

    class ArchiveShaper:
        def shape(self, messages):
            return [
                {**message, "content": "[Archived context]"} if message.get("content") == archive else message
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
    environment = LocalEnvironment(str(tmp_path))
    async with _client(model) as client:
        agent = _agent(thinking=False, model=model)
        session = build_session(agent=agent, llm=client, env=environment,
                                auto_save_path=str(path), shaper=ArchiveShaper())
        running = restored = None
        try:
            if failure == "capacity":
                session.messages = [*session.messages, {
                    "role": "user", "content": _blocks("[Archived prefix] " + "p" * 20_000),
                }]
            await session.add_user_message("Warm up the conversation")
            assert await session.run_loop() == "done"
            await session.add_user_message("Read source.txt before answering")
            await asyncio.gather(*session.pending_cleanup_tasks)
            session.save(str(path))
            base = path.read_bytes()
            sequence = json.loads(base)["_autosave_sequence"] + 1
            session.messages = [*session.messages, {"role": "user", "content": archive}]
            assert session.state.anthropic_history is None

            checkpoint_error = OSError(f"controlled checkpoint failure {failure}")
            original_write = session.store._atomic_json_write
            original_compact = storage_module.write_regular_bytes_atomic

            def write_snapshot(target, document, **options):
                if failure == "before-publish" and document["_autosave_sequence"] == sequence:
                    raise checkpoint_error
                original_write(target, document, **options)

            def compact_journal(target, payload, **options):
                if failure == "after-publish":
                    raise checkpoint_error
                original_compact(target, payload, **options)

            peer.reads = 2
            with monkeypatch.context() as fault:
                fault.setattr(session.store, "_atomic_json_write", write_snapshot)
                fault.setattr(storage_module, "write_regular_bytes_atomic", compact_journal)
                running = asyncio.create_task(session.run_loop())
                await asyncio.wait_for(held_request.wait(), 5)
                await asyncio.gather(*session._auto_save_subscriber.pending_tasks)

                assert not running.done()
                saved = session.store.load_snapshot(str(path), agent.system_prompt)
                assert saved["_autosave_sequence"] == sequence
                assert any(message["content"] == archive for message in saved["messages"])
                restored = load_session(str(path), agent=agent, llm=client, env=environment)
                assert restored.state.anthropic_history is not None
                assert [row["content"] for row in restored.messages if row["role"] == "tool"] == [
                    row["content"] for row in session.messages if row["role"] == "tool"
                ]
                assert sum(row["role"] == "tool" for row in restored.messages) == 1
                if failure == "after-publish":
                    assert session.persistence_errors == (checkpoint_error,)
                    assert json.loads(path.read_bytes())["_autosave_sequence"] == sequence
                    assert journal.read_bytes() == b""
                elif failure == "format":
                    assert not session.persistence_errors
                    assert len(json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8")) > limit
                    assert path.stat().st_size < limit
                    assert json.loads(path.read_bytes())["_autosave_sequence"] == sequence
                    assert journal.read_bytes() == b""
                else:
                    assert not session.persistence_errors
                    record = json.loads(journal.read_bytes())
                    assert record["sequence"] == sequence
                    assert record["replace_from"] > 0
                    assert path.read_bytes() == base
                    if failure == "capacity":
                        assert len(journal.read_bytes()) < limit
                        assert len(json.dumps(saved, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > limit

            resume.set()
            if failure == "capacity":
                with pytest.raises(ValueError, match="session snapshot exceeds"):
                    await running
            else:
                assert await running == "done"
        finally:
            resume.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            if restored is not None:
                await restored.aclose()
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("peer_publishes_during_fallback", [False, True], ids=["checkpoint", "fallback"])
async def test_first_native_autosave_preserves_a_compacted_peer(
    monkeypatch, tmp_path, peer_publishes_during_fallback,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    (tmp_path / "source.txt").write_text("file evidence\n", encoding="utf-8")
    path = tmp_path / "peer-checkpoint.json"
    first_request, reply_to_first = asyncio.Event(), asyncio.Event()
    second_request, reply_to_second = asyncio.Event(), asyncio.Event()
    fallback_started, release_fallback = threading.Event(), threading.Event()
    peer = _BoundThinkingProvider(reads=1, thinking=False)

    async def provider(request):
        response = peer(request)
        if len(peer.requests) == 1:
            first_request.set()
            await reply_to_first.wait()
        elif len(peer.requests) == 2:
            second_request.set()
            await reply_to_second.wait()
        return response

    install_sdk_transport(monkeypatch, "anthropic", provider)
    model = "claude-sonnet-5-5"
    environment = LocalEnvironment(str(tmp_path))
    async with _client(model) as client:
        options = {"agent": _agent(thinking=False, model=model), "llm": client, "env": environment,
                   "auto_save_path": str(path)}
        writer = build_session(**options)
        stale = running = None
        try:
            await writer.add_user_message("Read source.txt and then answer")
            await asyncio.gather(*writer.pending_cleanup_tasks)
            writer.save(str(path))
            stale = load_session(str(path), **options)
            running = asyncio.create_task(stale.run_loop())
            await asyncio.wait_for(first_request.wait(), 10)

            if peer_publishes_during_fallback:
                append_replacement = stale.store.append_snapshot_replacement

                def fail_checkpoint(*args, **kwargs):
                    raise OSError("controlled failure before checkpoint publication")

                def pause_fallback(*args, **kwargs):
                    fallback_started.set()
                    if not release_fallback.wait(10):
                        raise TimeoutError("fallback was not released")
                    return append_replacement(*args, **kwargs)

                monkeypatch.setattr(stale.store, "_atomic_json_write", fail_checkpoint)
                monkeypatch.setattr(stale.store, "append_snapshot_replacement", pause_fallback)
                reply_to_first.set()
                assert await asyncio.to_thread(fallback_started.wait, 10)

            writer.messages = [*writer.messages, {"role": "user", "content": "peer checkpoint evidence"}]
            writer.save(str(path))
            published = OpenCollab.read_session_snapshot(path)
            release_fallback.set()
            reply_to_first.set()
            await asyncio.wait_for(second_request.wait(), 10)
            await asyncio.gather(*stale._auto_save_subscriber.pending_tasks)
            assert OpenCollab.read_session_snapshot(path) == published
            assert any("file evidence" in row["content"] for row in stale.messages if row["role"] == "tool")

            reply_to_second.set()
            assert await running == "done"
            assert stale.messages[-1]["content"] == "done"
            assert OpenCollab.read_session_snapshot(path) == published
            error, = stale.persistence_errors
            assert "conflicts with persisted" in str(error)
            assert path.with_suffix(".json.journal").read_bytes() == b""

            writer.messages.append({"role": "assistant", "content": "peer continued"})
            writer.save(str(path))
            resumed = OpenCollab.read_session_snapshot(path)
            assert resumed["_autosave_sequence"] == published["_autosave_sequence"] + 1
            assert resumed["messages"][-1]["content"] == "peer continued"
        finally:
            release_fallback.set()
            reply_to_first.set()
            reply_to_second.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            if stale is not None:
                await stale.aclose()
            await writer.aclose()
            await environment.cleanup()


@pytest.mark.parametrize("legacy_timestamps", [False, True], ids=["missing-timestamps", "timestamps"])
async def test_native_tool_step_preserves_a_near_capacity_legacy_prefix(monkeypatch, tmp_path, legacy_timestamps):
    limit = 96 * 1024
    monkeypatch.setattr(storage_module, "MAX_SESSION_SNAPSHOT_BYTES", limit)
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    (tmp_path / "source.txt").write_text("file evidence\n", encoding="utf-8")
    model = "claude-sonnet-5-5"
    agent = _agent(thinking=False, model=model)
    path = tmp_path / "legacy.json"
    archive = {"role": "user", "content": ""}
    prompt = "Read source.txt and then answer"
    messages = [
        {"role": "system", "content": agent.system_prompt, "timestamp": "2026-01-01T00:00:00+00:00"},
        archive,
        {"role": "assistant", "content": "That context is archived"},
        {"role": "user", "content": prompt},
    ]
    if legacy_timestamps:
        for message in messages:
            message["timestamp"] = "2026-01-01T00:00:00+00:00"
    snapshot = {
        "snapshot_version": 1, "_autosave_sequence": 2, "messages": messages,
        "session_state": {
            "phase": "idle", "pending_external_user_turn": {
                "turn_id": "legacy-turn", "status": "queued", "content": prompt, "message_index": 3,
            },
        },
    }
    archive["content"] = "x" * (limit - len(json.dumps(snapshot, separators=(",", ":"))) - 100)
    path.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    assert path.stat().st_size == limit - 100

    class ArchiveShaper:
        def shape(self, messages):
            return [
                {**message, "content": "[Archived context]"}
                if message.get("content") == archive["content"] else message
                for message in messages
            ]

    second_request = asyncio.Event()
    peer = _BoundThinkingProvider(reads=1, thinking=False)

    async def provider(request):
        response = peer(request)
        if len(peer.requests) == 2:
            second_request.set()
            await asyncio.Future()
        return response

    install_sdk_transport(monkeypatch, "anthropic", provider)
    environment = LocalEnvironment(str(tmp_path))
    async with _client(model) as client:
        session = load_session(
            str(path), agent=agent, llm=client, env=environment, auto_save_path=str(path), shaper=ArchiveShaper(),
        )
        running = asyncio.create_task(session.run_loop())
        waiting = asyncio.create_task(second_request.wait())
        restored = None
        try:
            done, _ = await asyncio.wait({waiting, running}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                pytest.fail(f"Turn ended before its second request with status {running.result()}")
            assert waiting in done
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)
            assert session.persistence_errors == ()
            saved = OpenCollab.read_session_snapshot(path)
            assert saved["_autosave_sequence"] == 3
            assert saved["messages"][:len(messages)] == messages
            record = json.loads(path.with_suffix(".json.journal").read_text(encoding="utf-8"))
            assert record["replace_from"] == len(messages)
            restored = load_session(str(path), agent=agent, llm=client, env=environment)
            assert restored.state.anthropic_history is not None
            assert any("file evidence" in row["content"] for row in restored.messages if row["role"] == "tool")
        finally:
            running.cancel()
            waiting.cancel()
            await asyncio.gather(running, waiting, return_exceptions=True)
            if restored is not None:
                await restored.aclose()
            await session.aclose()
            await environment.cleanup()


@pytest.mark.parametrize(
    ("base_sequence", "failed_write", "expected_writes"),
    [
        (4, "checkpoint", ["append"]),
        (7, None, ["append", "checkpoint"]),
        (7, "checkpoint", ["append", "checkpoint"]),
        (7, "append", ["append"]),
    ],
    ids=["between-checkpoints", "periodic-checkpoint", "checkpoint-error", "append-error"],
)
async def test_legacy_journal_persists_native_rewrites_before_checkpoint(
    monkeypatch, tmp_path, caplog, base_sequence, failed_write, expected_writes,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    path = str(tmp_path / "session.json")
    backend = storage_module.SessionStore()
    writes = []
    sequence = base_sequence + 1
    write_error = OSError(f"controlled {failed_write} failure")

    class LegacyJournalStore:
        save = backend.save
        load_messages = backend.load_messages
        load_snapshot = backend.load_snapshot
        save_manifest = backend.save_manifest
        has_snapshot = backend.has_snapshot

        def append_snapshot_delta(self, path, **delta):
            if delta["sequence"] == sequence:
                writes.append("append")
                assert delta["replace_from"] == 0
                if failed_write == "append":
                    raise write_error
            backend.append_snapshot_delta(path, **delta)

        def checkpoint_snapshot(self, path, messages, *, meta, sequence):
            if sequence == base_sequence + 1:
                writes.append("checkpoint")
                if failed_write == "checkpoint":
                    raise write_error
            backend.checkpoint_snapshot(path, messages, meta=meta, sequence=sequence)

    held_request, resume = asyncio.Event(), asyncio.Event()
    peer = _BoundThinkingProvider(reads=1, thinking=False)

    async def provider(request):
        response = peer(request)
        if len(peer.requests) == 2:
            held_request.set()
            await resume.wait()
        return response

    install_sdk_transport(monkeypatch, "anthropic", provider)
    model = "claude-sonnet-5-5"
    environment = LocalEnvironment(str(tmp_path))
    store = LegacyJournalStore()
    async with _client(model) as client:
        options = {"agent": _agent(thinking=False, model=model), "llm": client, "env": environment, "store": store}
        session = build_session(auto_save_path=path, **options)
        running = restored = None
        try:
            for _ in range(base_sequence):
                session.save(path)
            session.messages = [*session.messages, {"role": "user", "content": "Read source.txt before answering"}]
            running = asyncio.create_task(session.run_loop())
            await asyncio.wait_for(held_request.wait(), 5)
            await asyncio.gather(*session._auto_save_subscriber.pending_tasks)

            restored = load_session(path, **options)
            saved = store.load_snapshot(path, session.agent.system_prompt)
            if failed_write == "append":
                assert saved["_autosave_sequence"] == base_sequence
                assert restored.state.anthropic_history is None
                assert len(restored.messages) == 1
            else:
                assert saved["_autosave_sequence"] == sequence
                assert restored.state.anthropic_history is not None
                assert restored.messages == session.messages
                assert sum(row["role"] == "tool" for row in restored.messages) == 1
            assert writes == expected_writes
            if failed_write == "checkpoint" and "checkpoint" in expected_writes:
                assert "session checkpoint deferred after durable autosave" in caplog.text
            assert session.persistence_errors == ((write_error,) if failed_write == "append" else ())

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
