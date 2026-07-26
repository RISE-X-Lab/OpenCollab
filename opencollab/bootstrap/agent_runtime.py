"""Owned lifecycle for one programmatic Agent execution."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Literal

from opencollab.application.async_timeout import (
    await_owned_operation,
    consume_task_result,
    force_task_terminal,
)
from opencollab.application.ports import LLMPort, TracePort
from opencollab.bootstrap.container import build_workspace_safety_policy
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent


class AgentRuntimeLifecycleError(RuntimeError):
    """Agent-owned work could not reach a proven terminal state."""


@dataclass(slots=True)
class AgentRuntimeResult:
    output: str | None
    outcome: Literal["completed", "timed_out", "failed"]
    error: Exception | None
    phase: str
    terminal_reason: str | None
    tokens_spent: int
    step_count: int
    markup_recovered: int
    cleanup_quiesced: bool
    persistence_errors: tuple[str, ...]


async def _run_session(session: Any, prompt: str) -> str:
    await session.add_user_message(prompt)
    return await session.run_loop()


async def run_environment_hook(environment: Any, name: str, timeout: float) -> bool:
    """Run one environment hook with bounded cancellation and honest failure."""
    hook = getattr(environment, name, None)
    if not callable(hook):
        return False
    try:
        outcome = hook()
    except Exception:
        return False
    if not inspect.isawaitable(outcome):
        return True
    owner = asyncio.ensure_future(outcome)
    done, pending = await asyncio.wait({owner}, timeout=timeout)
    if pending:
        terminal = await force_task_terminal(owner, timeout=timeout)
        if not terminal:
            owner.add_done_callback(consume_task_result)
        return False
    try:
        owner.result()
    except BaseException:
        return False
    return True


async def revoke_and_abort_environment(environment: Any, timeout: float) -> bool:
    revoke = getattr(environment, "revoke", None)
    revoked = True
    if callable(revoke):
        try:
            revoke()
        except Exception:
            revoked = False
    aborted = await run_environment_hook(environment, "abort", timeout)
    return revoked and aborted


async def _wait_session_tasks(session: Any, timeout: float) -> bool:
    pending = {
        task
        for task in session.pending_cleanup_tasks
        if not task.done() and task is not asyncio.current_task()
    }
    if pending:
        _done, pending = await asyncio.wait(pending, timeout=timeout)
    return not pending and not session.pending_cleanup_tasks


async def _finalize_session(
    session: Any,
    environment: Any,
    *,
    timeout: float,
    cleanup_environment: bool,
) -> bool:
    quiesced = await _wait_session_tasks(session, timeout)
    persistence_ready = False
    try:
        save_owner = session.enqueue_auto_save()
    except Exception:
        save_owner = None
    else:
        persistence_ready = session.auto_save_path is None or save_owner is not None
    persistence_quiesced = await _wait_session_tasks(session, timeout)
    cleanup_quiesced = not cleanup_environment or await run_environment_hook(
        environment,
        "cleanup",
        timeout,
    )
    return (
        quiesced
        and persistence_ready
        and persistence_quiesced
        and cleanup_quiesced
        and not session.persistence_errors
    )


def _result(
    session: Any,
    *,
    output: str | None,
    outcome: Literal["completed", "timed_out", "failed"],
    error: Exception | None,
    cleanup_quiesced: bool,
) -> AgentRuntimeResult:
    return AgentRuntimeResult(
        output=output,
        outcome=outcome,
        error=error,
        phase=session.phase.value,
        terminal_reason=session.state.terminal_reason,
        tokens_spent=session.used_tokens,
        step_count=session.step_count,
        markup_recovered=int(getattr(session, "markup_recovered", 0)),
        cleanup_quiesced=cleanup_quiesced,
        persistence_errors=tuple(str(error) for error in session.persistence_errors),
    )


async def run_agent(
    *,
    agent: Agent,
    environment: Any,
    prompt: str,
    max_tokens: int,
    max_steps: int,
    timeout_seconds: float | None,
    cleanup_timeout_seconds: float,
    transcript_path: str | None,
    tracer: TracePort | None = None,
    llm: LLMPort | None = None,
    llm_timeout_seconds: float = 600.0,
    cleanup_environment: bool = False,
) -> AgentRuntimeResult:
    """Run one Agent and return only after cleanup and final save quiesce."""
    session = build_session(
        agent=agent,
        env=environment,
        tracer=tracer,
        max_budget_tokens=max_tokens,
        max_steps=max_steps,
        auto_save_path=transcript_path,
        safety_policy=build_workspace_safety_policy(environment),
        llm=llm,
        llm_timeout=llm_timeout_seconds,
    )
    owner = asyncio.create_task(_run_session(session, prompt))
    finalization_attempted = False
    finalization_result = False
    stop_task: asyncio.Task[tuple[bool, bool, bool]] | None = None

    async def finalize_once() -> bool:
        nonlocal finalization_attempted, finalization_result
        if not finalization_attempted:
            finalization_attempted = True
            finalization_result = await _finalize_session(
                session,
                environment,
                timeout=cleanup_timeout_seconds,
                cleanup_environment=cleanup_environment,
            )
        return finalization_result

    async def stop_owned() -> tuple[bool, bool, bool]:
        aborted = await revoke_and_abort_environment(environment, cleanup_timeout_seconds)
        terminal = await force_task_terminal(owner, timeout=cleanup_timeout_seconds)
        finalized = await finalize_once()
        return aborted, terminal, finalized

    async def stop_once(*, propagate_cancellation: bool) -> tuple[bool, bool, bool]:
        nonlocal stop_task
        if stop_task is None:
            stop_task = asyncio.create_task(stop_owned())
        return await await_owned_operation(
            stop_task,
            propagate_cancellation=propagate_cancellation,
        )

    try:
        done, pending = await asyncio.wait({owner}, timeout=timeout_seconds)
        if pending:
            aborted, terminated, finalized = await stop_once(
                propagate_cancellation=True
            )
            if not aborted or not terminated or not finalized:
                raise AgentRuntimeLifecycleError(
                    "timed-out agent did not reach a quiescent terminal state"
                )
            return _result(
                session,
                output=None,
                outcome="timed_out",
                error=TimeoutError(f"agent exceeded {timeout_seconds:g} seconds"),
                cleanup_quiesced=True,
            )
        try:
            output = owner.result()
        except Exception as exc:
            finalized = await finalize_once()
            if not finalized:
                raise AgentRuntimeLifecycleError("failed agent cleanup or persistence did not quiesce") from exc
            return _result(
                session,
                output=None,
                outcome="failed",
                error=exc,
                cleanup_quiesced=True,
            )
        finalized = await finalize_once()
        if not finalized:
            raise AgentRuntimeLifecycleError("agent cleanup or persistence did not quiesce")
        return _result(
            session,
            output=output,
            outcome="completed",
            error=None,
            cleanup_quiesced=True,
        )
    except asyncio.CancelledError:
        await stop_once(propagate_cancellation=False)
        raise
    except Exception:
        if not finalization_attempted:
            if not owner.done():
                await stop_once(propagate_cancellation=False)
            else:
                await finalize_once()
        raise


__all__ = [
    "AgentRuntimeLifecycleError",
    "AgentRuntimeResult",
    "revoke_and_abort_environment",
    "run_agent",
    "run_environment_hook",
]
