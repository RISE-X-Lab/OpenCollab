"""Manifest persistence and resource release for evaluator tasks."""

from __future__ import annotations

import asyncio
from typing import Any


async def persist_manifest(
    facade: Any,
    state: Any,
    *,
    task: Any,
    workflow: Any,
    run_dir: str | None,
    workflow_ctx: Any,
    cleanup_timeout: float,
    guard: Any,
) -> None:
    if not (
        state.execution_quiesced
        and workflow_ctx is not None
        and workflow is not None
        and run_dir is not None
    ):
        return
    try:
        quiesced, manifest_error, state.manifest_lingering = await guard.wait(
            facade._persist_eval_workflow_manifest_owned(
                run_dir,
                task=task,
                workflow=workflow,
                ctx=workflow_ctx,
                cleanup_timeout=cleanup_timeout,
            )
        )
    except Exception as exc:
        state.persistence_succeeded = False
        state.error = facade._append_harness_error(
            state.error, "workflow manifest failed", exc
        )
        return
    if manifest_error is not None:
        state.persistence_succeeded = False
        state.error = facade._append_harness_error(
            state.error, "workflow manifest failed", manifest_error
        )
    if not quiesced:
        state.persistence_succeeded = False
        state.execution_quiesced = False
        state.error = facade._append_harness_error(
            state.error,
            "workflow manifest timed out",
            TimeoutError("manifest persistence owner remained active"),
        )


def collect_live_dependencies(
    state: Any,
    *,
    execution: Any,
) -> set[asyncio.Task[Any]]:
    dependencies = {
        owned
        for owned in (
            *execution.owned_tasks,
            *state.final_snapshot_lingering,
            *state.manifest_lingering,
            *state.checkpoint_lingering,
        )
        if not owned.done()
    }
    for owner in (execution.checkpoint, execution.workflow_ctx, execution.session):
        pending = (
            getattr(owner, "pending_tasks", ())
            if owner is execution.checkpoint
            else getattr(owner, "pending_cleanup_tasks", ())
        )
        dependencies.update(
            task
            for task in pending
            if isinstance(task, asyncio.Task) and not task.done()
        )
    return dependencies


async def _defer_live_resources(
    facade: Any,
    state: Any,
    dependencies: set[asyncio.Task[Any]],
    *,
    tracer: Any,
    env: Any,
    cleanup_timeout: float,
    guard: Any,
) -> None:
    state.execution_quiesced = False
    if env is not None:
        try:
            state.environment_revocation_quiesced = await guard.wait(
                facade._abort_environment(env, cleanup_timeout=cleanup_timeout)
            )
        except Exception as exc:
            state.error = facade._append_harness_error(
                state.error, "environment abort failed", exc
            )
        if not state.environment_revocation_quiesced:
            state.error = facade._append_harness_error(
                state.error,
                "environment abort timed out",
                TimeoutError("environment abort hook remained active"),
            )
    facade._defer_eval_resource_cleanup(
        tuple(dependencies),
        tracer=tracer,
        env=env if not state.environment_revocation_quiesced else None,
    )
    state.error = facade._append_harness_error(
        state.error,
        "resource cleanup deferred",
        TimeoutError("owned persistence or execution task remained active"),
    )


def _close_tracer(facade: Any, state: Any, tracer: Any) -> None:
    try:
        tracer.close()
    except Exception as exc:
        state.error = facade._append_harness_error(
            state.error, "tracer close failed", exc
        )
    write_error = getattr(tracer, "write_error", None)
    if write_error:
        state.error = facade._append_harness_error(
            state.error,
            "tracer write failed",
            RuntimeError(str(write_error)),
        )


async def _cleanup_environment(
    facade: Any,
    state: Any,
    *,
    env: Any,
    cleanup_timeout: float,
    guard: Any,
) -> None:
    cleanup_raised = False
    try:
        cleaned = await guard.wait(
            facade._cleanup_environment_bounded(env, cleanup_timeout=cleanup_timeout)
        )
    except Exception as exc:
        cleanup_raised = True
        cleaned = False
        state.error = facade._append_harness_error(
            state.error, "environment cleanup failed", exc
        )
    if cleaned:
        return
    state.execution_quiesced = False
    state.patch = ""
    state.patch_extraction_succeeded = False
    if not cleanup_raised:
        state.error = facade._append_harness_error(
            state.error,
            "environment cleanup timed out",
            TimeoutError("environment cleanup hook remained active"),
        )
    try:
        aborted = await guard.wait(
            facade._abort_environment(env, cleanup_timeout=cleanup_timeout)
        )
        if not aborted:
            state.error = facade._append_harness_error(
                state.error,
                "environment abort timed out",
                TimeoutError("environment abort hook remained active"),
            )
    except Exception as exc:
        state.error = facade._append_harness_error(
            state.error, "environment abort failed", exc
        )


async def release_resources(
    facade: Any,
    state: Any,
    *,
    dependencies: set[asyncio.Task[Any]],
    tracer: Any,
    env: Any,
    cleanup_timeout: float,
    guard: Any,
) -> None:
    deferred = bool(dependencies)
    if deferred:
        await _defer_live_resources(
            facade,
            state,
            dependencies,
            tracer=tracer,
            env=env,
            cleanup_timeout=cleanup_timeout,
            guard=guard,
        )
    else:
        _close_tracer(facade, state, tracer)
    if env and (not deferred or state.environment_revocation_quiesced):
        await _cleanup_environment(
            facade,
            state,
            env=env,
            cleanup_timeout=cleanup_timeout,
            guard=guard,
        )


def build_eval_result(
    facade: Any,
    state: Any,
    *,
    task: Any,
    workflow_ctx: Any,
    session: Any,
    tracer: Any,
    duration: float,
) -> Any:
    if workflow_ctx is not None:
        sessions = workflow_ctx.sessions
        tokens_used = facade._aggregate_tokens(sessions)
        steps = facade._aggregate_steps(sessions)
        markup_recovered = facade._aggregate_markup_recovery(sessions)
        workflow_error = getattr(workflow_ctx, "workflow_error", None)
        if workflow_error:
            state.error = (
                f"{workflow_error}; {state.error}" if state.error else workflow_error
            )
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0
        markup_recovered = getattr(session, "markup_recovered", 0) if session else 0
    return facade.EvalResult(
        task_id=task.task_id,
        patch=state.patch,
        patch_produced=bool(state.patch.strip()),
        tokens_used=tokens_used,
        steps=steps,
        duration=duration,
        error=state.error,
        trajectory_path=tracer.path,
        markup_recovered=markup_recovered,
        workflow_result=(
            getattr(workflow_ctx, "workflow_result", None) if workflow_ctx else None
        ),
        checkpoint_result=state.checkpoint_result,
        test_patch_isolation_failed=state.test_patch_isolation_failed,
        execution_quiesced=state.execution_quiesced,
        patch_extraction_succeeded=state.patch_extraction_succeeded,
        injected_path_cleanup_proven=state.injected_path_cleanup_proven,
        harness_artifact_exclusion_proven=state.harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=state.checkpoint_restore_integrity_proven,
        task_stage_integrity_proven=state.task_stage_integrity_proven,
        submission_eligible=(
            state.execution_quiesced
            and state.patch_extraction_succeeded
            and state.injected_path_cleanup_proven
            and state.harness_artifact_exclusion_proven
            and state.checkpoint_restore_integrity_proven
            and state.task_stage_integrity_proven
            and state.persistence_succeeded
            and not state.test_patch_isolation_failed
        ),
    )


async def settle_execution(
    facade: Any,
    execution: Any,
    *,
    cleanup_timeout: float,
    guard: Any,
) -> tuple[bool, bool, tuple[asyncio.Task[Any], ...]]:
    if execution.session is None and execution.session_holder:
        execution.session = execution.session_holder[0]
    if execution.workflow_ctx is None and execution.workflow_context_holder:
        execution.workflow_ctx = execution.workflow_context_holder[0]
    try:
        quiesced = await guard.wait(
            facade._wait_for_owned_execution(
                execution.owned_tasks,
                execution.workflow_ctx,
                cleanup_timeout=cleanup_timeout,
            )
        )
    except Exception as exc:
        quiesced = False
        guard.error = facade._append_harness_error(
            guard.error, "execution teardown failed", exc
        )
    if execution.environment_setup_owner is not None:
        for stage, disposal_error in execution.environment_setup_owner.disposal_errors:
            guard.error = facade._append_harness_error(
                guard.error, stage, disposal_error
            )
            quiesced = False
    return await _finalize_workflow_sessions(
        facade,
        execution,
        quiesced=quiesced,
        cleanup_timeout=cleanup_timeout,
        guard=guard,
    )


async def _finalize_workflow_sessions(
    facade: Any,
    execution: Any,
    *,
    quiesced: bool,
    cleanup_timeout: float,
    guard: Any,
) -> tuple[bool, bool, tuple[asyncio.Task[Any], ...]]:
    persistence_succeeded = True
    lingering: tuple[asyncio.Task[Any], ...] = ()
    if not quiesced or execution.workflow_ctx is None:
        return quiesced, persistence_succeeded, lingering
    try:
        finalized, persistence_errors, lingering = await guard.wait(
            facade._finalize_eval_workflow_sessions(
                execution.workflow_ctx,
                cleanup_timeout=cleanup_timeout,
            )
        )
    except Exception as exc:
        persistence_succeeded = False
        finalized = False
        lingering = tuple(
            task
            for task in execution.workflow_ctx.pending_cleanup_tasks
            if isinstance(task, asyncio.Task) and not task.done()
        )
        guard.error = facade._append_harness_error(
            guard.error, "final workflow snapshot failed", exc
        )
    else:
        for persistence_error in persistence_errors:
            persistence_succeeded = False
            guard.error = facade._append_harness_error(
                guard.error,
                "final workflow snapshot failed",
                persistence_error,
            )
    if not finalized:
        persistence_succeeded = False
        quiesced = False
        guard.error = facade._append_harness_error(
            guard.error,
            "final workflow snapshot timed out",
            TimeoutError("session persistence owner remained active"),
        )
    return quiesced, persistence_succeeded, lingering
