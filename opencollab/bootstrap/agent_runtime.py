"""Owned lifecycle for one programmatic Agent execution."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Literal

from opencollab.application.async_timeout import (
    await_owned_operation,
    cancel_tasks_and_wait,
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
    environment_cleanup_quiesced: bool | None
    environment_quiesced: bool | None
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


async def _terminate_session_tasks(session: Any, timeout: float) -> bool:
    """Cancel overdue session owners and wait one bounded phase for quiescence."""
    pending = {
        task
        for task in session.pending_cleanup_tasks
        if not task.done() and task is not asyncio.current_task()
    }
    if pending:
        pending = await await_owned_operation(
            cancel_tasks_and_wait(pending, timeout=timeout),
            propagate_cancellation=True,
        )
    return not pending and not any(
        not task.done() for task in session.pending_cleanup_tasks
    )


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
    cleanup_environment: bool,
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
        environment_cleanup_quiesced=True if cleanup_environment else None,
        environment_quiesced=True if cleanup_environment else None,
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
    finalization_task: asyncio.Task[bool] | None = None
    finalization_succeeded = False
    finalization_attempts = 0
    max_finalization_attempts = 2
    stop_task: asyncio.Task[tuple[bool, bool, bool]] | None = None

    def record_finalization_result(task: asyncio.Task[bool]) -> bool:
        nonlocal finalization_task, finalization_succeeded
        try:
            succeeded = task.result()
        except BaseException:
            succeeded = False
        if succeeded:
            finalization_succeeded = True
        elif finalization_task is task:
            finalization_task = None
        return succeeded

    async def finalize_attempt() -> bool:
        nonlocal finalization_task, finalization_attempts
        if finalization_succeeded:
            return True
        if finalization_task is None:
            if finalization_attempts >= max_finalization_attempts:
                return False
            finalization_attempts += 1
            finalization_task = asyncio.create_task(
                _finalize_session(
                    session,
                    environment,
                    timeout=cleanup_timeout_seconds,
                    cleanup_environment=cleanup_environment,
                )
            )
        task = finalization_task
        try:
            await await_owned_operation(task, propagate_cancellation=True)
        except asyncio.CancelledError:
            if task.done():
                record_finalization_result(task)
            raise
        except BaseException:
            if task.done():
                record_finalization_result(task)
            return False
        return record_finalization_result(task)

    async def finalize_with_retry() -> bool:
        while not finalization_succeeded:
            if (
                finalization_task is None
                and finalization_attempts >= max_finalization_attempts
            ):
                break
            if await finalize_attempt():
                return True
            await _terminate_session_tasks(session, cleanup_timeout_seconds)
        await _terminate_session_tasks(session, cleanup_timeout_seconds)
        return False

    async def stop_owned() -> tuple[bool, bool, bool]:
        aborted = True
        if cleanup_environment:
            aborted = await revoke_and_abort_environment(
                environment,
                cleanup_timeout_seconds,
            )
        terminal = await force_task_terminal(owner, timeout=cleanup_timeout_seconds)
        finalized = await finalize_with_retry()
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
                cleanup_environment=cleanup_environment,
            )
        try:
            output = owner.result()
        except Exception as exc:
            finalized = await finalize_with_retry()
            if not finalized:
                raise AgentRuntimeLifecycleError(
                    "failed agent cleanup or persistence did not quiesce"
                ) from exc
            return _result(
                session,
                output=None,
                outcome="failed",
                error=exc,
                cleanup_quiesced=True,
                cleanup_environment=cleanup_environment,
            )
        finalized = await finalize_with_retry()
        if not finalized:
            raise AgentRuntimeLifecycleError("agent cleanup or persistence did not quiesce")
        return _result(
            session,
            output=output,
            outcome="completed",
            error=None,
            cleanup_quiesced=True,
            cleanup_environment=cleanup_environment,
        )
    except asyncio.CancelledError as cancellation:
        aborted, terminated, finalized = await stop_once(
            propagate_cancellation=False
        )
        if not aborted or not terminated or not finalized:
            raise AgentRuntimeLifecycleError(
                "cancelled agent did not reach a quiescent terminal state"
            ) from cancellation
        raise
    except Exception:
        if not owner.done():
            await stop_once(propagate_cancellation=False)
        elif not finalization_succeeded:
            await finalize_with_retry()
        raise


__all__ = [
    "AgentRuntimeLifecycleError",
    "AgentRuntimeResult",
    "revoke_and_abort_environment",
    "run_agent",
    "run_environment_hook",
]
