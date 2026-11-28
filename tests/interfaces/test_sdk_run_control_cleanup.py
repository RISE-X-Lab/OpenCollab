"""Run receivers remain attached until session-owned cleanup settles."""

from __future__ import annotations

import asyncio
import threading

import pytest

from opencollab import OpenCollab, RunControl, RunError
from opencollab.adapters.storage import SessionStore
from opencollab.bootstrap import agent_runtime, build_session, container, load_session
from opencollab.domain.agent import Agent
from tests.support.environment_test_support import local_environment_factory as local_environment_factory
from tests.support.session_characterization_test_support import (
    FakeLLMClient,
    FakeTool,
    llm_response,
    tool_call,
)


class Model(FakeLLMClient):
    async def complete(self, messages, tools=None, temperature=0.0, **kwargs):
        return await super().complete(messages, tools, temperature)


@pytest.mark.parametrize("delayed_cleanup", [False, True])
async def test_cleanup_event_follows_real_tool_finally(tmp_path, monkeypatch, delayed_cleanup):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events, sessions = [], []
    cleanup_file = tmp_path / "resource-closed.txt"
    original = agent_runtime.build_session

    def observed_session(**kwargs):
        session = original(**kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(agent_runtime, "build_session", observed_session)

    class Tool(FakeTool):
        async def execute_with_runtime(self, args, runtime):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                while delayed_cleanup and not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                cleanup_file.write_text("closed")
                finished.set()

    def observe(event):
        events.append((
            event, finished.is_set(), cleanup_file.exists(),
            bool(sessions[0].pending_cleanup_tasks),
        ))

    model = Model([llm_response(tool_calls=[tool_call(name="cleanup_tool", arguments="{}")])])
    owner = asyncio.create_task(OpenCollab(tmp_path, model="fixture").agent2(
        "run the tool", system_prompt="sys", tools=[Tool("cleanup_tool")],
        llm=model, trace=False, timeout=0.1, cleanup_timeout=0.01,
        run_control=RunControl(on_event=observe, tool_cancellation_cleanup_timeout=0.01),
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if delayed_cleanup:
            with pytest.raises(RunError, match="quiescent terminal state"):
                await asyncio.wait_for(asyncio.shield(owner), 2)
            assert not finished.is_set() and not cleanup_file.exists()
            assert sessions[0].pending_cleanup_tasks
            assert not any(event.type == "cleanup_completed" for event, *_ in events)
            await sessions[0].aclose()
            assert not any(event.type == "cleanup_completed" for event, *_ in events)
        else:
            result = await asyncio.wait_for(asyncio.shield(owner), 2)
            assert result.status == "stopped"
            assert result.reason == "timeout"
            assert result.metrics["cleanup_quiesced"] is True
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(owner, *sessions[0].pending_cleanup_tasks, return_exceptions=True)
        await asyncio.sleep(0)

    completed = [item for item in events if item[0].type == "cleanup_completed"]
    assert len(completed) == 1
    assert completed[0][1:] == (True, True, False)
    assert events[-1] == completed[0]
    assert completed[0][0].data["pending_provider_requests"] == 0
    await sessions[0].aclose()
    assert len([event for event, *_ in events if event.type == "cleanup_completed"]) == 1


async def test_failed_owned_provider_close_retains_error_without_cleanup_event(tmp_path, monkeypatch):
    events, models = [], []

    class FailingCloseModel(Model):
        def __init__(self, **kwargs):
            super().__init__([llm_response(content="finished")])
            self.close_calls = 0
            models.append(self)

        async def close(self):
            self.close_calls += 1
            raise RuntimeError("provider close failed")

    monkeypatch.setattr(container, "LLMClient", FailingCloseModel)
    with pytest.raises(RunError, match="cleanup or persistence"):
        await OpenCollab(tmp_path, model="fixture").agent2(
            "finish", system_prompt="sys", tools=(), trace=False, cleanup_timeout=0.01,
            run_control=RunControl(on_event=events.append),
        )
    assert models[0].close_calls == 1
    errors = [event for event in events if event.type == "error"]
    assert errors[-1].data["message"] == "provider close failed"
    assert not any(event.type == "cleanup_completed" for event in events)


async def test_late_provider_usage_keeps_receiver_through_new_subscriber_work(
    local_environment_factory, tmp_path, monkeypatch,
):
    started, release = asyncio.Event(), asyncio.Event()
    save_started, save_release = threading.Event(), threading.Event()
    events = []
    resumed = []
    path = tmp_path / "session.json"

    class LateModel(Model):
        async def complete(self, messages, tools=None, **kwargs):
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return llm_response(content="late answer", input_tokens=13, output_tokens=24)

    def observe(event):
        events.append(event)
        if event.type == "cleanup_completed":
            resumed.append(load_session(
                str(path), agent=session.agent, env=session.env, llm=Model(),
                auto_save_path=str(path),
            ))

    session = build_session(
        env=local_environment_factory(), agent=Agent(name="late-provider", system_prompt="sys"), llm=LateModel(),
        auto_save_path=str(path), run_control=RunControl(on_event=observe),
    )
    await session.add_user_message("finish")
    owner = asyncio.create_task(session.run_loop())
    try:
        await asyncio.wait_for(started.wait(), 1)
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 1)
        assert session.runner.pending_cleanup_tasks

        prepare = session._auto_save_subscriber._prepare

        def delayed_prepare():
            operation = prepare()

            def save():
                save_started.set()
                assert save_release.wait(5)
                operation()

            return save

        monkeypatch.setattr(session._auto_save_subscriber, "_prepare", delayed_prepare)
        await session.aclose()
        assert events[-1].type == "session_stopped"
        assert events[-1].data["pending_provider_requests"] == 1
        release.set()
        assert await asyncio.to_thread(save_started.wait, 1)
        assert session.runner.pending_cleanup_tasks == ()
        assert session.event_bus.pending_tasks
        usages = [event for event in events if event.type == "usage"]
        assert len(usages) == 1 and usages[0].data["late"] is True
        assert usages[0].data["total_tokens"] == session.used_tokens == 37
        assert not any(event.type == "cleanup_completed" for event in events)
    finally:
        release.set()
        save_release.set()
        await asyncio.gather(owner, return_exceptions=True)
        while session.pending_cleanup_tasks:
            await asyncio.wait_for(
                asyncio.gather(*session.pending_cleanup_tasks, return_exceptions=True), 1,
            )
        await asyncio.sleep(0)

    snapshot = SessionStore().load_snapshot(str(path), session.agent.system_prompt)
    assert snapshot["session_state"]["used_tokens"] == 37
    assert events[-1].type == "cleanup_completed"
    recovered, = resumed
    assert recovered.used_tokens == 37
    await recovered.aclose()
    assert len([event for event in events if event.type == "cleanup_completed"]) == 1
