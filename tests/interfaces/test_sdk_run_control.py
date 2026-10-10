"""Public controlled agents execute real sessions and reserve real requests."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import asdict

import pytest

from opencollab import BudgetDecision, OpenCollab, RunControl
from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.bootstrap import agent_runtime, build_session
from opencollab.bootstrap.container import _history_compaction_settings
from opencollab.domain.agent import Agent
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.runtime.test_async_compaction import history
from tests.runtime.test_compaction_accounting import AccountingModel
from tests.runtime.test_llm_chat_streaming_http import fake_chat_server


class Model:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def estimate_request_tokens(self, messages, tools, **kwargs):
        return 10 + sum(len(message.get("content", "")) for message in messages if message["role"] == "system")

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        return self.responses.pop(0) if self.responses else reply()


def reply(tokens=10, content="finished"):
    return LLMResponse(content=content, finish_reason="stop", usage=Usage(input_tokens=2, output_tokens=tokens - 2))


def client(tmp_path, **config):
    return OpenCollab(tmp_path, model="unit-model", provider="openai", config=config)


def consumption(events):
    return [event for event in events if event.type == "usage"]


@pytest.fixture(autouse=True)
def quiet_steering(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")


@pytest.mark.parametrize("trace", [False, True])
async def test_control_clips_initial_soft_allowance_and_emits_with_either_trace_mode(tmp_path, trace):
    events = []
    model = Model()
    result = await client(tmp_path).agent2(
        "finish", tools=(), system_prompt="sys", budget=100, llm=model,
        run_id="clipped", trace=trace, artifacts=tmp_path / "artifacts",
        run_control=RunControl(initial_soft_budget_tokens=200, on_event=events.append),
    )
    assert result.ok
    assert model.calls[0]["max_output_tokens"] == 87
    assert sum(event.data["total_tokens"] for event in consumption(events)) == result.tokens == 10
    assert {event.run_id for event in events} == {"clipped"}
    assert json.loads(json.dumps(asdict(consumption(events)[0])))["data"]["total_tokens"] == 10
    assert len({event.session_id for event in events}) == 1
    assert [event.type for event in events][-2:] == ["session_stopped", "cleanup_completed"]
    assert events[-2].data["pending_provider_requests"] == 0
    assert bool(list((tmp_path / "artifacts").glob("trajectory.jsonl"))) is trace


async def test_soft_allowance_extension_continues_the_same_sdk_session(tmp_path):
    events, snapshots = [], []
    model = Model([reply(tokens=100, content=""), reply()])

    async def decide(snapshot):
        snapshots.append(snapshot)
        await asyncio.sleep(0)
        return BudgetDecision(200, final_prompt="Finish from the collected evidence.")

    result = await client(tmp_path).agent2(
        "finish", tools=(), system_prompt="sys", budget=200, llm=model, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=100, decide_budget=decide, on_event=events.append),
    )
    assert result.ok and result.tokens == 110
    assert len(model.calls) == 2
    assert snapshots[0].reason == "precheck"
    assert snapshots[0].used_tokens == snapshots[0].soft_budget_tokens == 100
    assert snapshots[0].hard_budget_tokens == 200
    assert snapshots[0].steps == 1
    assert len(model.calls[1]["messages"]) > len(model.calls[0]["messages"])
    assert model.calls[1]["messages"][-1]["content"] == "Finish from the collected evidence."
    assert {event.session_id for event in events} == {snapshots[0].session_id}
    assert [event.data["total_tokens"] for event in consumption(events)] == [100, 10]


@pytest.mark.parametrize("suggestion", [90, 201, None, True])
async def test_illegal_runtime_budget_suggestions_fail_before_the_next_provider_attempt(tmp_path, suggestion):
    events = []
    model = Model([reply(tokens=100, content="")])
    result = await client(tmp_path).agent(
        "finish", tools=(), system_prompt="sys", budget=200, llm=model, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=100,
                               decide_budget=lambda _: BudgetDecision(suggestion), on_event=events.append),
    )
    assert result.status == "failed"
    assert result.tokens == 100 and len(model.calls) == 1
    assert "run-control budget policy failed" in str(result.error)
    rejected = next(event for event in events if event.type == "budget_decision")
    assert rejected.data["accepted"] is False
    assert rejected.data["suggested_budget"] == suggestion
    assert events[-1].type == "cleanup_completed"


async def test_hard_exhaustion_keeps_ordinary_budget_stop(tmp_path):
    model = Model([reply(tokens=201, content="")])
    decisions = []
    result = await client(tmp_path).agent(
        "finish", tools=(), system_prompt="sys", budget=200, llm=model, trace=False,
        run_control=RunControl(decide_budget=lambda snapshot: decisions.append(snapshot) or BudgetDecision(200)),
    )
    assert result.status == "stopped" and result.tokens == 201
    assert "budget" in result.reason and decisions == []


@pytest.mark.parametrize("too_long", [False, True])
async def test_final_prompt_is_present_in_current_http_request_and_reserves_input(tmp_path, too_long):
    events = []
    prompt = "Finish using the evidence already collected." if not too_long else "x" * 20_000
    with fake_chat_server() as (base_url, requests):
        result = await client(tmp_path, api_key="fixture-key", base_url=base_url, llm_max_retries=0).agent2(  # pragma: allowlist secret
            "finish", system_prompt="sys", tools=(), budget=500, trace=False,
            run_control=RunControl(initial_soft_budget_tokens=10,
                                   decide_budget=lambda _: BudgetDecision(500, final_prompt=prompt),
                                   on_event=events.append),
        )
    if too_long:
        assert result.status == "stopped" and requests == [] and result.tokens == 0
    else:
        assert result.ok and len(requests) == 1
        body = requests[0]
        assert any(message.get("content") == prompt for message in body["messages"])
        assert body["max_tokens"] == 500 - estimate_request_tokens(body["messages"])
    decision = next(event for event in events if event.type == "budget_decision")
    assert decision.data["accepted"] is True


async def test_summary_spend_reaches_soft_cap_and_next_request_uses_host_extension(tmp_path, monkeypatch):
    sessions, snapshots, events = [], [], []
    original = agent_runtime.build_session

    def seeded_session(**kwargs):
        session = original(**kwargs)
        session.messages = history()
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", seeded_session)
    model = AccountingModel(summary_tokens=20_000)
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), budget=40_000, llm=model, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=20_000,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(40_000),
                               on_event=events.append),
    )
    assert result.ok and result.tokens == 20_010
    assert [call["summary"] for call in model.calls] == [True, False]
    assert snapshots[-1].used_tokens == 20_000
    assert sessions[0].max_budget_tokens == sessions[0].runner.max_budget_tokens == 40_000
    assert [event.data["purpose"] for event in consumption(events)] == ["summary", "completion"]
    assert any(event.type == "context_shaping" and event.data["rung"] == "auto_compact" for event in events)


@pytest.mark.parametrize("summary_error", [False, True])
async def test_usage_on_provider_exception_is_accounted_and_emitted_once(tmp_path, monkeypatch, summary_error):
    events = []

    class ChargedError(RuntimeError):
        usage = Usage(input_tokens=13, output_tokens=24)

    class FailingModel(AccountingModel):
        async def complete(self, messages, tools=None, **kwargs):
            if not self.calls:
                self.calls.append({"failed": True})
                raise ChargedError("fixture reported usage")
            return await super().complete(messages, tools, **kwargs)

    if summary_error:
        original = agent_runtime.build_session

        def seeded_session(**kwargs):
            session = original(**kwargs)
            session.messages = history()
            return session

        monkeypatch.setattr(agent_runtime, "build_session", seeded_session)
    model = FailingModel()
    result = await client(tmp_path).agent(
        "finish", system_prompt="sys", tools=(), llm=model, trace=False,
        run_control=RunControl(on_event=events.append),
    )
    usages = consumption(events)
    assert usages[0].data["total_tokens"] == 37
    assert usages[0].data["error_type"] == "ChargedError"
    assert usages[0].data["purpose"] == ("summary" if summary_error else "completion")
    assert len([event for event in usages if event.data["error_type"] == "ChargedError"]) == 1
    assert sum(event.data["total_tokens"] for event in usages) == result.tokens
    assert result.tokens == (47 if summary_error else 37)
    assert events[-1].type == "cleanup_completed"


@pytest.mark.parametrize("late_error", [False, True])
async def test_late_usage_keeps_original_receiver_while_same_client_runs_again(tmp_path, late_error):
    first_events, second_events = [], []
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class ChargedError(RuntimeError):
        usage = Usage(input_tokens=13, output_tokens=24)

    class LateModel(Model):
        async def complete(self, messages, tools=None, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            if late_error:
                raise ChargedError("late fixture error")
            return reply(tokens=37)

    shared = client(tmp_path)
    first = asyncio.create_task(shared.agent(
        "first", system_prompt="sys", tools=(), llm=LateModel(), trace=False,
        run_id="first", timeout=0.01, cleanup_timeout=0.5,
        run_control=RunControl(on_event=first_events.append),
    ))
    await asyncio.wait_for(started.wait(), 0.5)
    await asyncio.wait_for(cancelled.wait(), 0.5)
    try:
        second = await shared.agent2(
            "second", system_prompt="sys", tools=(), llm=Model(), trace=False, run_id="second",
            run_control=RunControl(on_event=second_events.append),
        )
        assert second.ok
        assert not first.done()
        assert first_events[-1].type == "session_stopped"
        assert first_events[-1].data["pending_provider_requests"] == 1
    finally:
        release.set()
    result = await asyncio.wait_for(first, 0.5)
    assert result.tokens == 37
    assert [event.data["total_tokens"] for event in consumption(first_events)] == [37]
    assert consumption(first_events)[0].data["late"] is True
    assert {event.run_id for event in first_events} == {"first"}
    assert {event.run_id for event in second_events} == {"second"}
    assert consumption(first_events)[0].session_id != consumption(second_events)[0].session_id
    assert first_events[-1].type == "cleanup_completed"


async def test_receiver_failure_keeps_accounting_and_cleanup_and_reports_observation_error(tmp_path):
    received = []

    def failing_receiver(event):
        received.append(event)
        if event.type == "usage":
            raise RuntimeError("fixture sink failed")

    result = await client(tmp_path).agent(
        "finish", system_prompt="sys", tools=(), llm=Model(), trace=False,
        run_control=RunControl(on_event=failing_receiver),
    )
    assert result.ok and result.tokens == 10
    assert result.metrics["session_quiesced"] is True
    assert result.metrics["observation_errors"] == ["usage: RuntimeError: fixture sink failed"]
    assert received[-1].type == "cleanup_completed"


@pytest.mark.parametrize("window,cap,expected", [(40_000, None, (7_000, 5_250)),
                                               (40_000, 4_000, (4_000, 3_000)),
                                               (40_000, 9_000, (7_000, 5_250)),
                                               (None, 4_000, (120_000, 90_000))])
def test_history_cap_uses_existing_window_rules(window, cap, expected):
    settings = _history_compaction_settings(type("Window", (), {"context_window": lambda self: window})(), cap)
    assert (settings["history_trigger_tokens"], settings["history_target_tokens"]) == expected


@pytest.mark.parametrize("invalid", [0, 1, True, 1.5, "4000"])
def test_history_cap_rejects_invalid_values(invalid):
    with pytest.raises(ValueError, match="history_trigger_tokens"):
        RunControl(history_trigger_tokens=invalid)


async def test_parallel_sessions_apply_independent_history_caps_and_record_effective_values(tmp_path):
    async def run(name, cap):
        artifacts = tmp_path / name
        result = await client(tmp_path).agent2(
            "finish", system_prompt="sys", tools=(), llm=AccountingModel(), trace=True, artifacts=artifacts,
            run_control=RunControl(history_trigger_tokens=cap),
        )
        assert result.ok
        rows = [json.loads(line) for line in (artifacts / "trajectory.jsonl").read_text().splitlines()]
        return next(row["payload"] for row in rows if row["type"] == "session.history_compaction")

    left, right = await asyncio.gather(run("left", 4_000), run("right", 6_000))
    assert (left["history_trigger_tokens"], left["history_target_tokens"]) == (4_000, 3_000)
    assert (right["history_trigger_tokens"], right["history_target_tokens"]) == (6_000, 4_500)
    assert left["history_thresholds_from"] == right["history_thresholds_from"] == "context_window_with_run_control"


async def test_policy_can_hold_soft_cap_and_stop_after_retaining_prior_usage(tmp_path):
    snapshots = []
    model = Model([reply(tokens=100, content="")])
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), budget=200, llm=model, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=100,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(100)),
    )
    assert result.status == "stopped" and result.tokens == 100
    assert len(model.calls) == len(snapshots) == 1
    assert snapshots[0].reason == "precheck"


async def test_completed_summary_at_precheck_can_extend_before_local_stop():
    events, snapshots = [], []
    model = AccountingModel(summary_tokens=20_000)
    session = build_session(
        agent=Agent(name="summary-precheck", system_prompt="sys"), llm=model, max_budget_tokens=40_000,
        run_control=RunControl(initial_soft_budget_tokens=20_000, on_event=events.append,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(40_000)),
    )
    session.messages = history()
    await session.runner._shape_and_trace(session.messages)
    assert session.used_tokens == 20_000
    assert await session.run_loop() == "answer"
    await session.aclose()
    assert snapshots[0].reason == "precheck"
    assert [call["summary"] for call in model.calls] == [True, False]
    assert sum(event.data["total_tokens"] for event in consumption(events)) == session.used_tokens == 20_010


@pytest.mark.parametrize("retry", ["context", "tool_choice"])
async def test_each_retry_reserves_again_and_reports_each_attempt_usage(tmp_path, monkeypatch, retry):
    events, snapshots = [], []

    class Rejected(RuntimeError):
        status_code = 400
        usage = Usage(input_tokens=13, output_tokens=24)

    class RetryModel(Model):
        async def complete(self, messages, tools=None, **kwargs):
            if not self.calls:
                self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
                raise Rejected("maximum context length exceeded" if retry == "context" else "tool_choice unsupported")
            return await super().complete(messages, tools=tools, **kwargs)

    if retry == "tool_choice":
        original = agent_runtime.build_session

        def forced_session(**kwargs):
            kwargs["agent"].tool_choice = "required"
            return original(**kwargs)

        monkeypatch.setattr(agent_runtime, "build_session", forced_session)
    model = RetryModel()
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=model, budget=100, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=50, on_event=events.append,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(100)),
    )
    assert result.ok and result.tokens == 47
    assert len(model.calls) == 2
    assert snapshots[0].used_tokens == 37 and snapshots[0].reason == "request"
    assert snapshots[0].reserved_input_tokens == 13
    assert [event.data["total_tokens"] for event in consumption(events)] == [37, 10]
    if retry == "context":
        assert any(event.type == "context_shaping" and event.data["emergency"] for event in events)
    else:
        assert [call["tool_choice"] for call in model.calls] == ["required", "auto"]


@pytest.mark.parametrize("explicit_budget", [False, True])
async def test_run_control_obeys_single2_unbounded_configuration_priority(tmp_path, monkeypatch, explicit_budget):
    monkeypatch.setenv("OPENCOLLAB_UNBOUNDED_LIMITS", "true")
    snapshots = []
    model = Model()
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=model, trace=False,
        **({"budget": 200} if explicit_budget else {}),
        run_control=RunControl(initial_soft_budget_tokens=10,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(100)),
    )
    assert result.ok
    assert snapshots[0].hard_budget_tokens == (200 if explicit_budget else None)
    assert model.calls[0]["max_output_tokens"] == 87


@pytest.mark.parametrize("late_error", [False, True])
async def test_late_summary_accounting_retains_original_receiver(tmp_path, monkeypatch, late_error):
    events = []
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = agent_runtime.build_session

    def seeded_session(**kwargs):
        session = original(**kwargs)
        session.messages = history()
        return session

    monkeypatch.setattr(agent_runtime, "build_session", seeded_session)

    class ChargedError(RuntimeError):
        usage = Usage(input_tokens=13, output_tokens=24)

    class LateSummary(AccountingModel):
        async def complete(self, messages, tools=None, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            if late_error:
                raise ChargedError("late summary fixture error")
            return await super().complete(messages, tools, **kwargs)

    task = asyncio.create_task(client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=LateSummary(summary_tokens=37),
        trace=False, timeout=0.01, cleanup_timeout=0.5,
        run_control=RunControl(on_event=events.append),
    ))
    await asyncio.wait_for(started.wait(), 0.5)
    await asyncio.wait_for(cancelled.wait(), 0.5)
    release.set()
    result = await asyncio.wait_for(task, 0.5)
    assert result.tokens == 37
    usages = consumption(events)
    assert len(usages) == 1
    assert usages[0].data["purpose"] == "summary" and usages[0].data["late"] is True
    assert events[-1].type == "cleanup_completed"


async def test_parallel_sdk_calls_own_their_tool_cancellation_cleanup_timeout(tmp_path, monkeypatch):
    observed = {}
    original = agent_runtime.build_session

    def observed_session(**kwargs):
        session = original(**kwargs)
        observed[session.run_id] = session.tool_execution._cancellation_cleanup_timeout
        return session

    monkeypatch.setattr(agent_runtime, "build_session", observed_session)
    shared = client(tmp_path)
    results = await asyncio.gather(*(shared.agent2(
        "finish", system_prompt="sys", tools=(), llm=Model(), trace=False, run_id=name,
        run_control=RunControl(tool_cancellation_cleanup_timeout=timeout),
    ) for name, timeout in [("long-cleanup", 10), ("short-cleanup", 0.25)]))
    assert all(result.ok for result in results)
    assert observed == {"long-cleanup": 10, "short-cleanup": 0.25}


@pytest.mark.parametrize("invalid", [0, -1, True, float("nan"), float("inf"), "10"])
def test_tool_cleanup_timeout_rejects_invalid_values(invalid):
    with pytest.raises(ValueError, match="tool_cancellation_cleanup_timeout"):
        RunControl(tool_cancellation_cleanup_timeout=invalid)


async def test_output_minimum_is_exposed_before_a_request_that_cannot_fit(tmp_path):
    snapshots = []

    class RequiredOutput(Model):
        def minimum_output_tokens(self, **kwargs):
            return 20

    model = RequiredOutput()
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=model, budget=100, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=30,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(30)),
    )
    assert result.status == "stopped" and model.calls == []
    assert snapshots[0].reserved_input_tokens == 13
    assert snapshots[0].minimum_output_tokens == 20
    assert "less than 20 output tokens" in result.reason


async def test_policy_callback_exception_preserves_accounted_usage_and_finishes_cleanup(tmp_path):
    events = []

    def failed_policy(snapshot):
        raise RuntimeError("fixture policy failed")

    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=Model([reply(tokens=100, content="")]),
        budget=200, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=100, decide_budget=failed_policy, on_event=events.append),
    )
    assert result.status == "failed" and result.tokens == 100
    assert "fixture policy failed" in str(result.error)
    assert [event.data["total_tokens"] for event in consumption(events)] == [100]
    assert events[-1].type == "cleanup_completed"


async def test_holding_soft_cap_after_response_overspend_keeps_ordinary_budget_stop(tmp_path):
    snapshots = []
    model = Model([reply(tokens=101)])
    result = await client(tmp_path).agent2(
        "finish", system_prompt="sys", tools=(), llm=model, budget=200, trace=False,
        run_control=RunControl(initial_soft_budget_tokens=100,
                               decide_budget=lambda snapshot: snapshots.append(snapshot) or BudgetDecision(100)),
    )
    assert result.status == "stopped" and result.tokens == 101
    assert result.error is None and "budget exceeded after model call" in result.reason
    assert len(model.calls) == 1
    assert snapshots[0].reason == "after_response"
    assert snapshots[0].soft_budget_tokens == 100 and snapshots[0].used_tokens == 101
