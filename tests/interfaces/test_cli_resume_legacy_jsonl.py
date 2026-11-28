"""Exercise documented CLI and snapshot API support for one-row legacy JSONL."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli.main import app as cli
from tests.support.provider_sdk_http import completion_http_response, install_sdk_transport

SOURCE = "The previously verified shipment identifier is 731."
PROMPT = "State the previously verified shipment identifier."
ANSWER = "The shipment identifier is 731."


@pytest.mark.parametrize("scene", [
    "single-user", "single-assistant", "single-no-newline", "two-record", "legacy-array", "native",
    "invalid-native", "invalid-unversioned", "invalid-native-messages", "invalid-legacy",
])
def test_cli_restores_legacy_jsonl_with_any_valid_record_count(tmp_path, monkeypatch, scene):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_WIRE_PROTOCOL", "chat_completions")
    monkeypatch.setenv("OPENCOLLAB_LLM_STREAM_CHAT", "false")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    team = tmp_path / "team.yaml"
    team.write_text("entry: lead\nroles:\n  lead:\n    prompt: Report the recorded shipment.\n    tools: []\n")
    source_role = "assistant" if scene == "single-assistant" else "user"
    row = {"role": source_role, "content": SOURCE}
    legacy = scene.startswith("single-") or scene in {"two-record", "invalid-legacy"}
    restored_path = tmp_path / ("saved.jsonl" if legacy else "saved.json")
    if scene == "two-record":
        serialized = json.dumps({"role": "system", "content": "Keep the shipment record."}) + "\n" + json.dumps(row)
    elif scene == "invalid-legacy":
        serialized = json.dumps({**row, "content": 17})
    elif legacy:
        serialized = json.dumps(row)
    elif scene == "legacy-array":
        serialized = json.dumps([row])
    elif scene == "native":
        serialized = json.dumps({"snapshot_version": 1, "messages": [row]})
    elif scene == "invalid-unversioned":
        serialized = json.dumps({"session_state": {"used_tokens": 0}, **row})
    elif scene == "invalid-native-messages":
        serialized = json.dumps({"snapshot_version": 1, "messages": SOURCE, **row})
    else:
        serialized = json.dumps({"snapshot_version": 1, **row})
    restored_path.write_text(serialized if scene == "single-no-newline" else serialized + "\n", encoding="utf-8")
    source_bytes = restored_path.read_bytes()
    before, read_error = None, None
    try:
        before = OpenCollab.read_session_snapshot(restored_path)
    except ValueError as error:
        read_error = str(error)

    calls = []

    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        messages = json.loads(request.content)["messages"]
        calls.append(messages)
        match = [m for m in messages if m.get("role") == source_role and m.get("content") == SOURCE]
        if len(match) == 1 and messages[-1] == {"role": "user", "content": PROMPT}:
            return completion_http_response(request, text=ANSWER, output_tokens=5)
        return completion_http_response(request, text="No shipment record was loaded.", output_tokens=5)

    install_sdk_transport(monkeypatch, "openai", handler)
    result = CliRunner().invoke(cli, [
        "--workspace", str(workspace), "--team-config", str(team), "--no-worktrees",
        "--provider", "openai", "--model", "gpt-4o", "--base-url", "https://fixture.invalid/v1",
        "--api-key", tmp_path.name, "--session", str(restored_path), "--prompt", PROMPT,
    ])
    output_seen = ANSWER in result.output
    native_calls = len(calls)
    assert restored_path.read_bytes() == source_bytes
    if scene.startswith("invalid-"):
        expected = {
            "invalid-native": "missing required 'messages'",
            "invalid-unversioned": "missing required 'messages'",
            "invalid-native-messages": "'messages' must be a list",
            "invalid-legacy": "'content' must be text",
        }[scene]
        assert result.exit_code == 1 and native_calls == 0
        assert expected in read_error and expected in str(result.exception)
        return

    assert result.exit_code == 0, (result.output, result.exception)
    assert read_error is None and row in before["messages"]
    assert native_calls == 1 and output_seen
    assert [m.get("content") for m in calls[0]].count(SOURCE) == 1
    assert calls[0][-1] == {"role": "user", "content": PROMPT}
    transcript, = list((workspace / ".opencollab" / "sessions").glob("*/agent_0_*.json"))
    snapshot = OpenCollab.read_session_snapshot(transcript)
    assert snapshot["messages"][-1]["content"] == ANSWER
    assert snapshot["session_state"]["used_tokens"] == 7


@pytest.mark.parametrize("row", [
    {"role": "user", "content": [{"type": "text", "text": SOURCE}], "timestamp": "2026-11-26T15:00:00Z"},
    {"role": "assistant", "tool_calls": [{
        "id": "call-1", "type": "function", "function": {"name": "read_report", "arguments": "{}"},
    }]},
])
def test_reader_preserves_legitimate_single_message_fields(tmp_path, row):
    path = tmp_path / "record.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert OpenCollab.read_session_snapshot(path)["messages"] == [row]
