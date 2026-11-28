"""Returned summary usage survives owned-client cleanup windows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opencollab.application.session_run import GenerationTimeoutError
from opencollab.bootstrap import build_session, container
from opencollab.domain.agent import Agent
from tests.runtime.test_async_compaction import history
from tests.runtime.test_compaction_accounting import AccountingModel


@pytest.mark.parametrize("deadline", [None, 0.02])
def test_returned_summary_usage_survives_cancellation_during_owned_client_close(monkeypatch, deadline):
    clients = []
    traces = []

    class PausedCloseModel(AccountingModel):
        def __init__(self, **kwargs):
            super().__init__()
            self.number = len(clients)
            self.started_close = asyncio.Event()
            self.close_exited = False
            clients.append(self)

        async def close(self):
            try:
                if self.number == 1:
                    self.started_close.set()
                    await asyncio.Event().wait()
            finally:
                self.close_exited = True

    monkeypatch.setattr(container, "LLMClient", PausedCloseModel)
    tracer = SimpleNamespace(log_step=lambda **row: traces.append(row), flush=lambda: None)
    session = build_session(agent=Agent(name="close-window", system_prompt="system"),
                            llm_timeout=deadline, tracer=tracer)
    session.messages = history()
    session.state.set_context_tokens(23)

    async def scenario():
        owner = asyncio.create_task(session.run_loop())
        async def wait_for_summary_client():
            while len(clients) < 2:
                await asyncio.sleep(0)

        try:
            await asyncio.wait_for(wait_for_summary_client(), 0.2)
            await asyncio.wait_for(clients[1].started_close.wait(), 0.2)
            assert session.used_tokens == 7_600
            if deadline is None:
                owner.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(owner, 0.2)
            else:
                with pytest.raises(GenerationTimeoutError):
                    await asyncio.wait_for(owner, 0.2)
        finally:
            owner.cancel()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 0.2)
            await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True), 0.2)
            await session.aclose()
            await session.env.cleanup()

    asyncio.run(scenario())
    assert session.used_tokens == 7_600
    assert session.state.context_tokens == 23
    assert session.runner.late_provider_usage == ()
    assert session.pending_cleanup_tasks == ()
    assert clients[1].close_exited is True
    assert clients[0].calls == []
    calls = [row for row in traces if row["step_type"] == "llm_call"]
    assert len(calls) == 1
    assert calls[0]["tokens"] == 7_600
    assert calls[0]["payload"]["purpose"] == "summary"
    assert not calls[0]["payload"].get("late")

def test_returned_summary_usage_survives_owned_client_close_failure(monkeypatch):
    clients = []

    class FailingCloseModel(AccountingModel):
        def __init__(self, **kwargs):
            super().__init__()
            self.number = len(clients)
            clients.append(self)

        async def close(self):
            if self.number == 1:
                raise RuntimeError("fixture close failed")

    monkeypatch.setattr(container, "LLMClient", FailingCloseModel)
    session = build_session(agent=Agent(name="failed-close", system_prompt="system"))
    session.messages = history()
    async def scenario():
        assert await asyncio.wait_for(session.run_loop(), 0.2) == "answer"
        await session.aclose()
        await session.env.cleanup()

    asyncio.run(scenario())
    assert session.used_tokens == 7_610
    assert len(clients[1].calls) == 1
    assert session.pending_cleanup_tasks == ()

def test_late_summary_usage_is_charged_once_before_owned_client_close_finishes(monkeypatch):
    clients = []

    class LateCloseModel(AccountingModel):
        def __init__(self, **kwargs):
            super().__init__()
            self.number = len(clients)
            self.started_close = asyncio.Event()
            self.release_close = asyncio.Event()
            clients.append(self)

        async def complete(self, messages, tools=None, **kwargs):
            try:
                await asyncio.sleep(0.15)
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
            return await super().complete(messages, tools, **kwargs)

        async def close(self):
            if self.number == 1:
                self.started_close.set()
                await self.release_close.wait()

    monkeypatch.setattr(container, "LLMClient", LateCloseModel)
    session = build_session(agent=Agent(name="late-close", system_prompt="system"), llm_timeout=0.01)
    session.messages = history()

    async def scenario():
        try:
            with pytest.raises(GenerationTimeoutError):
                await asyncio.wait_for(session.run_loop(), 0.2)
            await asyncio.wait_for(clients[1].started_close.wait(), 0.2)
            assert session.used_tokens == 7_600
            assert session.runner.late_provider_usage == (7_600,)
            assert session.pending_cleanup_tasks
        finally:
            clients[1].release_close.set()
            await asyncio.wait_for(asyncio.gather(*session.pending_cleanup_tasks), 0.2)
            await session.aclose()
            await session.env.cleanup()

    asyncio.run(scenario())
    assert session.used_tokens == 7_600
    assert session.runner.late_provider_usage == (7_600,)
    assert session.pending_cleanup_tasks == ()
