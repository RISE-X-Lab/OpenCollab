"""Lifecycle tests for the shared bootstrap Agent runtime."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from opencollab.bootstrap import agent_runtime
from opencollab.bootstrap.agent_runtime import AgentRuntimeLifecycleError
from opencollab.domain.agent import Agent


class Environment:
    def __init__(
        self,
        *,
        abort_fails: bool = False,
        revoke_fails: bool = False,
        block_abort: bool = False,
    ) -> None:
        self.revoked = False
        self.abort_fails = abort_fails
        self.revoke_fails = revoke_fails
        self.abort_calls = 0
        self.cleanup_calls = 0
        self.block_abort = block_abort
        self.abort_started = asyncio.Event()
        self.abort_release = asyncio.Event()

    def revoke(self) -> None:
        if self.revoke_fails:
            raise RuntimeError("revoke failed")
        self.revoked = True

    async def abort(self) -> None:
        self.abort_calls += 1
        self.revoked = True
        self.abort_started.set()
        if self.block_abort:
            await self.abort_release.wait()
        if self.abort_fails:
            raise RuntimeError("abort failed")

    async def cleanup(self) -> None:
        self.cleanup_calls += 1


class Session:
    def __init__(self, *, outcome: str = "complete", auto_save_path: str | None = None) -> None:
        self.outcome = outcome
        self.auto_save_path = auto_save_path
        self.persistence_errors: tuple[str, ...] = ()
        self.pending_cleanup_tasks: tuple[asyncio.Task, ...] = ()
        self.phase = SimpleNamespace(value="done")
        self.state = SimpleNamespace(terminal_reason="completed")
        self.used_tokens = 7
        self.step_count = 2
        self.started = asyncio.Event()
        self.save_calls = 0

    async def add_user_message(self, _prompt: str) -> None:
        return None

    async def run_loop(self) -> str:
        self.started.set()
        if self.outcome == "block":
            await asyncio.Event().wait()
        if self.outcome == "fail":
            self.phase.value = "error"
            self.state.terminal_reason = "provider failed"
            raise RuntimeError("provider failed")
        return "done"

    def enqueue_auto_save(self):
        self.save_calls += 1
        if self.auto_save_path is None:
            return None
        return asyncio.create_task(asyncio.sleep(0))


def _agent() -> Agent:
    return Agent(name="agent", system_prompt="prompt", model="model", provider="provider")


def _patch_session(monkeypatch, session: Session) -> None:
    monkeypatch.setattr(agent_runtime, "build_session", lambda **_kwargs: session)


async def test_agent_runtime_returns_metrics_after_final_save(monkeypatch) -> None:
    session = Session(auto_save_path="agent.json")
    _patch_session(monkeypatch, session)
    environment = Environment()
    result = await agent_runtime.run_agent(
        agent=_agent(),
        environment=environment,
        prompt="run",
        max_tokens=100,
        max_steps=5,
        timeout_seconds=None,
        cleanup_timeout_seconds=0.1,
        transcript_path="agent.json",
        cleanup_environment=True,
    )
    assert result.output == "done"
    assert result.outcome == "completed"
    assert result.tokens_spent == 7
    assert result.step_count == 2
    assert result.cleanup_quiesced
    assert environment.cleanup_calls == 1


async def test_agent_runtime_returns_quiescent_execution_failure(monkeypatch) -> None:
    session = Session(outcome="fail")
    _patch_session(monkeypatch, session)
    result = await agent_runtime.run_agent(
        agent=_agent(),
        environment=Environment(),
        prompt="run",
        max_tokens=100,
        max_steps=5,
        timeout_seconds=None,
        cleanup_timeout_seconds=0.1,
        transcript_path=None,
    )
    assert result.outcome == "failed"
    assert isinstance(result.error, RuntimeError)
    assert result.phase == "error"


async def test_agent_runtime_timeout_revokes_aborts_and_quiesces(monkeypatch) -> None:
    session = Session(outcome="block")
    _patch_session(monkeypatch, session)
    environment = Environment()
    result = await agent_runtime.run_agent(
        agent=_agent(),
        environment=environment,
        prompt="run",
        max_tokens=100,
        max_steps=5,
        timeout_seconds=0.01,
        cleanup_timeout_seconds=0.1,
        transcript_path=None,
    )
    assert result.outcome == "timed_out"
    assert result.cleanup_quiesced
    assert environment.revoked
    assert environment.abort_calls == 1


async def test_agent_runtime_timeout_fails_when_abort_fails(monkeypatch) -> None:
    session = Session(outcome="block")
    _patch_session(monkeypatch, session)
    with pytest.raises(AgentRuntimeLifecycleError, match="quiescent"):
        await agent_runtime.run_agent(
            agent=_agent(),
            environment=Environment(abort_fails=True),
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=0.01,
            cleanup_timeout_seconds=0.1,
            transcript_path=None,
        )


async def test_agent_runtime_timeout_still_aborts_when_revoke_fails(monkeypatch) -> None:
    session = Session(outcome="block")
    _patch_session(monkeypatch, session)
    environment = Environment(revoke_fails=True)
    with pytest.raises(AgentRuntimeLifecycleError, match="quiescent"):
        await agent_runtime.run_agent(
            agent=_agent(),
            environment=environment,
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=0.01,
            cleanup_timeout_seconds=0.1,
            transcript_path=None,
        )
    assert environment.abort_calls == 1


async def test_agent_runtime_caller_cancellation_still_aborts(monkeypatch) -> None:
    session = Session(outcome="block")
    _patch_session(monkeypatch, session)
    environment = Environment()
    owner = asyncio.create_task(
        agent_runtime.run_agent(
            agent=_agent(),
            environment=environment,
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=None,
            cleanup_timeout_seconds=0.1,
            transcript_path=None,
        )
    )
    await session.started.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert environment.revoked
    assert environment.abort_calls == 1


async def test_agent_runtime_double_cancellation_finishes_abort_and_save(monkeypatch) -> None:
    session = Session(outcome="block")
    _patch_session(monkeypatch, session)
    environment = Environment(block_abort=True)
    owner = asyncio.create_task(
        agent_runtime.run_agent(
            agent=_agent(),
            environment=environment,
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=None,
            cleanup_timeout_seconds=0.1,
            transcript_path=None,
        )
    )
    await session.started.wait()
    owner.cancel()
    await environment.abort_started.wait()
    owner.cancel()
    environment.abort_release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert environment.abort_calls == 1
    assert session.save_calls == 1


async def test_agent_runtime_rejects_missing_final_save_owner(monkeypatch) -> None:
    session = Session(auto_save_path="agent.json")
    original_enqueue = session.enqueue_auto_save

    def missing_owner():
        session.save_calls += 1
        return None

    session.enqueue_auto_save = missing_owner
    _patch_session(monkeypatch, session)
    environment = Environment()
    with pytest.raises(AgentRuntimeLifecycleError, match="persistence"):
        await agent_runtime.run_agent(
            agent=_agent(),
            environment=environment,
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=None,
            cleanup_timeout_seconds=0.1,
            transcript_path="agent.json",
            cleanup_environment=True,
        )
    assert session.save_calls == 1
    assert environment.cleanup_calls == 1
    assert original_enqueue is not None


async def test_agent_runtime_attempts_cleanup_after_pending_task_timeout(monkeypatch) -> None:
    session = Session()
    pending = asyncio.create_task(asyncio.Event().wait())
    session.pending_cleanup_tasks = (pending,)
    _patch_session(monkeypatch, session)
    environment = Environment()
    with pytest.raises(AgentRuntimeLifecycleError, match="cleanup or persistence"):
        await agent_runtime.run_agent(
            agent=_agent(),
            environment=environment,
            prompt="run",
            max_tokens=100,
            max_steps=5,
            timeout_seconds=None,
            cleanup_timeout_seconds=0.01,
            transcript_path=None,
            cleanup_environment=True,
        )
    assert environment.cleanup_calls == 1
    assert session.save_calls == 1
