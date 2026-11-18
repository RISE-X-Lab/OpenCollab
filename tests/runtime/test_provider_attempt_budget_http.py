from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from opencollab import OpenCollab, RunControl
from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm import retry
from opencollab.adapters.llm.client import LLMClient
from opencollab.application.event_bus import EventBus
from opencollab.application.scheduler import Scheduler
from opencollab.bootstrap import build_session
from opencollab.domain.agent import Agent
from opencollab.domain.scheduler import SessionControlBlock
from opencollab.domain.session import SessionPhase, SessionState
from tests.interfaces.test_member_authored_workflow import _manager
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.paths import REPO_ROOT


def _event(value):
    return f"data: {json.dumps(value)}\n\n".encode()


@pytest.fixture
def attempt_server(monkeypatch, tmp_path):
    servers = []
    monkeypatch.setenv("OPENCOLLAB_API_USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")
    monkeypatch.delenv("OPENCOLLAB_EXTERNAL_PROVIDER_ISOLATION", raising=False)
    monkeypatch.setattr(retry, "extract_retry_after_seconds", lambda _error: 0.0)
    monkeypatch.setattr(retry.random, "uniform", lambda _lo, _hi: 0.0)

    def start(protocol, observe=lambda: None):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"body": body, "observed": observe()})
                first = len(requests) == 1
                total = 1500 if first else 4
                status = 200
                content_type = "text/event-stream"
                if protocol == "chat":
                    common = {"id": "chat_attempt", "object": "chat.completion.chunk",
                              "created": 1, "model": body["model"]}
                    payload = b"".join([
                        _event({**common, "choices": [{"index": 0, "delta": {
                            "role": "assistant", "content": "ok"}, "finish_reason": None}]}),
                        _event({**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                        _event({**common, "choices": [], "usage": {
                            "prompt_tokens": total - 2, "completion_tokens": 2, "total_tokens": total}}),
                    ])
                    if not first:
                        payload += b"data: [DONE]\n\n"
                elif protocol == "responses":
                    item = {"id": "msg_attempt", "type": "message", "role": "assistant",
                            "status": "completed", "content": [{"type": "output_text", "text": "ok",
                                                                   "annotations": []}]}
                    response = {"id": "resp_attempt", "object": "response", "model": body["model"],
                                "status": "failed" if first else "completed", "output": [] if first else [item],
                                "error": {"code": "server_error", "message": "Temporarily unavailable"}
                                if first else None, "incomplete_details": None,
                                "usage": {"input_tokens": total - 2, "output_tokens": 2, "total_tokens": total}}
                    payload = _event({"type": "response.failed", "response": response}) if first else b"".join([
                        _event({"type": "response.output_item.done", "output_index": 0, "item": item}),
                        _event({"type": "response.completed", "response": response}),
                    ])
                else:
                    content_type = "application/json"
                    status = 503 if first else 200
                    payload = json.dumps({"type": "error", "error": {
                        "type": "overloaded_error", "message": "Temporarily unavailable"}}
                        if first else {"id": "msg_attempt", "type": "message", "role": "assistant",
                                       "model": body["model"], "content": [{"type": "text", "text": "ok"}],
                                       "stop_reason": "end_turn", "stop_sequence": None,
                                       "usage": {"input_tokens": 2, "output_tokens": 2}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if protocol == "chat" and first:
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                else:
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}/v1", requests

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


def _client(protocol, url, *, retries=1, **kwargs):
    return LLMClient(
        provider="anthropic" if protocol == "anthropic" else "openai",
        model="claude-sonnet-4-6" if protocol == "anthropic" else "gpt-4o",
        wire_protocol="responses" if protocol == "responses" else "chat_completions",
        api_key="controlled-test", base_url=url,  # pragma: allowlist secret
        stream_chat=protocol == "chat", max_retries=retries,
        **kwargs,
    )


def _records(tmp_path):
    return [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("budget, retries, count", [(2000, 1, 1), (20000, 1, 2), (None, 1, 2), (2000, 0, 1)])
async def test_public_agent_rechecks_reported_retry_spend(tmp_path, attempt_server, protocol, budget, retries, count):
    url, requests = attempt_server(protocol)
    events = []
    result = await OpenCollab(
        tmp_path, model="routed-worker", model_routes={"routed-worker": {
            "model": "gpt-4o", "provider": "openai", "api_key": "controlled-test",  # pragma: allowlist secret
            "base_url": url, "wire_protocol": "responses" if protocol == "responses" else "chat_completions",
        }}, config={"max_output_tokens": 128, "llm_stream_chat": protocol == "chat", "llm_max_retries": retries},
    ).agent("Reply ok.", tools=[], budget=budget, trace=False, run_control=RunControl(
        on_event=lambda event: events.append(event.data) if event.type == "usage" else None,
    ))
    assert len(requests) == count
    assert result.tokens == 1500 + 4 * (count - 1)
    assert result.status == ("failed" if retries == 0 else "stopped" if count == 1 else "completed")
    if count == 1 and retries:
        assert "budget exhausted before model call" in result.reason
    records = _records(tmp_path)
    assert len(records) == len(events) == 1
    assert records[0]["usage"]["total_tokens"] == events[0]["total_tokens"] == result.tokens


@pytest.mark.parametrize("protocol, field", [("chat", "max_tokens"), ("responses", "max_output_tokens"),
                                            ("anthropic", "max_tokens")])
@pytest.mark.parametrize("change", ["reduced", "exhausted", "team", "cancel"])
@pytest.mark.parametrize("summary", [False, True])
async def test_physical_retry_observes_current_session_state(
    tmp_path, monkeypatch, local_environment_factory, attempt_server, protocol, field, change, summary,
):
    sessions = []
    url, requests = attempt_server(protocol, lambda: sessions[0].used_tokens)
    team_stop, cancel = asyncio.Event(), asyncio.Event()
    known = 0 if protocol == "anthropic" else 1500
    async with _client(protocol, url) as client:
        session = build_session(
            env=local_environment_factory(), llm=client, max_budget_tokens=20000,
            agent=Agent(name="worker", model=client.model, provider=client.provider,
                        system_prompt="Finish the task.", max_tokens_per_step=128),
            team_budget_exhausted=team_stop.is_set, default_cancel_event=cancel,
        )
        sessions.append(session)
        await session.add_user_message("Reply ok.")
        messages = list(session.messages)
        reserved = client.estimate_request_tokens(messages)

        def before_backoff(_error):
            assert session.used_tokens == known
            if change in {"reduced", "exhausted"}:
                session.max_budget_tokens = known + reserved + (37 if change == "reduced" else 0)
            elif change == "team":
                team_stop.set()
            else:
                cancel.set()
            return 0.0

        monkeypatch.setattr(retry, "extract_retry_after_seconds", before_backoff)

        async def complete_summary(messages, *, on_response, **kwargs):
            response = await client.complete(messages, **kwargs)
            on_response(response)
            return response

        if summary:
            operation = session.runner._invoke_summary(complete_summary, messages)
            if change == "reduced":
                assert (await operation).content == "ok"
            else:
                with pytest.raises(Exception) as error:
                    await operation
                assert type(error.value).__name__ == {
                    "exhausted": "_TokenBudgetStop", "team": "_TeamBudgetStop", "cancel": "_RunCancelled",
                }[change]
                if known:
                    assert error.value.usage.total_tokens == known
        else:
            assert await session.run_loop() == ("ok" if change == "reduced" else "")
            assert session.phase is (SessionPhase.DONE if change == "reduced" else SessionPhase.STOPPED)
    assert len(requests) == (2 if change == "reduced" else 1)
    if change == "reduced":
        assert requests[1]["body"][field] == 37
        assert requests[1]["observed"] == known
    assert session.used_tokens == known + (4 if change == "reduced" else 0)
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["usage"]["total_tokens"] == session.used_tokens


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("callback", ["attempt", "final", "revalidate"])
async def test_callback_errors_stop_retry_and_retain_usage(tmp_path, attempt_server, protocol, callback):
    url, requests = attempt_server(protocol)
    attempts, final, admissions, checks = [], [], [], []

    def failed_callback():
        raise RuntimeError("timeout in caller callback")

    def on_attempt(usage):
        attempts.append(usage.total_tokens)
        if callback == "attempt":
            failed_callback()

    def on_usage(usage):
        final.append(usage.total_tokens)
        if callback == "final":
            failed_callback()

    def before_request():
        checks.append(True)
        if len(checks) == 2 and callback == "revalidate":
            failed_callback()
        return 128

    async with _client(protocol, url, retries=3) as client:
        with pytest.raises(RuntimeError, match="timeout in caller callback") as error:
            await client.complete(
                [{"role": "user", "content": "Reply ok."}], before_request=before_request,
                on_attempt_usage=on_attempt, on_usage=on_usage, on_admitted=lambda: admissions.append(True),
            )
    expected = 1504 if callback == "final" else 1500
    assert len(requests) == (2 if callback == "final" else 1)
    assert attempts == [1500]
    assert final == [expected]
    assert admissions == [True]
    assert error.value.usage.total_tokens == expected
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["usage"]["total_tokens"] == expected


@pytest.mark.parametrize("callback", ["before_first", "admitted", "before_retry", "attempt", "final"])
@pytest.mark.parametrize("timeout", [None, 600.0])
@pytest.mark.parametrize("retry_budget", [0, 30])
async def test_responses_preserves_callback_timeout_identity(
    tmp_path, attempt_server, callback, timeout, retry_budget,
):
    url, requests = attempt_server("responses")
    original = TimeoutError("owned caller deadline")
    checks, attempts, finals = [], [], []

    def before_request():
        checks.append(True)
        if callback == "before_first" or callback == "before_retry" and len(checks) == 2:
            raise original
        return 128

    def on_admitted():
        if callback == "admitted":
            raise original

    def on_attempt(usage):
        attempts.append(usage.total_tokens)
        if callback == "attempt":
            raise original

    def on_usage(usage):
        finals.append(usage.total_tokens)
        if callback == "final":
            raise original

    async with _client("responses", url, request_timeout=timeout, provider_error_time_budget=retry_budget) as client:
        with pytest.raises(TimeoutError) as error:
            await client.complete(
                [{"role": "user", "content": "Reply ok."}], before_request=before_request,
                on_admitted=on_admitted, on_attempt_usage=on_attempt, on_usage=on_usage,
            )
    assert error.value is original
    count = 0 if callback in {"before_first", "admitted"} else 2 if callback == "final" else 1
    expected = 0 if not count else 1500 + 4 * (count - 1)
    assert len(requests) == count
    assert attempts == ([1500] if count else [])
    assert finals == ([expected] if count else [])
    assert getattr(getattr(error.value, "usage", None), "total_tokens", 0) == expected
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["usage"]["total_tokens"] == expected


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_workflow_observes_failure_spend_during_retry(tmp_path, monkeypatch, attempt_server, protocol):
    contexts, seen = [], []
    url, requests = attempt_server(protocol, lambda: contexts[0].tokens_spent())

    def backoff(_error):
        seen.append(contexts[0].tokens_spent())
        return 0.0

    monkeypatch.setattr(retry, "extract_retry_after_seconds", backoff)
    async with _client(protocol, url) as client:
        async def flow(ctx, _args):
            contexts.append(ctx)
            return await ctx.agent("Reply ok.", tools=[], budget=10000)

        result = await OpenCollab(tmp_path, config={"max_output_tokens": 128}).workflow(
            flow, llm=client, budget=20000, trace=False, limit_mode="explicit",
        )
    assert result.status == "completed"
    assert result.tokens == 1504
    assert seen == [1500]
    assert [request["observed"] for request in requests] == [0, 1500]


@pytest.mark.parametrize("budget, retries, count, stopped_by", [
    (2000, 1, 1, "_TokenBudgetStop"),
    (20000, 1, 1, "_TeamBudgetStop"),
    (20000, 1, 1, "_RunCancelled"),
    (20000, 1, 2, None),
    (None, 1, 2, None),
    (2000, 0, 1, None),
])
async def test_authored_subprocess_accounts_failure_before_retry(
    tmp_path, monkeypatch, attempt_server, budget, retries, count, stopped_by,
):
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    hosts = []
    url, requests = attempt_server("chat", lambda: (hosts[0].used_tokens, hosts[1].bridge.tokens))
    (tmp_path / "authored.py").write_text(
        'from opencollab import RunControl\n'
        'async def main(ctx, args):\n'
        '    events = []\n'
        '    def observe(event):\n'
        '        if event.type == "usage":\n'
        '            events.append([event.data["total_tokens"], event.data["error_type"],\n'
    '                           event.data["cache_read_tokens"]])\n'
        f'    goal = {"context " * 300!r}\n'
        f'    await ctx.agent_run(goal, tools=[], budget={budget!r}, run_control=RunControl(on_event=observe))\n'
        '    return {"workflow": ctx.tokens_spent(), "session": ctx.sessions[0].used_tokens, "events": events}\n',
        encoding="utf-8",
    )
    environment = LocalEnvironment(str(tmp_path))
    parent, tool, runtime = _manager(environment, budget=budget)
    if stopped_by == "_TeamBudgetStop":
        parent.runner._team_budget_exhausted = lambda: parent.used_tokens >= 1500
    if stopped_by == "_RunCancelled":
        def request_stop(_error):
            hosts[1].stop_requested = True
            return 0.0
        monkeypatch.setattr(retry, "extract_retry_after_seconds", request_stop)
    for target in (tool._config, parent.agent):
        target.base_url = url
        target.api_key = "controlled-test"  # pragma: allowlist secret
        target.llm_stream_chat = True
        target.llm_max_retries = retries
    parent.agent.max_tokens_per_step = 128
    hosts.append(parent)
    try:
        launched = json.loads(await tool.execute_with_runtime(
            {"action": "launch", "source": "authored.py", "python": sys.executable, "budget": budget}, runtime,
        ))
        child = tool._children[launched["id"]]
        hosts.append(child)
        await asyncio.wait_for(child.task, 20)
        assert child.result["status"] == "completed", child.result
        assert len(requests) == count
        expected = 1500 + 4 * (count - 1)
        assert child.tokens == child.bridge.tokens == parent.used_tokens == expected
        artifacts = Path(child.artifacts)
        assert json.loads((artifacts / "usage.json").read_text()) == {"tokens": expected}
        result = json.loads((artifacts / "result.json").read_text())
        assert result["tokens"] == expected
        output = result["output"]
        assert output["workflow"] == output["session"] == expected
        assert [event[0] for event in output["events"]] == [expected]
        if stopped_by is not None:
            assert output["events"] == [[expected, stopped_by, None]]
        if count == 2:
            assert requests[1]["observed"] == (1500, 1500)
        records = _records(tmp_path)
        assert len(records) == 1
        assert records[0]["usage"]["total_tokens"] == parent.used_tokens
    finally:
        await parent.aclose()
        await environment.cleanup()


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_team_observes_failure_spend_before_retry(
    tmp_path, monkeypatch, local_environment_factory, attempt_server, protocol,
):
    scheduler = Scheduler(session_factory=None, worktree_pool=None, event_sink=EventBus(), max_budget_tokens=2000)
    sibling = SessionState(messages=[], used_tokens=750)
    scheduler.table.add(SessionControlBlock(
        aid=1, parent_aid=0, agent=Agent(name="sibling", system_prompt="Finish."), state=sibling,
    ))
    url, requests = attempt_server(protocol)

    def backoff(_error):
        assert scheduler.used_tokens == 2250
        assert scheduler.budget_exhausted
        return 0.0

    monkeypatch.setattr(retry, "extract_retry_after_seconds", backoff)
    async with _client(protocol, url) as client:
        session = build_session(
            env=local_environment_factory(), llm=client, max_budget_tokens=20000,
            agent=Agent(name="worker", model="gpt-4o", system_prompt="Finish.", max_tokens_per_step=128),
            team_budget_exhausted=lambda: scheduler.budget_exhausted,
        )
        scheduler.table.add(SessionControlBlock(aid=0, parent_aid=None, agent=session.agent, state=session.state))
        await session.add_user_message("Reply ok.")
        assert await session.run_loop() == ""
    assert len(requests) == 1
    assert session.used_tokens == 1500
    assert session.phase is SessionPhase.STOPPED
    assert "team budget exceeded" in session.state.terminal_reason
    assert _records(tmp_path)[0]["usage"]["total_tokens"] == 1500
