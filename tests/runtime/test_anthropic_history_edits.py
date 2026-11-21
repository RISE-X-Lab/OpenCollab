import asyncio
import json
from copy import deepcopy

import pytest

from opencollab import RunControl
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.client import LLMClient
from opencollab.application._anthropic_history import _anthropic_history_input, _remember_anthropic_history
from opencollab.application.shaping import (
    AutoCompactShaper,
    OldHistorySnipShaper,
    ShaperPipeline,
    ToolOutputClearShaper,
)
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
@pytest.mark.parametrize(("edit_user", "snip_mode"), [
    pytest.param(True, "pipeline", id="edit-summary-source"),
    pytest.param(False, "pipeline", id="edit-snipped-result"),
    pytest.param(True, "pipeline-copy", id="copied-snip-result"),
    pytest.param(True, "pipeline-copy-input", id="copied-snip-input"),
    pytest.param(True, "pipeline-copy-both", id="copied-snip-input-and-result"),
    pytest.param(True, "pipeline-copy-composed", id="copied-nested-pipeline-result"),
    pytest.param(True, "sync", id="summary-trial-snip"),
    pytest.param(True, "async", id="async-summary-trial-snip"),
    pytest.param(True, "shaper", id="shaper-trial-snip"),
    pytest.param(True, "restored-trial-original", id="restored-trial-original"),
    pytest.param(True, "restored-trial-copy", id="restored-trial-copy"),
    pytest.param(True, "restored-trial-result", id="restored-trial-result"),
    pytest.param(True, "pipeline-noop-copy", id="noop-copy-after-adopted-snip"),
])
@pytest.mark.parametrize("assistant_text", [False, True], ids=["tool-only", "with-assistant-text"])
async def test_edit_keeps_independent_snipped_exchanges(
    monkeypatch, tmp_path, edit_before_save, edit_user, assistant_text, snip_mode,
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
    trial_ran = False
    copying_stage = snip_mode.startswith("pipeline-copy")
    snip_as_stage = snip_mode in {"pipeline", "pipeline-noop-copy"} or copying_stage

    def peer(request):
        body = json.loads(request.content)
        requests.append(body["messages"])
        if snip_as_stage and len(requests) > 3 and len(request.content) > 6000:
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
    snipper = OldHistorySnipShaper(**large_history)

    def summarize(rows):
        nonlocal trial_ran
        if snip_mode in {"sync", "async"}:
            trial = snipper.shape(rows)
            trial_ran |= len(trial) < len(rows)
        return summary

    class TrialShaper:
        def shape(self, rows):
            nonlocal trial_ran
            trial, _ = ShaperPipeline((snipper,)).shape_with_report(list(rows))
            trial_ran |= len(trial) < len(rows)
            return [dict(row) for row in rows]

    class RestoredTrialShaper:
        def shape(self, rows):
            class RestoreInput:
                def shape(self, trial):
                    nonlocal trial_ran
                    trial_ran |= len(trial) < len(rows)
                    return [dict(row) for row in rows]

            trial = ShaperPipeline((snipper, RestoreInput())).shape(rows)
            assert trial == rows and trial is not rows
            if snip_mode == "restored-trial-original":
                return rows
            if snip_mode == "restored-trial-result":
                return trial
            return [dict(row) for row in rows]

    class CopyInput:
        def shape(self, rows):
            return [dict(row) for row in rows]

    class CopyingShaper:
        def shape(self, rows):
            if snip_mode == "pipeline-copy-composed":
                if sum(row.get("role") == "tool" for row in rows) < 2:
                    return rows
                return [dict(row) for row in ShaperPipeline((snipper, compact)).shape(list(rows))]
            source = rows if snip_mode == "pipeline-copy" else list(rows)
            result = ShaperPipeline((snipper,)).shape(source)
            return result if snip_mode == "pipeline-copy-input" else [dict(row) for row in result]

    class AsyncSummarizer:
        def __call__(self, rows):
            return summarize(rows)

        async def asummarize(self, rows):
            await asyncio.sleep(0)
            return summarize(rows)

    shapers = [snipper] if snip_mode in {"pipeline", "pipeline-noop-copy"} else []
    if copying_stage:
        shapers.append(CopyingShaper())
    if snip_mode == "shaper":
        shapers.append(TrialShaper())
    if snip_mode.startswith("restored-trial-"):
        shapers.append(RestoredTrialShaper())
    if snip_mode == "pipeline-noop-copy":
        shapers.append(CopyInput())
    callback = AsyncSummarizer() if snip_mode == "async" else summarize
    compact_options = dict(large_history)
    if snip_mode == "pipeline-copy-composed":
        compact_options["estimate_tokens"] = lambda rows: len(json.dumps(rows)) // 4
    compact = AutoCompactShaper(**compact_options, summarizer=callback)
    if edit_user and snip_mode != "pipeline-copy-composed":
        shapers.append(compact)
    async with LLMClient(
        model=model, provider="anthropic", api_key="test-placeholder",  # pragma: allowlist secret
        base_url="https://provider.invalid/v1", max_retries=0,
    ) as client:
        options = {"agent": agent, "llm": client, "env": env, "max_budget_tokens": 100_000}
        if assistant_text:
            options["run_control"] = RunControl(on_event=lambda _event: None)
        resume = {**options, "context_policy": ContextPolicy(name="no_history_compaction")}
        session = build_session(**options, shaper=ShaperPipeline(tuple(shapers)))
        try:
            await session.add_user_message(original)
            assert await session.run_loop() == "Finished"
            projected = json.dumps(requests[-1])
            assert "ARCHIVE_older" not in projected and "ARCHIVE_unchanged" not in projected
            assert not edit_user or summary in projected
            assert all(f"ARCHIVE_{name}" in json.dumps(session.messages) for name in archive_names)
            assert snip_as_stage or trial_ran
            if edit_before_save:
                edit(session)
            session.save(snapshot)
            await session.aclose()
            session = load_session(snapshot, **resume)
            if not edit_before_save:
                edit(session)

            for turn in range(3 if snip_as_stage else 1):
                if turn == 2:
                    corrected_row = next(row for row in session.messages if row.get("content") == correction)
                    corrected_row["content"] = correction + " Verify the dates."
                await session.add_user_message("Continue the archive review.")
                assert await session.run_loop() == "Finished"
                assert len(requests) == 4 + turn
                projected = json.dumps(requests[-1])
                if not snip_as_stage:
                    assert correction in projected and summary not in projected
                    assert all(f"ARCHIVE_{name}" in projected for name in archive_names)
                    assert "snipped_exchanges" not in session.state.anthropic_history
                    break
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


@pytest.mark.parametrize("edit", ["delete", "reorder"])
@pytest.mark.parametrize("include_shared_result", [False, True])
def test_ambiguous_adjacent_reused_calls_rebuild_after_edit(edit, include_shared_result):
    call = {"role": "assistant", "content": "Reading archive.", "tool_calls": [
        {"id": "archive", "type": "function", "function": {"name": "file_read", "arguments": "{}"}},
    ]}
    first = {"role": "tool", "tool_call_id": "archive", "content": "First version"}
    second = {**first, "content": "Second version"} if edit == "reorder" else deepcopy(first)
    shared = []
    if include_shared_result:
        call["tool_calls"].append({
            "id": "index", "type": "function", "function": {"name": "file_read", "arguments": "{}"},
        })
        shared = [{"role": "tool", "tool_call_id": "index", "content": "Shared index"}]
    first_batch = [call, first, *shared]
    second_batch = [deepcopy(call), second, *deepcopy(shared)]
    state = SessionState(messages=[
        {"role": "system", "content": "Review the archives."},
        {"role": "user", "content": "Read both archive versions."},
        *first_batch, *second_batch,
        {"role": "user", "content": "Compare their dates."},
    ])
    view = deepcopy(state.messages)
    view[3]["content"] = "[Old tool result content cleared]"
    _remember_anthropic_history(state, view, None)
    state.messages[1]["content"] = "Read the selected archive versions."
    state.messages[-1]["content"] = "Compare their deadlines."
    boundary = 2 + len(first_batch)
    if edit == "delete":
        del state.messages[2:boundary]
    else:
        state.messages[2:-1] = [*state.messages[boundary:-1], *state.messages[2:boundary]]
    assert _anthropic_history_input(state) == state.messages


@pytest.mark.parametrize("scenario", [
    "first-edit", "later-edit", "both-edit", "same-sibling", "asymmetric", "custom-marker",
    "delete", "reorder", "reordered-projection", "marker-conflicts",
])
def test_adjacent_cleared_batches_keep_their_ordered_projection(scenario):
    leader = {"role": "assistant", "content": "Reading the archive.", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "file_read", "arguments": "{}"}}
        for call_id in ("report", "index")
    ]}
    source = [
        {"role": "system", "content": "Summarize the archive."},
        {"role": "user", "content": "Read both archive versions."},
    ]
    for month in ("April", "May"):
        report = "Identical" if scenario == "delete" else month
        index = "Shared" if scenario in {"same-sibling", "delete"} else month
        index_content = f"{index} index details. " * 10
        if scenario in {"asymmetric", "reordered-projection"}:
            index_content = f"{index} index."
        if scenario == "marker-conflicts" and month == "May":
            index_content = "Earlier result cleared."
        source.extend([
            deepcopy(leader),
            {"role": "tool", "tool_call_id": "report", "content": f"{report} report details. " * 10},
            {"role": "tool", "tool_call_id": "index", "content": index_content},
        ])
    source.extend([
        {"role": "assistant", "content": "Finished reading both reports."},
        {"role": "user", "content": "Compare their dates."},
    ])
    state = SessionState(messages=source)
    projection = deepcopy(source)
    marker = (
        "Earlier result cleared." if scenario in {"custom-marker", "marker-conflicts"}
        else "[Old tool result content cleared]"
    )
    cleared_rows = (3, 6) if scenario in {"asymmetric", "reordered-projection"} else (3, 4, 6, 7)
    for index in cleared_rows:
        if scenario != "delete" or index != 7:
            projection[index]["content"] = marker
    if scenario == "reordered-projection":
        projection[2:8] = [*projection[5:8], *projection[2:5]]
    _remember_anthropic_history(state, projection, None)

    source[-1]["content"] = "Compare their updated deadlines."
    if scenario == "delete":
        del source[2:5]
    elif scenario == "reorder":
        source[2:8] = [*source[5:8], *source[2:5]]
    else:
        targets = (6,) if scenario == "later-edit" else (3, 6) if scenario in {"both-edit", "same-sibling"} else (3,)
        for index in targets:
            source[index]["content"] = f"Edited report {index}."
    if scenario in {"delete", "reorder", "reordered-projection", "marker-conflicts"}:
        expected = source
    else:
        expected = projection
        expected[-1] = source[-1]
        for index in targets:
            expected[index] = source[index]
    assert _anthropic_history_input(state) == expected


@pytest.mark.parametrize("edited_batch", [0, 1])
@pytest.mark.parametrize("scenario", [
    "different-leaders", "identical-leaders", "shared-sibling", "changed-neighbor",
    "deleted-neighbor", "shared-sibling-changed-neighbor", "shared-sibling-deleted-neighbor", "replaced-leader",
])
def test_reused_batch_keeps_unchanged_results_in_its_original_turn(edited_batch, scenario):
    calls = [
        {"id": call_id, "type": "function", "function": {"name": "file_read", "arguments": f'{{"path":"{call_id}"}}'}}
        for call_id in ("report", "index")
    ]
    rows = [{"role": "system", "content": "Review the archive."}]
    batches = []
    for version in ("original", "revised"):
        current_batch = {
            "user": {"role": "user", "content": f"Read the {version} archive."},
            "leader": {
                "role": "assistant", "tool_calls": deepcopy(calls),
                "content": (
                    f"Reading the {version} archive." if scenario == "different-leaders" else "Reading the archive."
                ),
            },
            "report": {"role": "tool", "tool_call_id": "report", "content": f"{version} report"},
            "index": {
                "role": "tool", "tool_call_id": "index",
                "content": "Shared index" if scenario.startswith("shared-sibling") else f"{version} index",
            },
            "answer": {"role": "assistant", "content": f"Reviewed the {version} batch."},
        }
        rows.extend(current_batch.values())
        batches.append(current_batch)
    rows.append({"role": "user", "content": "Compare the archives."})
    state = SessionState(messages=rows)
    view = deepcopy(rows)
    for index, batch in enumerate(batches):
        for tool_id in ("report", "index"):
            result_index = next(i for i, row in enumerate(rows) if row is batch[tool_id])
            view[result_index]["content"] = f"Cleared {tool_id} from batch {index}."
    _remember_anthropic_history(state, view, None)

    changed_batch = batches[edited_batch]
    if scenario.endswith("changed-neighbor"):
        changed_batch["user"]["content"] += " Include a citation."
    if scenario.endswith("deleted-neighbor"):
        rows.remove(changed_batch["user"])
    if scenario == "replaced-leader":
        changed_batch["leader"]["tool_calls"][0]["function"]["arguments"] = '{"path":"replacement"}'
    changed_batch["report"]["content"] = "Updated report."

    expected = deepcopy(rows)
    for index, batch in enumerate(batches):
        for tool_id in ("report", "index"):
            if index == edited_batch and (tool_id == "report" or scenario == "replaced-leader"):
                continue
            result_index = next(i for i, row in enumerate(rows) if row is batch[tool_id])
            expected[result_index]["content"] = f"Cleared {tool_id} from batch {index}."
    assert _anthropic_history_input(state) == expected


def test_changed_tool_arguments_do_not_reuse_previous_results():
    calls = [
        {"id": call_id, "type": "function", "function": {"name": "file_read", "arguments": "{}"}}
        for call_id in ("left", "right")
    ]
    state = SessionState(messages=[
        {"role": "user", "content": "Read both archived files."},
        {"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "left", "content": "Original left file"},
        {"role": "tool", "tool_call_id": "right", "content": "Original right file"},
    ])
    view = deepcopy(state.messages)
    for result in view[2:]:
        result["content"] = "[Old tool result content cleared]"
    _remember_anthropic_history(state, view, None)

    calls[0]["function"]["arguments"] = '{"path":"revised.txt"}'
    state.messages[2]["content"] = "Updated left file"
    assert _anthropic_history_input(state) == state.messages


@pytest.mark.parametrize("edit_before_save", [True, False])
@pytest.mark.parametrize("scenario", [
    "different-files", "same-file", "edit-user-neighbor", "edit-assistant-neighbor", "edit-first-result",
    "edit-second-result",
])
async def test_edits_preserve_unchanged_cleared_results(
    tmp_path, monkeypatch, edit_before_save, scenario,
):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    model = "claude-sonnet-5-5"
    requests = []
    revisions = ["ARCHIVE_FIRST_REVISION", "ARCHIVE_SECOND_REVISION", "ARCHIVE_LATEST_REVISION"]
    batch = scenario in {"edit-first-result", "edit-second-result"}
    neighbor = {"edit-user-neighbor": "user", "edit-assistant-neighbor": "assistant"}.get(scenario)
    edited_result = {"edit-first-result": "toolu_first", "edit-second-result": "toolu_second"}.get(scenario)
    same_file = scenario == "same-file" or neighbor is not None
    read_paths = ("archive.txt", "archive.txt", "recent.txt") if same_file else (
        "first.txt", "second.txt", "recent.txt",
    )
    call_ids = ("toolu_first", "toolu_second", "toolu_recent") if batch else (
        "toolu_archive", "toolu_archive", "toolu_recent",
    )
    first, second, recent = zip(read_paths, revisions, call_ids, strict=True)
    read_rounds = ((first, second), (recent,)) if batch else ((first,), (second,), (recent,))
    active_reads = ()
    resuming = False

    def peer(request):
        nonlocal active_reads
        requests.append(request.content)
        if resuming and len(request.content) > 8_000:
            return http_response(request, 400, json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "prompt is too long for the context window"},
            })
        if not active_reads:
            return completion_http_response(request, text="Recorded.")
        reads, active_reads = active_reads, ()
        blocks = []
        for name, revision, call_id in reads:
            (tmp_path / name).write_text((f"{revision} " + "entry " * 20 + "\n") * 35)
            blocks.append({
                "type": "tool_use", "id": call_id, "name": "file_read", "input": {"path": name, "limit": 35},
            })
        return http_response(request, 200, json={
            "id": f"msg_{revision}", "type": "message", "role": "assistant", "model": model,
            "content": blocks,
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        })

    def revise(session):
        for message in session.messages:
            if message.get("content") == "Start reviewing the archive.":
                message["content"] = "Review the archived dates."
            elif message.get("content") == "Compare the archive entries.":
                message["content"] = "Compare the revised archive entries."
            elif edited_result is not None and message.get("tool_call_id") == edited_result:
                message["content"] = "The amount in this archive was corrected."
        if neighbor is not None:
            first_result = next(i for i, row in enumerate(session.messages) if row["role"] == "tool")
            adjacent = session.messages[first_result - 2 if neighbor == "user" else first_result + 1]
            assert adjacent["role"] == neighbor
            adjacent["content"] += " Add the page reference."

    install_sdk_transport(monkeypatch, "anthropic", peer)
    agent = Agent(name="reader", system_prompt="Review the archives.", model=model, provider="anthropic",
                  thinking=False, tools=builtin_tools("file_read", headless=False))
    env = LocalEnvironment(str(tmp_path))
    snapshot = str(tmp_path / "export.json")
    try:
        async with LLMClient(model=model, provider="anthropic", max_retries=0,
                             api_key=__name__, base_url="https://example.invalid/v1") as client:
            options = {"agent": agent, "llm": client, "env": env}
            session = build_session(
                **options, shaper=ToolOutputClearShaper(trigger_tokens=200, target_tokens=100, keep_recent=1),
            )
            try:
                await session.add_user_message("Start reviewing the archive.")
                assert await session.run_loop() == "Recorded."
                for number, reads in enumerate(read_rounds, 1):
                    active_reads = reads
                    await session.add_user_message(f"Open archive revision {number}.")
                    assert await session.run_loop() == "Recorded."
                await session.add_user_message("Compare the archive entries.")
                assert await session.run_loop() == "Recorded."
                assert requests[-1].count(b"[Old tool result content cleared]") == 2
                assert all(revision in json.dumps(session.messages) for revision in revisions)
                if edit_before_save:
                    revise(session)
                session.save(snapshot)
            finally:
                await session.aclose()

            session = load_session(snapshot, **options, context_policy=ContextPolicy(name="no_history_compaction"))
            try:
                if not edit_before_save:
                    revise(session)
                resuming = True
                await session.add_user_message("What should the report highlight?")
                sent = len(requests)
                assert await session.run_loop() == "Recorded."
                assert len(requests) == sent + 1
                assert requests[-1].count(b"[Old tool result content cleared]") == (1 if batch else 2)
                assert b"Review the archived dates." in requests[-1]
                assert b"Compare the revised archive entries." in requests[-1]
                if neighbor is not None:
                    assert b"Add the page reference." in requests[-1]
                if edited_result is not None:
                    assert b"The amount in this archive was corrected." in requests[-1]
                assert revisions[0].encode() not in requests[-1]
                assert revisions[1].encode() not in requests[-1]
                assert revisions[2].encode() in requests[-1]
                blocks = [
                    (block["type"], block.get("id") or block.get("tool_use_id"))
                    for message in json.loads(requests[-1])["messages"]
                    if isinstance(message["content"], list)
                    for block in message["content"]
                    if block["type"] in {"tool_use", "tool_result"}
                ]
                expected = []
                for reads in read_rounds:
                    expected.extend((kind, call_id)
                                    for kind in ("tool_use", "tool_result") for _, _, call_id in reads)
                assert blocks == expected
            finally:
                await session.aclose()
    finally:
        await env.cleanup()
