import json
from copy import deepcopy

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.application._anthropic_history import _anthropic_history_input, _remember_anthropic_history
from opencollab.application.shaping import AutoCompactShaper, OldHistorySnipShaper, ShaperPipeline
from opencollab.bootstrap import build_session, load_session
from opencollab.bootstrap.context_policy import ContextPolicy
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState
from opencollab.tools import builtin_tools
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


@pytest.mark.parametrize("save_after_edit", [False, True], ids=["edit-after-restore", "edit-before-save"])
async def test_edit_of_repeated_turn_keeps_saved_summary(monkeypatch, tmp_path, save_after_edit):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    document = "EARLY DOCUMENT " + ("Prior grant application details. " * 650)
    summary = "The grant application was acknowledged."
    correction = "Continue using Friday as the revised deadline."
    requests = []
    summaries = []

    def summarize(messages):
        summaries.append(messages)
        return summary

    def peer(request):
        requests.append(json.loads(request.content))
        if len(requests) > 3 and len(request.content) > 8_000:
            return http_response(request, 400, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "prompt is too long for the context window"},
            })
        return completion_http_response(request, text="Acknowledged")

    install_sdk_transport(monkeypatch, "anthropic", peer)
    model = "claude-sonnet-5-5"
    agent = Agent(
        name="reviewer", system_prompt="Review the grant application.", model=model, provider="anthropic",
    )
    env = LocalEnvironment(str(tmp_path))
    snapshot = str(tmp_path / "session.json")
    async with LLMClient(
        model=model, provider="anthropic", api_key="test-placeholder",  # pragma: allowlist secret
        base_url="https://provider.invalid/v1", max_retries=0,
    ) as client:
        settings = {"agent": agent, "llm": client, "env": env, "max_budget_tokens": 100_000}
        session = build_session(
            **settings,
            shaper=AutoCompactShaper(
                trigger_tokens=600, target_tokens=300, keep_recent_groups=3, summarizer=summarize,
                estimate_tokens=lambda rows: sum(len(str(row["content"])) for row in rows) // 4,
            ),
        )
        try:
            for prompt in (document, "Continue", "Continue"):
                await session.add_user_message(prompt)
                assert await session.run_loop() == "Acknowledged"
            assert summary in json.dumps(requests[-1]["messages"])
            assert document in json.dumps(session.messages)
            if save_after_edit:
                session.messages[5]["content"] = correction
            session.save(snapshot)
            await session.aclose()
            session = load_session(snapshot, **settings, context_policy=ContextPolicy(name="no_history_compaction"))
            if not save_after_edit:
                session.messages[5]["content"] = correction
            await session.add_user_message("What should we do next?")
            assert await session.run_loop() == "Acknowledged"
            assert len(requests) == 4
            assert len(summaries) == 1
            assert session.messages[5]["content"] == correction
            wire = json.dumps(requests[-1]["messages"])
            assert summary in wire
            assert correction in wire
            assert "EARLY DOCUMENT" not in wire
        finally:
            await session.aclose()
            await env.cleanup()

@pytest.mark.parametrize("edit_before_save", [True, False], ids=["edit-before-save", "edit-after-restore"])
@pytest.mark.parametrize("edit_user", [True, False], ids=["edit-summary-source", "edit-snipped-result"])
@pytest.mark.parametrize("assistant_text", [False, True], ids=["tool-only", "with-assistant-text"])
async def test_edit_keeps_independent_snipped_exchanges(
    monkeypatch, tmp_path, edit_before_save, edit_user, assistant_text,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    archive_names = ("older", "unchanged")
    for name in archive_names:
        (tmp_path / f"{name}.txt").write_text((f"ARCHIVE_{name} " + "data " * 22 + "\n") * 160)
    original = "Read the historical archives."
    if edit_user:
        original += " Background for the archive review." * 500
    summary = "The historical archives were requested."
    updated_summary = "The archive was reviewed."
    correction = "Use the April archive."
    requests = []

    def peer(request):
        body = json.loads(request.content)
        requests.append(body["messages"])
        if len(requests) > 3 and len(request.content) > 6000:
            return http_response(request, 400, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "prompt is too long for the context window"},
            })
        if len(requests) > 2:
            return completion_http_response(request, text="Finished")
        name = archive_names[len(requests) - 1]
        blocks = [{"type": "text", "text": f"Opening {name}."}] if assistant_text else []
        blocks.append({"type": "tool_use", "id": f"read-{name}", "name": "file_read", "input": {"path": f"{name}.txt"}})
        return http_response(request, 200, json={
            "id": f"msg_{name}", "type": "message", "role": "assistant", "model": body["model"],
            "content": blocks, "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        })

    def edit(session):
        target = next(row for row in session.messages if (
            row.get("content") == original if edit_user else row.get("tool_call_id") == "read-older"
        ))
        target["content"] = correction

    install_sdk_transport(monkeypatch, "anthropic", peer)
    model = "claude-sonnet-5-5"
    agent = Agent(
        name="reviewer", system_prompt="Review the archive.", model=model, provider="anthropic",
        tools=builtin_tools("file_read", headless=False),
    )
    env = LocalEnvironment(str(tmp_path))
    snapshot = str(tmp_path / "session.json")
    large_history = {"trigger_tokens": 100, "target_tokens": 60, "keep_recent_groups": 0}
    shapers = [OldHistorySnipShaper(**large_history)]
    if edit_user:
        shapers.append(AutoCompactShaper(**large_history, summarizer=lambda rows: summary))
    async with LLMClient(
        model=model, provider="anthropic", api_key="test-placeholder",  # pragma: allowlist secret
        base_url="https://provider.invalid/v1", max_retries=0,
    ) as client:
        options = {"agent": agent, "llm": client, "env": env, "max_budget_tokens": 100_000}
        resume = {**options, "context_policy": ContextPolicy(name="no_history_compaction")}
        session = build_session(**options, shaper=ShaperPipeline(tuple(shapers)))
        try:
            await session.add_user_message(original)
            assert await session.run_loop() == "Finished"
            projected = json.dumps(requests[-1])
            assert "ARCHIVE_older" not in projected and "ARCHIVE_unchanged" not in projected
            assert not edit_user or summary in projected
            assert all(f"ARCHIVE_{name}" in json.dumps(session.messages) for name in archive_names)
            if edit_before_save:
                edit(session)
            session.save(snapshot)
            await session.aclose()
            session = load_session(snapshot, **resume)
            if not edit_before_save:
                edit(session)

            for turn in range(3):
                if turn == 2:
                    corrected_row = next(row for row in session.messages if row.get("content") == correction)
                    corrected_row["content"] = correction + " Verify the dates."
                await session.add_user_message("Continue the archive review.")
                assert await session.run_loop() == "Finished"
                assert len(requests) == 4 + turn
                projected = json.dumps(requests[-1])
                assert "ARCHIVE_older" not in projected and "ARCHIVE_unchanged" not in projected
                assert "ARCHIVE_unchanged" in json.dumps(session.messages)
                assert summary not in projected
                if turn == 1:
                    assert updated_summary in projected and correction not in projected
                else:
                    assert correction in projected and updated_summary not in projected
                    blocks = [
                        (part["type"], part.get("id") or part.get("tool_use_id"))
                        for message in requests[-1] if isinstance(message["content"], list)
                        for part in message["content"]
                        if part["type"] in {"tool_use", "tool_result"}
                    ]
                    assert blocks == ([] if edit_user else [("tool_use", "read-older"), ("tool_result", "read-older")])
                    if assistant_text:
                        assert "Opening unchanged." in projected
                if turn == 2:
                    break
                session.save(snapshot)
                await session.aclose()
                if turn == 0:
                    reshape = AutoCompactShaper(
                        trigger_tokens=35, target_tokens=20, keep_recent_groups=0,
                        summarizer=lambda rows: updated_summary,
                    )
                    session = load_session(snapshot, **options, shaper=reshape)
                else:
                    session = load_session(snapshot, **resume)
        finally:
            await session.aclose()
            await env.cleanup()


@pytest.mark.parametrize("edit", ["insert-outside-summary", "insert-inside-summary", "delete-inside-summary"])
def test_disjoint_edits_keep_independent_summaries_and_compacted_tools(edit):
    system = {"role": "system", "content": "Review the two grant applications."}
    first_document = {"role": "user", "content": "First application"}
    first_reply = {"role": "assistant", "content": "First review"}
    first_summary = {"role": "system", "compacted": True, "content": "Summary of the first application"}
    bridge = {"role": "user", "content": "Read the archive for context"}
    call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "read-archive", "type": "function", "function": {"name": "file_read", "arguments": "{}"}},
    ]}
    result = {"role": "tool", "tool_call_id": "read-archive", "content": "Archive data"}
    cleared = {**result, "content": "[Old tool result content cleared]"}
    second_document = {"role": "user", "content": "Second application"}
    second_reply = {"role": "assistant", "content": "Second review"}
    second_summary = {"role": "system", "compacted": True, "content": "Summary of the second application"}
    repeat = {"role": "user", "content": "Continue"}
    acknowledgement = {"role": "assistant", "content": "Acknowledged"}
    state = SessionState(messages=[
        system, first_document, first_reply, bridge, call, result,
        second_document, second_reply, repeat, acknowledgement, repeat,
    ])
    _remember_anthropic_history(state, [
        system, first_summary, bridge, call, cleared, second_summary, repeat, acknowledgement, repeat,
    ], None)

    revised_bridge = {"role": "user", "content": "Check the archive's dates"}
    revised_tail = {"role": "user", "content": "Continue with the new dates"}
    insertion = {"role": "user", "content": "Check the updated budget"}
    state.messages[3] = revised_bridge
    state.messages[-1] = revised_tail
    if edit == "insert-outside-summary":
        state.messages.insert(8, insertion)
        remaining = [second_summary, insertion]
    elif edit == "insert-inside-summary":
        state.messages.insert(7, insertion)
        remaining = [second_document, insertion, second_reply]
    else:
        del state.messages[7]
        remaining = [second_document]
    assert _anthropic_history_input(state) == [
        system, first_summary, revised_bridge, call, cleared, *remaining, repeat, acknowledgement, revised_tail,
    ]


@pytest.mark.parametrize("edited_index", [1, -1], ids=["earlier-edit", "later-edit"])
def test_reused_tool_ids_keep_a_compacted_exchange_in_an_unchanged_end(edited_index):
    call = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "read-archive", "type": "function", "function": {"name": "file_read", "arguments": "{}"}},
    ]}
    result = {"role": "tool", "tool_call_id": "read-archive", "content": "First archive version"}
    state = SessionState(messages=[
        {"role": "system", "content": "Review the grant application."},
        {"role": "user", "content": "Read the original archive"},
        call, result,
        {"role": "user", "content": "Read the updated archive"},
        deepcopy(call), {**result, "content": "New archive version"},
        {"role": "user", "content": "Compare the dates"},
    ])
    view = deepcopy(state.messages)
    view[3]["content"] = "[Old tool result content cleared]"
    _remember_anthropic_history(state, view, None)
    state.messages[edited_index]["content"] = "Look up the deadline"
    view[edited_index]["content"] = "Look up the deadline"
    assert _anthropic_history_input(state) == view
