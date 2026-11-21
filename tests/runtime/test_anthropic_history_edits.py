import json
from copy import deepcopy

import pytest

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.application._anthropic_history import _anthropic_history_input, _remember_anthropic_history
from opencollab.application.shaping import AutoCompactShaper
from opencollab.bootstrap import build_session, load_session
from opencollab.bootstrap.context_policy import ContextPolicy
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionState
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
