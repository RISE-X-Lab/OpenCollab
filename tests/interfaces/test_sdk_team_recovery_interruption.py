from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from opencollab import OpenCollab
from opencollab.adapters.cli import main as cli_main
from opencollab.adapters.cli.turn_queue import TurnQueue
from opencollab.application.scheduler_types import SchedulerTurnError
from opencollab.bootstrap import build_scheduler
from opencollab.bootstrap.config import build_config
from opencollab.bootstrap.runtime_context import build_runtime_context
from opencollab.domain.session import SessionPhase
from tests.support.provider_sdk_http import completion_http_response, http_response, install_sdk_transport


def _tool_reply(request, *calls):
    return http_response(request, 200, json={
        "id": "chat-team-recovery", "object": "chat.completion", "created": 1,
        "model": json.loads(request.content)["model"],
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(args),
                }}
                for call_id, name, args in calls
            ],
        }}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    })


@pytest.mark.parametrize(
    ("interruption", "submit"),
    [("cancel", True), ("timeout", True), ("cooperative", True), ("cooperative", False)],
)
async def test_interrupted_team_recovery_keeps_accepted_submission(tmp_path, monkeypatch, interruption, submit):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_LLM_MAX_RETRIES", "0")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: lead-fixture\n    prompt: Report the verified table count.\n"
        "    tools: [spawn_agent, submit]\n"
        "  reviewer:\n    model: reviewer-fixture\n    prompt: Review the table.\n"
        "    tools: []\ntopology:\n  lead: [reviewer]\n  reviewer: []\n",
        encoding="utf-8",
    )

    original_prompt = "Review the table."
    queued_prompt = "Follow up on the table review."
    final_prompt = "Recall the previously submitted count."
    summary = "The verified table contains 17 rows."
    restored_reply = "The previous submission reported 17 rows."
    original_review_started = asyncio.Event()
    original_review_release = asyncio.Event()
    recovery_review_started = asyncio.Event()
    recovery_review_cancelled = asyncio.Event()
    late_review_returned = asyncio.Event()
    final_requests = []
    stage = "original"

    async def handler(request):
        body = json.loads(request.content)
        messages = body["messages"]
        if stage == "final":
            final_requests.append((body["model"], messages))
            return completion_http_response(request, text=restored_reply)
        if body["model"] == "reviewer-fixture":
            if stage == "original":
                original_review_started.set()
                await original_review_release.wait()
                return completion_http_response(request, text="Original table reviewed.")
            recovery_review_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                recovery_review_cancelled.set()
                if interruption != "cooperative":
                    raise
                await asyncio.sleep(0)
                late_review_returned.set()
                return completion_http_response(request, text="Late reviewer result.")
        tool_rows = [row for row in messages if row["role"] == "tool"]
        if stage == "original":
            if tool_rows:
                return completion_http_response(request, text="Original answer.")
            return _tool_reply(request, ("original-review", "spawn_agent", {
                "role": "reviewer", "task": "Review the original table.",
            }))
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "original-review"
        assert messages[-1]["content"] == "deferred child interrupted by session restore"
        calls = [("recovery-review", "spawn_agent", {"role": "reviewer", "task": "Check the next table."})]
        if submit:
            calls.append(("submit-count", "submit", {"summary": summary}))
        return _tool_reply(request, *calls)

    install_sdk_transport(monkeypatch, "openai", handler)
    provider = {
        "provider": "openai", "model": "lead-fixture", "api_key": "team-test-token",  # pragma: allowlist secret
        "base_url": "https://fixture.invalid/v1",
    }
    transport = {"wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0}
    app = OpenCollab(tmp_path, config=transport, **provider)

    async def await_suspension(directory, *, submitted_summary=None, pending_call=None):
        while True:
            for path in directory.glob("agent_0_*.json"):
                snapshot = app.read_session_snapshot(path)
                state = snapshot["session_state"]
                if (
                    state["phase"] == "awaiting_events"
                    and state["submitted_summary"] == submitted_summary
                    and (pending_call is None or any(
                        row["tool_call_id"] == pending_call and row["status"] == "pending"
                        for row in state["pending_events"]
                    ))
                ):
                    return path, snapshot
            await asyncio.sleep(0.005)

    original_dir = tmp_path / "original"
    original = asyncio.create_task(app.team(
        original_prompt, config=team, artifacts=original_dir,
        use_worktrees=False, trace=False, budget=100_000, timeout=10,
    ))
    try:
        await asyncio.wait_for(original_review_started.wait(), 5)
        _, original_snapshot = await asyncio.wait_for(await_suspension(original_dir), 5)
        checkpoint = tmp_path / "checkpoint.json"
        checkpoint.write_text(json.dumps(original_snapshot), encoding="utf-8")
    finally:
        original_review_release.set()
        first_result = await asyncio.wait_for(original, 5)
    assert first_result.status == "completed"

    stage = "recovery"
    cfg = build_config(str(tmp_path), overrides={**provider, **transport, "budget": 100_000})
    context = build_runtime_context(str(tmp_path), cfg.model_dump(), trace=False)
    recovery_dir = tmp_path / "recovery"
    scheduler = build_scheduler(
        context, use_worktrees=False, interactive=True, team_config_path=team,
        session_file=str(checkpoint), save_dir=recovery_dir,
    )
    async def run_queued(prompt, aid, cancel_event):
        await scheduler.run_turn(aid, prompt, cancel_event=cancel_event)

    queue = TurnQueue(run_queued)
    if interruption == "cooperative":
        active = asyncio.create_task(queue.run())
        queue.submit(queued_prompt, 0)
    else:
        active = asyncio.create_task(scheduler.run(queued_prompt))
    try:
        await asyncio.wait_for(recovery_review_started.wait(), 5)
        recovery_path, accepted = await asyncio.wait_for(
            await_suspension(
                recovery_dir, submitted_summary=summary if submit else None, pending_call="recovery-review",
            ), 5,
        )
        pending_rows = [("recovery-review", "pending")]
        if submit:
            pending_rows.append(("submit-count", "done"))
        accepted_rows = accepted["session_state"]["pending_events"]
        assert [(row["tool_call_id"], row["status"]) for row in accepted_rows] == pending_rows
        if interruption == "cooperative":
            assert queue.interrupt()
            with pytest.raises(SchedulerTurnError) as stopped:
                await asyncio.wait_for(active, 5)
            assert stopped.value.terminal_reason == "interrupted by user"
            assert stopped.value.partial_answer == (summary if submit else None)
            await asyncio.wait_for(late_review_returned.wait(), 5)
        elif interruption == "cancel":
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
        else:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(active, 0)
        assert recovery_review_cancelled.is_set()
    finally:
        if not active.done():
            active.cancel()
        await asyncio.gather(active, return_exceptions=True)
        await scheduler.cleanup()

    settled = app.read_session_snapshot(recovery_path)
    stage = "final"
    resumed = await asyncio.to_thread(CliRunner().invoke, cli_main.app, [
        "--workspace", str(tmp_path), "--team-config", str(team), "--no-worktrees",
        "--provider", provider["provider"], "--model", provider["model"], "--api-key", provider["api_key"],
        "--base-url", provider["base_url"], "--budget", "100000", "--session", str(recovery_path),
        "--prompt", final_prompt,
    ])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    assert restored_reply in resumed.output
    assert [model for model, _ in final_requests] == ["lead-fixture"]
    messages = final_requests[0][1]
    assert messages[-1] == {"role": "user", "content": final_prompt}
    summary_message = {"role": "assistant", "content": summary}
    assert messages.count(summary_message) == int(submit)
    if submit:
        assert messages[-2] == summary_message
    assert [row["content"] for row in messages if row["role"] == "user"] == [original_prompt, final_prompt]
    review_result = (
        "Error: agent cancelled before completing delegated work" if interruption == "cooperative"
        else "Error: scheduler cleanup cancelled delegated work"
    )
    tools = [
        ("original-review", "deferred child interrupted by session restore"),
        ("recovery-review", review_result),
    ]
    if submit:
        tools.append(("submit-count", "Submitted. Your turn ends here."))
    assert [(row["tool_call_id"], row["content"]) for row in messages if row["role"] == "tool"] == tools
    assert settled["session_state"]["phase"] == "stopped"
    assert settled["session_state"]["pending_events"] == []
    assert settled["session_state"]["submitted_summary"] is None
    assert sum(row.get("content") == summary for row in settled["messages"]) == int(submit)
    if submit:
        assert settled["messages"][-1]["content"] == summary


@pytest.mark.parametrize(
    ("exit_input", "submit"),
    [("interrupt", True), ("eof", True), ("eof", False), ("after_drain", True)],
)
async def test_fresh_team_interrupt_then_exit_keeps_accepted_submission(tmp_path, monkeypatch, exit_input, submit):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    team = tmp_path / "team.yaml"
    team.write_text(
        "entry: lead\nroles:\n"
        "  lead:\n    model: lead-fixture\n    prompt: Report the verified table count.\n"
        "    tools: [spawn_agent, submit]\n"
        "  reviewer:\n    model: reviewer-fixture\n    prompt: Review the table.\n"
        "    tools: []\ntopology:\n  lead: [reviewer]\n  reviewer: []\n",
        encoding="utf-8",
    )

    original = "Check the table and report its size."
    followup = "Recall the previously submitted count."
    summary = "The verified count is 17 rows."
    late_review = "Late reviewer result."
    started = asyncio.Event()
    abandoned = asyncio.Event()
    release = asyncio.Event()
    returned = asyncio.Event()
    final_requests = []
    restoring = False

    async def handler(request):
        body = json.loads(request.content)
        if restoring:
            final_requests.append(body)
            return completion_http_response(request, text="Follow-up complete.")
        if body["model"] == "reviewer-fixture":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                abandoned.set()
                await release.wait()
                returned.set()
                return completion_http_response(request, text=late_review)
        assert body["messages"][-1]["content"] == original
        calls = [("review", "spawn_agent", {"role": "reviewer", "task": "Review the appendix."})]
        if submit:
            calls.append(("submit-count", "submit", {"summary": summary}))
        return _tool_reply(request, *calls)

    install_sdk_transport(monkeypatch, "openai", handler)
    provider = {
        "provider": "openai", "model": "lead-fixture", "api_key": "team-test-token",  # pragma: allowlist secret
        "base_url": "https://fixture.invalid/v1",
    }
    transport = {"wire_protocol": "chat_completions", "llm_stream_chat": False, "llm_max_retries": 0}
    cfg = build_config(str(tmp_path), overrides={**provider, **transport, "budget": 100_000})
    context = build_runtime_context(str(tmp_path), cfg.model_dump(), trace=False)
    artifacts = tmp_path / "fresh"
    scheduler = build_scheduler(
        context, use_worktrees=False, interactive=True, team_config_path=team,
        save_dir=artifacts, cleanup_timeout=2,
    )
    lead = scheduler.lead_session
    checkpoint = lead._checkpoint_terminal_snapshot
    checkpoint_published = asyncio.Event()
    driver_cancelled = asyncio.Event()

    async def checkpoint_then_hold(**kwargs):
        suspended = lead.state.phase is SessionPhase.AWAITING_EVENTS
        await checkpoint(**kwargs)
        if suspended:
            checkpoint_published.set()
            try:
                await asyncio.Event().wait()
            finally:
                driver_cancelled.set()

    if exit_input != "after_drain":
        monkeypatch.setattr(lead, "_checkpoint_terminal_snapshot", checkpoint_then_hold)

    completed_interrupts = []
    hard_cancellations = []

    async def run_queued(prompt, aid, event):
        try:
            await scheduler.run_turn(aid, prompt, cancel_event=event)
        except SchedulerTurnError as exc:
            completed_interrupts.append((exc.terminal_reason, exc.partial_answer))
        except asyncio.CancelledError as exc:
            hard_cancellations.append(exc)
            raise

    queue = TurnQueue(run_queued)

    async def wait_for_submitted_turn():
        await started.wait()
        if exit_input != "after_drain":
            await checkpoint_published.wait()
        while True:
            for path in artifacts.glob("agent_0_*.json"):
                snapshot = OpenCollab.read_session_snapshot(path)
                state = snapshot["session_state"]
                if state["phase"] != "awaiting_events":
                    continue
                assert state["submitted_summary"] == (summary if submit else None)
                pending = [("review", "pending"), ("submit-count", "done")] if submit else [("review", "pending")]
                assert [(row["tool_call_id"], row["status"]) for row in state["pending_events"]] == pending
                return
            await asyncio.sleep(0.005)

    async def return_late_response():
        await abandoned.wait()
        if exit_input != "after_drain":
            while not scheduler._shutting_down:
                await asyncio.sleep(0.001)
        release.set()

    class Prompt:
        reads = 0

        async def read(self):
            self.reads += 1
            if self.reads == 1:
                return original
            if self.reads == 2:
                await wait_for_submitted_turn()
                raise KeyboardInterrupt
            await abandoned.wait()
            assert queue.busy
            if exit_input == "after_drain":
                await queue.drain()
            if exit_input == "interrupt":
                raise KeyboardInterrupt
            raise EOFError

        def deliver(self, _line):
            return False

        def decline_pending(self):
            return False

    prompt = Prompt()
    late_response_task = asyncio.create_task(return_late_response())
    try:
        await asyncio.wait_for(cli_main._drive(
            queue, reader=cli_main._read_loop(prompt, queue, SimpleNamespace(selected_aid=0), scheduler.lead_session),
            until_drained=False,
        ), 6)
    finally:
        release.set()
        late_response_task.cancel()
        await asyncio.gather(late_response_task, return_exceptions=True)
        await cli_main._await_scheduler_cleanup(scheduler, cleanup_timeout=2)
    assert returned.is_set()
    assert prompt.reads == 3
    if exit_input == "after_drain":
        assert completed_interrupts == [("interrupted by user", summary)]
        assert not hard_cancellations
    else:
        assert not completed_interrupts
        assert len(hard_cancellations) == 1
        assert driver_cancelled.is_set()

    path = next(artifacts.glob("agent_0_*.json"))
    saved = OpenCollab.read_session_snapshot(path)
    state = saved["session_state"]
    assert (state["phase"], state["terminal_reason"]) == ("stopped", "interrupted by user")
    restoring = True
    resumed = await asyncio.to_thread(CliRunner().invoke, cli_main.app, [
        "--workspace", str(tmp_path), "--team-config", str(team), "--no-worktrees",
        "--provider", provider["provider"], "--model", provider["model"], "--api-key", provider["api_key"],
        "--base-url", provider["base_url"], "--budget", "100000", "--session", str(path), "--prompt", followup,
    ])
    assert resumed.exit_code == 0, (resumed.output, resumed.exception)
    assert [body["model"] for body in final_requests] == ["lead-fixture"]
    messages = final_requests[0]["messages"]
    assert [row["content"] for row in messages if row["role"] == "user"] == [original, followup]
    assert messages.count({"role": "assistant", "content": summary}) == int(submit)
    tool_rows = [row for row in messages if row["role"] == "tool"]
    assert [row["tool_call_id"] for row in tool_rows] == (["review", "submit-count"] if submit else ["review"])
    assert tool_rows[0]["content"].startswith("Error:")
    if submit:
        assert tool_rows[1]["content"] == "Submitted. Your turn ends here."
    assert all(row.get("content") != late_review for row in messages)
    assert state["pending_events"] == []
    assert state["submitted_summary"] is None
