from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path

import pytest

from opencollab import OpenCollab
from opencollab.adapters.env import LocalEnvironment
from opencollab.bootstrap import load_session
from opencollab.domain.agent import Agent
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport
from tests.support.responses_provider_test_support import function_item, message_item
from tests.support.session_characterization_test_support import FakeLLMClient

_INTERIM = "I will inspect the inventory before reporting a subtotal."
_VISIBLE = "Verified inventory subtotal is 42."
_UPDATED = "Verified inventory subtotal is 43."
_PARTIAL = _VISIBLE + " The next section"
_REFUSAL = "I can share the verified subtotal of 42."
_REASONING = "I should check whether the inventory is complete."
_FILTER_REASON = "provider filtered the output"
_TRUNCATED_REASON = "output truncated: provider reached its generation limit"


def _chat_reply(request, model, step, reason, message):
    return http_response(request, 200, json={
        "id": f"inventory-{step}", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    })


def _inventory_read():
    return {"content": _INTERIM, "tool_calls": [{
        "id": "read-inventory", "type": "function", "function": {"name": "file_read",
            "arguments": json.dumps({"path": "inventory.txt"})},
    }]}


@pytest.mark.parametrize(("finish_reason", "terminal_reply", "visible"), [
    pytest.param("stop", {"content": _VISIBLE}, _VISIBLE, id="completed"),
    pytest.param("content_filter", {"content": _VISIBLE}, _VISIBLE, id="partial"),
    pytest.param("content_filter", {"content": None, "refusal": _REFUSAL}, _REFUSAL, id="refusal"),
    pytest.param("content_filter", {"content": None}, "", id="empty-after-tool"),
    pytest.param("length", {"content": _PARTIAL}, _PARTIAL, id="truncated"),
    pytest.param("length", {"content": None}, "", id="truncated-empty-after-tool"),
    pytest.param("length", {"content": None, "reasoning_content": _REASONING}, "", id="truncated-reasoning-only"),
    pytest.param("model_context_window_exceeded", {"content": _PARTIAL}, _PARTIAL, id="context-limited"),
])
async def test_team_delivers_member_reply_text_to_the_delegating_agent(
    tmp_path, monkeypatch, finish_reason, terminal_reply, visible,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: fixture-lead\n    prompt: Delegate the inventory review.\n    tools: [spawn_agent]\n"
        "  reviewer:\n    model: fixture-reviewer\n    prompt: Read the inventory.\n    tools: [file_read]\n"
        "topology:\n  lead: [reviewer]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        requests.append(body)
        tool_results = [row["content"] for row in body["messages"] if row["role"] == "tool"]
        if model == "fixture-lead":
            if tool_results:
                return completion_http_response(request, text=f"Lead received: {tool_results[-1]}")
            reason = "tool_calls"
            message = {"content": None, "tool_calls": [{
                "id": "delegate-review", "type": "function", "function": {"name": "spawn_agent",
                    "arguments": json.dumps({"role": "reviewer", "task": "Report the verified inventory subtotal."})},
            }]}
        elif not tool_results:
            reason = "tool_calls"
            message = _inventory_read()
        else:
            assert "verified inventory: 42" in tool_results[-1]
            reason = finish_reason
            message = terminal_reply
        return _chat_reply(request, model, len(requests), reason, message)

    install_sdk_transport(monkeypatch, "openai", handler)
    artifacts = tmp_path / "artifacts"
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, trace=False,
        artifacts=artifacts, use_worktrees=False, budget=100_000, timeout=5,
    )

    stop_reason = {
        "content_filter": _FILTER_REASON,
        "length": _TRUNCATED_REASON,
        "model_context_window_exceeded": _TRUNCATED_REASON,
    }.get(finish_reason)
    expected_delivery = f"Error: agent stopped: {stop_reason}" if stop_reason else visible
    if stop_reason and visible:
        expected_delivery += f"\n\n[Partial response]\n{visible}"
    assert [row["model"] for row in requests] == [
        "fixture-lead", "fixture-reviewer", "fixture-reviewer", "fixture-lead",
    ]
    assert [row["content"] for row in requests[-1]["messages"] if row["role"] == "tool"] == [expected_delivery]
    assert result.status == "completed"
    assert result.output == f"Lead received: {expected_delivery}"
    assert result.tokens == 16
    assert [row["exception_type"] for row in result.agent_failures] == (["AgentStopped"] if stop_reason else [])

    path = next(artifacts.glob("agent_1_*.json"))
    snapshot = app.read_session_snapshot(path)
    assert snapshot["session_state"]["phase"] == ("stopped" if stop_reason else "done")
    assert snapshot["session_state"]["terminal_reason"] == (stop_reason or "completed")
    recorded_text = terminal_reply.get("reasoning_content", visible)
    assert [row["content"] for row in snapshot["messages"] if row["role"] == "assistant"] == (
        [_INTERIM, recorded_text] if recorded_text else [_INTERIM]
    )
    model = FakeLLMClient()
    env = LocalEnvironment(str(tmp_path))
    restored = load_session(str(path), agent=Agent(name="reviewer", system_prompt="Read the inventory."),
                            llm=model, env=env)
    try:
        if stop_reason == _TRUNCATED_REASON:
            cursor = restored.state.completed_turn_start_message_index
            terminal_text = snapshot["messages"][cursor]["content"] if cursor is not None else ""
            assert terminal_text == visible
        else:
            assert await restored.run_loop() == visible
        assert model.calls == []
    finally:
        await restored.aclose()
        await env.cleanup()


@pytest.mark.parametrize("capture_cancelled", [False, True], ids=["io-error", "cancelled"])
async def test_team_delivers_stop_notice_after_borrowed_environment_capture_fails(
    tmp_path, monkeypatch, capture_cancelled,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: fixture-lead\n    prompt: Ask for the subtotal.\n"
        "    tools: [message_agent]\n    budget: {tokens: 16000}\n"
        "  reviewer:\n    model: fixture-reviewer\n    prompt: Total the inventory.\n"
        "    tools: [file_read, message_agent]\n    budget: {tokens: 8000}\n"
        "  checker:\n    model: fixture-checker\n    prompt: Confirm the units.\n"
        "    tools: [message_agent]\n    budget: {tokens: 8000}\n"
        "topology:\n  lead: [reviewer, checker]\n  reviewer: [lead]\n  checker: [lead]\n",
        encoding="utf-8",
    )
    initial_capture = asyncio.Event()
    reviewer_capture = asyncio.Event()
    checker_capture = asyncio.Event()
    checker_driver = None
    captures = 0

    class BorrowedEnvironment(LocalEnvironment):
        async def capture_changes(self):
            nonlocal captures, checker_driver
            captures += 1
            if captures == 1:
                initial_capture.set()
            elif captures == 2:
                reviewer_capture.set()
                await checker_capture.wait()
                assert checker_driver is not None
                await asyncio.wait({checker_driver})
                if not capture_cancelled:
                    raise OSError("Capture I/O failed.")
                worker = asyncio.create_task(asyncio.Event().wait())
                worker.cancel()
                await worker
            elif captures == 3:
                checker_driver = asyncio.current_task()
                checker_capture.set()
            return ""

    calls = []
    notices = []

    async def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        calls.append(model)
        count = calls.count(model)
        if model == "fixture-lead":
            if count == 1:
                questions = [{
                    "id": role, "type": "function", "function": {
                        "name": "message_agent", "arguments": json.dumps({
                            "to_role": role, "summary": "Inventory", "content": "Check the inventory.",
                        }),
                    },
                } for role in ("reviewer", "checker")]
                return _chat_reply(request, model, len(calls), "tool_calls", {"tool_calls": questions})
            if count == 2:
                return completion_http_response(request, text="Waiting for the subtotal.")
            assert count == 3
            notices.append(body["messages"][-1]["content"])
            return completion_http_response(request, text="The Reviewer stopped.")
        if model == "fixture-checker":
            assert count == 1
            await reviewer_capture.wait()
            return completion_http_response(request, text="The inventory uses unit counts.")
        assert model == "fixture-reviewer"
        if count == 1:
            return _chat_reply(request, model, len(calls), "tool_calls", _inventory_read())
        assert count == 2
        assert "verified inventory: 42" in body["messages"][-1]["content"]
        await initial_capture.wait()
        return _chat_reply(request, model, len(calls), "length", {"content": _PARTIAL})

    install_sdk_transport(monkeypatch, "openai", handler)
    env = BorrowedEnvironment(str(workspace))
    app = OpenCollab(
        workspace, environment=env, model="fixture-lead", provider="openai",
        api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    try:
        result = await app.team(
            "Check the inventory.", config=team, artifacts=tmp_path / "artifacts",
            use_worktrees=False, prebuild_team=True, serialize_turns=False, timeout=5,
        )
        assert await env.read_file("inventory.txt") == "verified inventory: 42\n"
    finally:
        await env.cleanup()

    assert (result.status, result.output, result.tokens) == ("completed", "The Reviewer stopped.", 24)
    assert [row["exception_type"] for row in result.agent_failures] == ["AgentStopped"]
    notice, = notices
    assert "<team-notice" in notice and _TRUNCATED_REASON in notice
    if not capture_cancelled:
        assert _PARTIAL in notice and "Capture I/O failed." in notice


@pytest.mark.parametrize(("finish_reason", "content", "output", "status", "reason"), [
    pytest.param("content_filter", None, None, "stopped", _FILTER_REASON, id="empty-filter"),
    pytest.param("content_filter", _VISIBLE, _VISIBLE, "stopped", _FILTER_REASON, id="partial-filter"),
    pytest.param("length", None, _INTERIM, "stopped", _TRUNCATED_REASON, id="length-stop"),
    pytest.param("stop", _VISIBLE, _VISIBLE, "completed", None, id="completed"),
])
async def test_team_lead_returns_only_visible_filtered_text(
    tmp_path, monkeypatch, finish_reason, content, output, status, reason,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n  lead:\n    prompt: Read the inventory.\n    tools: [file_read]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        model = body["model"]
        requests.append(body)
        if len(requests) == 1:
            return _chat_reply(request, model, 1, "tool_calls", _inventory_read())
        assert len(requests) == 2
        read_result = next(row["content"] for row in body["messages"] if row["role"] == "tool")
        assert "verified inventory: 42" in read_result
        return _chat_reply(request, model, 2, finish_reason, {"content": content})

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, trace=False,
        artifacts=tmp_path / "artifacts", use_worktrees=False, budget=100_000, timeout=5,
    )

    assert (result.output, result.status, result.reason, result.tokens) == (output, status, reason, 8)
    assert len(requests) == 2


@pytest.mark.parametrize(("visible", "reasoning"), [
    pytest.param(_VISIBLE, None, id="visible"),
    pytest.param(None, None, id="empty"),
    pytest.param(None, _REASONING, id="reasoning"),
])
async def test_first_team_turn_provider_failure_keeps_its_visible_output(tmp_path, monkeypatch, visible, reasoning):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n  lead:\n    prompt: Read the inventory.\n    tools: [file_read]\n",
        encoding="utf-8",
    )
    requests = []
    provider_message = "The reconciliation service is temporarily unavailable."

    def handler(request):
        body = json.loads(request.content)
        messages = body["messages"]
        requests.append(messages)
        if len(requests) == 1:
            reply = {**_inventory_read(), "content": visible}
            if reasoning:
                reply["reasoning_content"] = reasoning
            return _chat_reply(request, body["model"], 1, "tool_calls", reply)
        assert len(requests) == 2
        assert any(row["role"] == "tool" and "verified inventory: 42" in row["content"] for row in messages)
        return http_response(request, 503, json={"error": {
            "message": provider_message, "type": "server_error", "code": "service_unavailable",
        }})

    install_sdk_transport(monkeypatch, "openai", handler)
    artifacts = tmp_path / "artifacts"
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    result = await app.team(
        "Report the verified inventory subtotal.", config=team, artifacts=artifacts,
        use_worktrees=False, trace=False, budget=100_000, timeout=5,
    )

    assert (result.status, result.output, result.tokens) == ("failed", visible, 4)
    assert provider_message in result.reason
    assert len(requests) == 2
    lead, = artifacts.glob("agent_0_*.json")
    messages = app.read_session_snapshot(lead)["messages"]
    assert [row.get("content") for row in messages if row["role"] == "assistant"] == [visible]


@pytest.mark.parametrize(("initial_answer", "finish_reason", "message", "output"), [
    pytest.param(_VISIBLE, "stop", {"content": _UPDATED}, _UPDATED, id="completed-followup"),
    pytest.param(_VISIBLE, "diff-error", {"content": _UPDATED}, _UPDATED, id="completed-capture-error"),
    pytest.param(_VISIBLE, "checkpoint-error-stop", {"content": _UPDATED}, _UPDATED, id="completed-checkpoint-error"),
    pytest.param(_VISIBLE, "checkpoint-error-length", {"content": _PARTIAL}, _PARTIAL, id="truncated-checkpoint-error"),
    pytest.param(_VISIBLE, "checkpoint-error-content_filter", {"content": _REFUSAL}, _REFUSAL,
                 id="filtered-checkpoint-error"),
    pytest.param(_VISIBLE, "checkpoint-error-length", {"content": None, "reasoning_content": _REASONING}, _VISIBLE,
                 id="reasoning-checkpoint-error"),
    pytest.param(None, "checkpoint-error-length", {"content": None, "reasoning_content": _REASONING}, None,
                 id="reasoning-checkpoint-error-without-prior-answer"),
    pytest.param(None, "checkpoint-error-content_filter", {"content": None}, None, id="empty-checkpoint-error"),
    pytest.param(_VISIBLE, "content_filter", {"content": None}, _VISIBLE, id="empty-filter"),
    pytest.param(_VISIBLE, "content_filter", {"content": _REFUSAL}, _REFUSAL, id="visible-filter"),
    pytest.param(None, "content_filter", {"content": None}, None, id="interim-only"),
    pytest.param(_VISIBLE, "length", {"content": None}, _VISIBLE, id="empty-truncation"),
    pytest.param(_VISIBLE, "length", {"content": None, "reasoning_content": _REASONING}, _VISIBLE,
                 id="reasoning-truncation"),
    pytest.param(_VISIBLE, "length", {"content": _PARTIAL}, _PARTIAL, id="visible-truncation"),
    pytest.param(_VISIBLE, "provider-error", {}, _VISIBLE, id="provider-error"),
])
@pytest.mark.parametrize("timeout", [5.0, None], ids=["deadline-set", "no-deadline"])
async def test_team_keeps_completed_lead_answer_after_followup_without_a_visible_reply(
    tmp_path, monkeypatch, initial_answer, finish_reason, message, output, timeout,
):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    (tmp_path / "inventory.txt").write_text("verified inventory: 42\n", encoding="utf-8")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: fixture-lead\n    prompt: Report the inventory.\n    tools: [message_agent, file_read]\n"
        "  worker:\n    model: fixture-worker\n    prompt: Send delivery updates.\n    tools: [message_agent]\n"
        "topology:\n  lead: [worker]\n  worker: [lead]\n",
        encoding="utf-8",
    )
    notice = "The delivery audit is pending."
    checkpoint_error = finish_reason.startswith("checkpoint-error-")
    finish_reason = finish_reason.removeprefix("checkpoint-error-")
    reads_inventory = finish_reason in {"length", "provider-error"} or checkpoint_error
    provider_message = "Optional reconciliation is temporarily unavailable."
    lead_requests = []
    worker_requests = []
    fail_next_capture = False
    capture_failed = False

    class EvidenceEnvironment(LocalEnvironment):
        async def get_diff(self):
            nonlocal fail_next_capture, capture_failed
            if fail_next_capture:
                fail_next_capture = False
                capture_failed = True
                raise OSError("Could not read the inventory evidence.")
            return ""

    def message_call(call_id, to_role, text, interim=None):
        return {"content": interim, "tool_calls": [{
            "id": call_id, "type": "function", "function": {"name": "message_agent",
                "arguments": json.dumps({"to_role": to_role, "summary": "Delivery status", "content": text}),
            },
        }]}

    def handler(request):
        nonlocal fail_next_capture
        body = json.loads(request.content)
        model = body["model"]
        messages = body["messages"]
        if model == "fixture-worker":
            worker_requests.append(messages)
            if len(worker_requests) == 1:
                return _chat_reply(request, model, 1, "tool_calls", message_call("notify-lead", "lead", notice))
            assert any("Message queued to aid 0" in row["content"] for row in messages if row["role"] == "tool")
            return completion_http_response(request, text="Notification sent.")
        lead_requests.append(messages)
        if any(notice in row.get("content", "") for row in messages if row["role"] == "user"):
            if reads_inventory:
                read = next((row for row in messages if row["role"] == "tool"
                             and row["tool_call_id"] == "read-inventory"), None)
                if read is None:
                    return _chat_reply(request, model, len(lead_requests), "tool_calls", _inventory_read())
                assert "verified inventory: 42" in read["content"]
            if finish_reason == "provider-error":
                return http_response(request, 503, json={"error": {
                    "message": provider_message, "type": "server_error", "code": "service_unavailable",
                }})
            if finish_reason == "diff-error":
                fail_next_capture = True
            if finish_reason == "diff-error":
                return _chat_reply(request, model, len(lead_requests), "stop", message)
            return _chat_reply(request, model, len(lead_requests), finish_reason, message)
        if len(lead_requests) == 1:
            return _chat_reply(
                request, model, 1, "tool_calls",
                message_call("ask-worker", "worker", "Send a separate delivery update.", interim=_INTERIM),
            )
        return _chat_reply(request, model, len(lead_requests), "stop", {"content": initial_answer})

    install_sdk_transport(monkeypatch, "openai", handler)
    environment = EvidenceEnvironment(str(tmp_path)) if finish_reason == "diff-error" else None
    app = OpenCollab(
        tmp_path, model="fixture-lead", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1", environment=environment,
        config={"llm_max_retries": 0, "wire_protocol": "chat_completions", "llm_stream_chat": False},
    )
    artifacts = tmp_path / "artifacts"
    checkpoint_failed = False
    replace = os.replace

    def fail_terminal_checkpoint(source, destination, **kwargs):
        nonlocal checkpoint_failed
        target = Path(destination)
        if not checkpoint_failed and target.parent == artifacts and target.match("agent_0_*.json"):
            candidate = json.loads(Path(source).read_text(encoding="utf-8"))
            terminal_phase = "done" if finish_reason == "stop" else "stopped"
            if candidate["session_state"]["phase"] == terminal_phase and any(
                row["role"] == "user" and notice in row["content"] for row in candidate["messages"]
            ):
                journal = target.with_name(target.name + ".journal")
                base = json.loads(target.read_text(encoding="utf-8"))
                sequence = base["_autosave_sequence"]
                phase = base["session_state"]["phase"]
                for line in journal.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    if record["sequence"] > sequence:
                        sequence = record["sequence"]
                        phase = record["meta"]["session_state"]["phase"]
                if phase == terminal_phase and sequence < candidate["_autosave_sequence"]:
                    checkpoint_failed = True
                    raise OSError(errno.EIO, "terminal checkpoint rename failed")
        return replace(source, destination, **kwargs)

    if checkpoint_error:
        monkeypatch.setattr(os, "replace", fail_terminal_checkpoint)
    try:
        result = await app.team(
            "Report the verified inventory subtotal.", config=team, artifacts=artifacts, trace=True,
            use_worktrees=False, prebuild_team=True, serialize_turns=True, budget=100_000, timeout=timeout,
        )
    finally:
        if environment is not None:
            await environment.cleanup()
    assert len(lead_requests) == 3 + int(initial_answer is None) + int(reads_inventory)
    assert len(worker_requests) == 2
    rows = [json.loads(line) for line in (artifacts / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()]
    lead_completed = next(
        row["step"] for row in rows if row["type"] == "agent_completed" and row["payload"]["aid"] == 0
    )
    worker_sent = next(
        row["step"] for row in rows if row["type"] == "message_sent" and row["payload"]["from_aid"] == 1
    )
    assert lead_completed < worker_sent
    assert result.output == output
    snapshots = [app.read_session_snapshot(path) for path in sorted(artifacts.glob("agent_*.json"))]
    assert len(snapshots) == 2
    lead = snapshots[0]
    usage = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert result.tokens == sum(row["usage"]["total_tokens"] for row in usage)
    assert result.tokens == sum(snapshot["session_state"]["used_tokens"] for snapshot in snapshots)
    assert capture_failed == (finish_reason == "diff-error")
    assert checkpoint_failed == checkpoint_error
    if finish_reason == "provider-error":
        assert result.status == "failed" and provider_message in result.reason
        assert lead["messages"][-2]["content"] == _INTERIM
        assert lead["messages"][-2]["tool_calls"][0]["id"] == "read-inventory"
    elif finish_reason == "diff-error" or checkpoint_error:
        assert result.status == "failed"
        assert lead["session_state"]["phase"] == "error"
        cursor = lead["session_state"]["completed_turn_start_message_index"]
        if expected_reply := message.get("content"):
            assert cursor is not None
            assert [row["content"] for row in lead["messages"][cursor:] if row["role"] == "assistant"
                    and not row.get("tool_calls")] == [expected_reply]
        if checkpoint_error:
            assert "terminal checkpoint rename failed" in result.reason
            assert lead["session_state"]["terminal_reason"] == result.reason
    else:
        reason = {"content_filter": _FILTER_REASON, "length": _TRUNCATED_REASON}.get(finish_reason)
        assert (result.status, result.reason) == ("stopped" if reason else "completed", reason)


@pytest.mark.parametrize(
    ("code", "exception_type", "status_code"),
    [
        ("content_filter", "ResponsesTerminalEventError", None),
        ("invalid_prompt", "ResponsesTerminalEventError", None),
        ("rate_limit_exceeded", "ResponsesTransientEventError", 429),
        ("rate_limit_error", "RateLimitError", 429),
    ],
)
async def test_team_keeps_responses_failure_metadata(tmp_path, monkeypatch, code, exception_type, status_code):
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: gpt-4o\n    prompt: Delegate the inventory review.\n    tools: [spawn_agent]\n"
        "  reviewer:\n    model: gpt-4o-mini\n    prompt: Review the inventory.\n    tools: []\n"
        "topology:\n  lead: [reviewer]\n",
        encoding="utf-8",
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path.endswith("/responses")
        requests.append(body)
        model = body["model"]
        if len(requests) == 3:
            assert model == "gpt-4o"
            assert any(row.get("type") == "function_call_output" for row in body["input"])
            return completion_http_response(request, text="Recorded the reviewer's outcome.")
        status = "completed"
        error = None
        incomplete = None
        if len(requests) == 1:
            assert model == "gpt-4o"
            items = [function_item("delegate", "spawn_agent", '{"role":"reviewer","task":"Review the subtotal."}')]
        else:
            assert len(requests) == 2 and model == "gpt-4o-mini"
            if code == "rate_limit_error":
                return http_response(request, 429, json={
                    "error": {"type": code, "message": "The inventory provider reported an error."},
                })
            if code == "content_filter":
                status = "incomplete"
                incomplete = {"reason": code}
                items = [message_item(_VISIBLE)]
            else:
                status = "failed"
                error = {"code": code, "message": "The inventory provider reported an error."}
                items = []
        response = {
            "id": f"inventory-{len(requests)}", "object": "response", "model": model,
            "status": status, "error": error, "incomplete_details": incomplete, "output": items,
            "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
        }
        events = [{"type": "response.output_item.done", "output_index": index, "item": item}
                  for index, item in enumerate(items)]
        events.append({"type": f"response.{status}", "response": response})
        frames = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return http_response(request, 200, headers={"Content-Type": "text/event-stream"}, text=frames)

    install_sdk_transport(monkeypatch, "openai", handler)
    app = OpenCollab(
        tmp_path, model="gpt-4o", provider="openai", api_key="controlled-test",  # pragma: allowlist secret
        base_url="https://fixture.invalid/v1",
        config={"llm_max_retries": 0, "wire_protocol": "responses"},
    )
    result = await app.team(
        "Report the inventory subtotal.", config=team, trace=False, use_worktrees=False,
        budget=100_000, timeout=5,
    )

    assert len(requests) == 3
    expected_tokens = 8 if code == "rate_limit_error" else 12
    assert (result.status, result.output, result.tokens) == (
        "completed", "Recorded the reviewer's outcome.", expected_tokens,
    )
    assert result.agent_failures == ({
        "label": "reviewer",
        "exception_type": exception_type,
        "status_code": status_code,
        "provider_error_type": code,
    },)
