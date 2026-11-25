"""Compaction calls share session budget, usage and provider traces."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from opencollab.adapters.llm.types import LLMResponse, Usage
from opencollab.adapters.trace import Tracer
from opencollab.bootstrap import build_session
from opencollab.bootstrap.context_policy import ContextPolicy
from opencollab.domain.agent import Agent
from opencollab.domain.session import SessionPhase
from opencollab.domain.token_estimation import estimate_request_tokens
from tests.runtime.test_async_compaction import history
from tests.support.environment_test_support import local_environment_factory as local_environment_factory


class AccountingModel:
    def __init__(self, summary_content="<summary>Preserved context.</summary>", summary_tokens=7_600):
        self.summary_content = summary_content
        self.summary_tokens = summary_tokens
        self.calls = []

    def context_window(self):
        return 40_000

    async def complete(self, messages, tools=None, **kwargs):
        summary = "Your task is to create a detailed summary" in messages[-1]["content"]
        self.calls.append({"summary": summary, "messages": copy.deepcopy(messages), "kwargs": kwargs})
        return LLMResponse(
            content=self.summary_content if summary else "answer",
            finish_reason="stop",
            usage=Usage(input_tokens=self.summary_tokens - 2 if summary else 8, output_tokens=2,
                        cache_read_tokens=20 if summary else 0, reasoning_tokens=1 if summary else None,
                        raw_usage={"provider_input": self.summary_tokens - 2} if summary else {}),
            reasoning="provider reasoning" if summary else None,
            provider_model="fixture-provider-model",
            transport_timing={"first_token_seconds": 0.001},
        )

@pytest.fixture(autouse=True)
def disable_steering(monkeypatch):
    monkeypatch.setenv("OPENCOLLAB_BUDGET_NUDGE_MODE", "off")
    monkeypatch.setenv("OPENCOLLAB_WRITE_NUDGE_MODE", "off")

@pytest.mark.parametrize("budget", [30_000, None])
@pytest.mark.parametrize("content", ["<summary>Preserved context.</summary>", "empty parsed summary"])
def test_summary_usage_and_native_trace_fields_are_recorded(local_environment_factory, tmp_path, budget, content):
    model = AccountingModel(content)
    tracer = Tracer(run_id="summary-usage", output_dir=str(tmp_path))
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="accounting", system_prompt="system"), llm=model,
                            tracer=tracer, max_budget_tokens=budget)
    session.messages = history()
    try:
        assert asyncio.run(session.run_loop()) == "answer"
        tracer.flush()
    finally:
        tracer.close()
    assert session.used_tokens == 7_610
    assert [call["summary"] for call in model.calls] == [True, False]
    with open(tracer.path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    calls = [row for row in rows if row["type"] == "llm_call"]
    assert len(calls) == 2
    summary = next(row for row in calls if row["payload"].get("purpose") == "summary")
    assert summary["metrics"]["tokens"] == 7_600
    assert not summary["payload"].get("late")
    assert summary["payload"]["usage"]["input_tokens"] == 7_598
    assert summary["payload"]["usage"]["cache_read_tokens"] == 20
    assert summary["payload"]["usage"]["raw_usage"] == {"provider_input": 7_598}
    assert summary["payload"]["usage"]["reasoning_tokens"] == 1
    assert summary["payload"]["reasoning"] == "provider reasoning"
    assert summary["payload"]["provider_model"] == "fixture-provider-model"
    assert summary["payload"]["transport_timing"] == {"first_token_seconds": 0.001}
    assert summary["payload"]["request_tool_names"] == []
    assert summary["payload"]["request_tool_choice"] is None
    terminal = next(row for row in rows if row["type"] == "session_terminal")
    assert terminal["payload"]["used_tokens"] == 7_610

@pytest.mark.parametrize("long", [False, True])
@pytest.mark.parametrize("already_spent", [4_900, 5_000])
def test_remaining_budget_stops_before_any_model_request(local_environment_factory, long, already_spent):
    model = AccountingModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="low-budget", system_prompt="system"), llm=model,
                            max_budget_tokens=5_000)
    session.messages = history(long)
    session.used_tokens = already_spent
    asyncio.run(session.run_loop())
    assert model.calls == []
    assert session.used_tokens == already_spent
    assert session.phase is SessionPhase.STOPPED
    assert "budget" in session.state.terminal_reason

def test_summary_reserves_input_and_limits_output(local_environment_factory):
    model = AccountingModel(summary_tokens=20)
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="bounded", system_prompt="system", max_tokens_per_step=50_000),
                            llm=model, max_budget_tokens=20_000)
    session.messages = history()
    asyncio.run(session.run_loop())
    summary, answer = model.calls
    assert summary["summary"] is True
    assert summary["kwargs"]["max_output_tokens"] == 20_000 - estimate_request_tokens(summary["messages"])
    assert answer["kwargs"]["max_output_tokens"] == 19_980 - estimate_request_tokens(answer["messages"])

def test_successful_summary_cache_reuse_charges_once(local_environment_factory):
    model = AccountingModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="cached", system_prompt="system"), llm=model)
    session.messages = history()

    async def scenario():
        first = await session.runner._shape_and_trace(session.messages)
        second = await session.runner._shape_and_trace(session.messages)
        assert first == second
        assert session.used_tokens == 7_600
        assert session.state.context_tokens == 7_598
        assert await session.run_loop() == "answer"

    asyncio.run(scenario())
    assert [call["summary"] for call in model.calls] == [True, False]
    assert session.used_tokens == 7_610

def test_summary_overspend_stops_before_an_answer_request(local_environment_factory):
    model = AccountingModel(summary_tokens=40_000)
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="overspend", system_prompt="system"), llm=model,
                            max_budget_tokens=30_000)
    session.messages = history()
    asyncio.run(session.run_loop())
    assert [call["summary"] for call in model.calls] == [True]
    assert session.used_tokens == 40_000
    assert session.phase is SessionPhase.STOPPED

def test_disabled_history_compaction_has_only_answer_usage(local_environment_factory):
    model = AccountingModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="disabled", system_prompt="system"), llm=model,
                            context_policy=ContextPolicy(name="no_history_compaction"))
    session.messages = history()
    asyncio.run(session.run_loop())
    assert [call["summary"] for call in model.calls] == [False]
    assert session.used_tokens == 10

@pytest.mark.parametrize("deadline", [None, 0.01])
def test_late_summary_response_is_owned_and_charged_once(local_environment_factory, deadline):
    from opencollab.application.session_run import GenerationTimeoutError

    class LateModel(AccountingModel):
        async def complete(self, messages, tools=None, **kwargs):
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
            return await super().complete(messages, tools, **kwargs)

    model = LateModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="late", system_prompt="system"), llm=model, llm_timeout=deadline)
    session.messages = history()

    async def scenario():
        task = asyncio.create_task(session.run_loop())
        if deadline is None:
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.1)
        else:
            with pytest.raises(GenerationTimeoutError):
                await asyncio.wait_for(task, 0.1)
        assert session.pending_cleanup_tasks
        await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks), 0.1)

    asyncio.run(scenario())
    assert [call["summary"] for call in model.calls] == [True]
    assert session.used_tokens == 7_600
    assert session.runner.late_provider_usage == (7_600,)
    assert session.pending_cleanup_tasks == ()

def test_summary_reservation_uses_the_provider_estimator_with_actual_thinking_controls(local_environment_factory):
    class EstimatingModel(AccountingModel):
        def __init__(self):
            super().__init__(summary_tokens=20)
            self.estimates = []

        def estimate_request_tokens(self, messages, tools=None, **kwargs):
            summary = "Your task is to create a detailed summary" in messages[-1]["content"]
            self.estimates.append({"summary": summary, "kwargs": kwargs})
            return 200 if summary else 120

    model = EstimatingModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="estimation", system_prompt="system", thinking=True,
                    thinking_params={"fixture": True}, max_tokens_per_step=50_000),
        llm=model, max_budget_tokens=1_000,
    )
    session.messages = history()
    asyncio.run(session.run_loop())
    assert model.calls[0]["kwargs"]["max_output_tokens"] == 800
    assert model.calls[1]["kwargs"]["max_output_tokens"] == 860
    summary_estimates = [item for item in model.estimates if item["summary"]]
    assert summary_estimates
    assert all(item["kwargs"] == {"thinking": False, "thinking_params": None} for item in summary_estimates)
    assert [item for item in model.estimates if not item["summary"]] == [
        {"summary": False, "kwargs": {"thinking": True, "thinking_params": {"fixture": True}}},
    ]

def test_forced_overflow_summary_uses_the_same_usage_accounting(local_environment_factory):
    class OverflowModel(AccountingModel):
        async def complete(self, messages, tools=None, **kwargs):
            if not self.calls:
                self.calls.append({"summary": False})
                raise ValueError("fixture context overflow")
            return await super().complete(messages, tools, **kwargs)

    model = OverflowModel()
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="forced-usage", system_prompt="system"), llm=model)
    session.messages = history(False)
    session.runner._is_context_overflow = lambda error: isinstance(error, ValueError)
    assert asyncio.run(session.run_loop()) == "answer"
    assert [call["summary"] for call in model.calls] == [False, True, False]
    assert session.used_tokens == 7_610

@pytest.mark.parametrize("sibling_spends_during_summary", [False, True])
@pytest.mark.parametrize("team_allowance", ["exhausted", "available", None])
def test_answer_rechecks_shared_team_allowance_after_summary(
    local_environment_factory,
    sibling_spends_during_summary, team_allowance,
):
    cap = (30 if sibling_spends_during_summary else 20) if team_allowance == "exhausted" else 1_000

    def team_exhausted():
        return session.used_tokens + sibling.used_tokens >= cap

    guard = None if team_allowance is None else team_exhausted
    sibling = build_session(
        env=local_environment_factory(),
        agent=Agent(name="sibling", system_prompt="system"),
        llm=AccountingModel(summary_tokens=20), team_budget_exhausted=guard,
    )

    class SharedAccountingModel(AccountingModel):
        async def complete(self, messages, tools=None, **kwargs):
            if sibling_spends_during_summary and "Your task is to create a detailed summary" in messages[-1]["content"]:
                await sibling.add_user_message("Complete the independent task.")
                assert await sibling.run_loop() == "answer"
            return await super().complete(messages, tools, **kwargs)

    model = SharedAccountingModel(summary_tokens=20)
    session = build_session(
        env=local_environment_factory(),
        agent=Agent(name="team-summary", system_prompt="system"), llm=model,
        max_budget_tokens=30_000, team_budget_exhausted=guard,
    )
    session.messages = history()
    answer = asyncio.run(session.run_loop())
    if team_allowance == "exhausted":
        assert [call["summary"] for call in model.calls] == [True]
        assert session.used_tokens == 20
        assert answer == ""
        assert session.phase is SessionPhase.STOPPED
        assert "team budget exceeded" in session.state.terminal_reason
    else:
        assert [call["summary"] for call in model.calls] == [True, False]
        assert session.used_tokens == 30
        assert answer == "answer"
        assert session.phase is SessionPhase.DONE
    assert sibling.used_tokens == (10 if sibling_spends_during_summary else 0)
