from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from opencollab.adapters.env import Environment
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowContext
from opencollab.harness.evaluator_patch import cleanup_injected_paths_and_extract_patch
from opencollab.harness.evaluator_task_setup import prepare_eval_run


@dataclass
class _FinalizationState:
    error: str | None
    execution_quiesced: bool
    checkpoint_result: dict[str, Any] | None
    injected_paths: list[str]
    harness_artifact_exclusion_proven: bool
    checkpoint_restore_integrity_proven: bool
    test_patch_isolation_failed: bool
    task_stage_integrity_proven: bool
    persistence_succeeded: bool = True
    checkpoint_lingering: set[asyncio.Task[Any]] = field(default_factory=set)
    final_snapshot_lingering: tuple[asyncio.Task[Any], ...] = ()
    manifest_lingering: tuple[asyncio.Task[Any], ...] = ()
    environment_revocation_quiesced: bool = False
    patch: str = ""
    patch_extraction_succeeded: bool = False
    injected_path_cleanup_proven: bool = False


def _collect_late_stage_outcomes(
    facade: Any,
    state: _FinalizationState,
    stage_tasks: dict[str, asyncio.Task[Any]],
    observed_stage_results: set[str],
) -> None:
    """Adopt stage results that arrived after their caller-side deadline."""
    if not state.execution_quiesced:
        return
    for stage_name, stage_task in stage_tasks.items():
        if stage_name in observed_stage_results:
            continue
        observed_stage_results.add(stage_name)
        if stage_task.cancelled():
            if stage_name == "checkpoint_restore":
                state.checkpoint_restore_integrity_proven = False
            elif stage_name == "test_patch_injection":
                state.test_patch_isolation_failed = True
            continue
        try:
            late_value = stage_task.result()
        except facade.TestPatchIsolationError as exc:
            if stage_name == "test_patch_injection":
                state.injected_paths = list(dict.fromkeys((*state.injected_paths, *exc.touched_paths)))
                state.test_patch_isolation_failed = True
            state.error = facade._append_harness_error(state.error, f"late {stage_name} failed", exc)
        except asyncio.CancelledError:
            if stage_name == "checkpoint_restore":
                state.checkpoint_restore_integrity_proven = False
            elif stage_name == "test_patch_injection":
                state.test_patch_isolation_failed = True
        except BaseException as exc:
            state.error = facade._append_harness_error(state.error, f"late {stage_name} failed", exc)
            if stage_name == "checkpoint_restore":
                state.checkpoint_restore_integrity_proven = False
            elif stage_name == "test_patch_injection":
                state.test_patch_isolation_failed = True
        else:
            if stage_name == "checkpoint_restore":
                if state.checkpoint_result is None:
                    state.checkpoint_result = {}
                state.checkpoint_result["restore"] = late_value.to_dict()
                state.checkpoint_restore_integrity_proven = False
            elif stage_name == "test_patch_injection":
                state.injected_paths = list(dict.fromkeys((*state.injected_paths, *late_value)))
                state.test_patch_isolation_failed = True


async def _finalize_checkpoint_and_patch(
    facade: Any,
    state: _FinalizationState,
    *,
    env: Any,
    checkpoint: Any,
    harness_artifact_paths: list[str],
    cleanup_timeout: float,
    await_teardown: Callable[[Awaitable[Any]], Awaitable[Any]],
) -> None:
    """Stop or abort checkpointing, then clean injected paths and extract the patch."""
    if not state.execution_quiesced:
        failure = TimeoutError("owned execution did not quiesce after cancellation; patch extraction skipped")
        state.error = facade._append_harness_error(state.error, "execution cleanup timed out", failure)
        if checkpoint is not None:
            try:
                checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
            except Exception as exc:
                checkpoint_quiesced = False
                state.error = facade._append_harness_error(state.error, "checkpoint abort failed", exc)
            if state.checkpoint_result is None:
                state.checkpoint_result = {}
            state.checkpoint_result["abort"] = {
                "status": "aborted_non_quiescent_execution" if checkpoint_quiesced else "checkpoint_abort_timed_out"
            }
            if not checkpoint_quiesced:
                state.error = facade._append_harness_error(
                    state.error,
                    "checkpoint abort timed out",
                    TimeoutError("periodic checkpoint capture remained active"),
                )
        if env is not None:
            env._aborted = True

    unsafe_checkpoint = state.test_patch_isolation_failed or not state.checkpoint_restore_integrity_proven
    if state.execution_quiesced and env and checkpoint is not None and unsafe_checkpoint:
        try:
            checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
        except Exception as exc:
            checkpoint_quiesced = False
            state.error = facade._append_harness_error(state.error, "checkpoint abort failed", exc)
        if state.checkpoint_result is None:
            state.checkpoint_result = {}
        skipped_status = (
            "skipped_test_patch_isolation_failure"
            if state.test_patch_isolation_failed
            else "skipped_checkpoint_restore_integrity_failure"
        )
        state.checkpoint_result["final"] = {
            "status": skipped_status if checkpoint_quiesced else "checkpoint_abort_timed_out"
        }
        if not checkpoint_quiesced:
            state.error = facade._append_harness_error(
                state.error,
                "checkpoint abort timed out",
                TimeoutError("periodic checkpoint capture remained active"),
            )
            state.execution_quiesced = False

    if state.execution_quiesced and env and checkpoint is not None and not unsafe_checkpoint:
        try:
            checkpoint_finalized, final_checkpoint, stop_lingering = await await_teardown(
                facade._stop_checkpoint_bounded(
                    checkpoint,
                    env,
                    exclude_paths=(*state.injected_paths, *harness_artifact_paths),
                    cleanup_timeout=cleanup_timeout,
                )
            )
            state.checkpoint_lingering.update(stop_lingering)
            if state.checkpoint_result is None:
                state.checkpoint_result = {}
            if checkpoint_finalized:
                state.checkpoint_result["final"] = final_checkpoint.to_dict()
            else:
                state.checkpoint_result["final"] = {"status": "checkpoint_finalization_timed_out"}
                state.error = facade._append_harness_error(
                    state.error,
                    "checkpoint finalization timed out",
                    TimeoutError("checkpoint stop remained active"),
                )
                try:
                    checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
                    if not checkpoint_quiesced:
                        state.error = facade._append_harness_error(
                            state.error,
                            "checkpoint abort timed out",
                            TimeoutError("periodic checkpoint capture remained active"),
                        )
                except Exception as exc:
                    state.error = facade._append_harness_error(state.error, "checkpoint abort failed", exc)
                env._aborted = True
                state.execution_quiesced = False
        except Exception as exc:
            state.error = facade._append_harness_error(state.error, "checkpoint finalization failed", exc)

    (
        state.injected_path_cleanup_proven,
        state.patch,
        state.patch_extraction_succeeded,
        state.error,
    ) = await cleanup_injected_paths_and_extract_patch(
        facade,
        env=env,
        execution_quiesced=state.execution_quiesced,
        injected_paths=state.injected_paths,
        harness_artifact_paths=harness_artifact_paths,
        cleanup_timeout=cleanup_timeout,
        error=state.error,
        test_patch_isolation_failed=state.test_patch_isolation_failed,
        harness_artifact_exclusion_proven=state.harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=state.checkpoint_restore_integrity_proven,
        task_stage_integrity_proven=state.task_stage_integrity_proven,
        await_teardown=await_teardown,
    )


async def _cleanup_resources_and_build_result(
    facade: Any,
    state: _FinalizationState,
    *,
    task: Any,
    workflow: Any,
    run_dir: str | None,
    workflow_ctx: WorkflowContext | None,
    session: Session | None,
    checkpoint: Any,
    owned_execution_tasks: list[asyncio.Task[Any]],
    tracer: Any,
    env: Any,
    cleanup_timeout: float,
    start: float,
    await_teardown: Callable[[Awaitable[Any]], Awaitable[Any]],
) -> Any:
    """Persist the manifest, release owned resources, and build the public result."""
    if state.execution_quiesced and workflow_ctx is not None and workflow is not None and run_dir is not None:
        try:
            manifest_quiesced, manifest_error, state.manifest_lingering = await await_teardown(
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
            state.error = facade._append_harness_error(state.error, "workflow manifest failed", exc)
        else:
            if manifest_error is not None:
                state.persistence_succeeded = False
                state.error = facade._append_harness_error(state.error, "workflow manifest failed", manifest_error)
            if not manifest_quiesced:
                state.persistence_succeeded = False
                state.execution_quiesced = False
                state.error = facade._append_harness_error(
                    state.error,
                    "workflow manifest timed out",
                    TimeoutError("manifest persistence owner remained active"),
                )

    live_resource_dependencies = {
        owned
        for owned in (
            *owned_execution_tasks,
            *state.final_snapshot_lingering,
            *state.manifest_lingering,
            *state.checkpoint_lingering,
        )
        if not owned.done()
    }
    if checkpoint is not None:
        live_resource_dependencies.update(
            owned
            for owned in getattr(checkpoint, "pending_tasks", ())
            if isinstance(owned, asyncio.Task) and not owned.done()
        )
    if workflow_ctx is not None:
        live_resource_dependencies.update(
            owned
            for owned in workflow_ctx.pending_cleanup_tasks
            if isinstance(owned, asyncio.Task) and not owned.done()
        )
    if session is not None:
        live_resource_dependencies.update(
            owned
            for owned in getattr(session, "pending_cleanup_tasks", ())
            if isinstance(owned, asyncio.Task) and not owned.done()
        )
    resources_deferred = bool(live_resource_dependencies)
    if resources_deferred:
        state.execution_quiesced = False
        if env is not None:
            try:
                state.environment_revocation_quiesced = await await_teardown(
                    facade._abort_environment(env, cleanup_timeout=cleanup_timeout)
                )
            except Exception as exc:
                state.error = facade._append_harness_error(state.error, "environment abort failed", exc)
            if not state.environment_revocation_quiesced:
                state.error = facade._append_harness_error(
                    state.error,
                    "environment abort timed out",
                    TimeoutError("environment abort hook remained active"),
                )
        facade._defer_eval_resource_cleanup(
            tuple(live_resource_dependencies),
            tracer=tracer,
            env=env if not state.environment_revocation_quiesced else None,
        )
        state.error = facade._append_harness_error(
            state.error,
            "resource cleanup deferred",
            TimeoutError("owned persistence or execution task remained active"),
        )

    duration = time.monotonic() - start
    if not resources_deferred:
        try:
            tracer.close()
        except Exception as exc:
            state.error = facade._append_harness_error(state.error, "tracer close failed", exc)
    tracer_write_error = getattr(tracer, "write_error", None)
    if tracer_write_error:
        state.error = facade._append_harness_error(
            state.error,
            "tracer write failed",
            RuntimeError(str(tracer_write_error)),
        )

    if env and (not resources_deferred or state.environment_revocation_quiesced):
        environment_cleaned = False
        cleanup_raised = False
        try:
            environment_cleaned = await await_teardown(
                facade._cleanup_environment_bounded(env, cleanup_timeout=cleanup_timeout)
            )
        except Exception as exc:
            cleanup_raised = True
            state.error = facade._append_harness_error(state.error, "environment cleanup failed", exc)
        if not environment_cleaned:
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
                abort_quiesced = await await_teardown(facade._abort_environment(env, cleanup_timeout=cleanup_timeout))
                if not abort_quiesced:
                    state.error = facade._append_harness_error(
                        state.error,
                        "environment abort timed out",
                        TimeoutError("environment abort hook remained active"),
                    )
            except Exception as exc:
                state.error = facade._append_harness_error(state.error, "environment abort failed", exc)

    if workflow_ctx is not None:
        sessions = workflow_ctx.sessions
        tokens_used = facade._aggregate_tokens(sessions)
        steps = facade._aggregate_steps(sessions)
        markup_recovered = facade._aggregate_markup_recovery(sessions)
        workflow_error = getattr(workflow_ctx, "workflow_error", None)
        if workflow_error:
            state.error = f"{workflow_error}; {state.error}" if state.error else workflow_error
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0
        markup_recovered = getattr(session, "markup_recovered", 0) if session else 0

    result = facade.EvalResult(
        task_id=task.task_id,
        patch=state.patch,
        # Any extracted diff remains submittable even when the run reports an error.
        patch_produced=bool(state.patch.strip()),
        tokens_used=tokens_used,
        steps=steps,
        duration=duration,
        error=state.error,
        trajectory_path=tracer.path,
        markup_recovered=markup_recovered,
        workflow_result=getattr(workflow_ctx, "workflow_result", None) if workflow_ctx else None,
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
    return result


async def run_eval_task_impl(
    task: Any,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    output_dir: str,
    prompt: str,
    tools_factory: Callable[[], Any],
    env_factory: Callable[[Any], Awaitable[Any]],
    max_steps: int,
    workflow: Any,
    temperature: float,
    top_p: float | None,
    thinking: bool,
    thinking_params: dict | None,
    checkpoint_interval_seconds: float | None,
    resume_from_checkpoint: bool,
    cancellation_cleanup_timeout: float,
) -> Any:
    facade = sys.modules["opencollab.harness.evaluator"]
    prepared = prepare_eval_run(
        facade,
        task=task,
        output_dir=output_dir,
        workflow=workflow,
        max_steps=max_steps,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
        cancellation_cleanup_timeout=cancellation_cleanup_timeout,
    )
    task = prepared.task
    max_steps = prepared.max_steps
    normalized_checkpoint_interval = prepared.checkpoint_interval
    cleanup_timeout = prepared.cleanup_timeout
    start = prepared.start
    task_deadline = prepared.task_deadline
    trajectories_dir = prepared.trajectories_dir
    run_dir = prepared.run_dir
    tracer = prepared.tracer
    _EnvironmentSetupOwner = facade._EnvironmentSetupOwner
    _append_harness_error = facade._append_harness_error
    _finalize_eval_workflow_sessions = facade._finalize_eval_workflow_sessions
    _legacy_result_temp_paths = facade._legacy_result_temp_paths
    _mapped_artifact_path_bound_error = facade._mapped_artifact_path_bound_error
    _run_single_session = facade._run_single_session
    _run_workflow_mode = facade._run_workflow_mode
    _wait_for_owned_execution = facade._wait_for_owned_execution
    _workspace_relative_artifact_paths = facade._workspace_relative_artifact_paths
    _workspace_relative_host_path = facade._workspace_relative_host_path
    apply_test_patch = facade.apply_test_patch
    build_repo_map_via_env = facade.build_repo_map_via_env
    RESULT_TEMP_DIRECTORY = facade.RESULT_TEMP_DIRECTORY
    TestPatchIsolationError = facade.TestPatchIsolationError
    WorktreeCheckpoint = facade.WorktreeCheckpoint
    env: Environment | None = None
    session: Session | None = None
    workflow_ctx: WorkflowContext | None = None
    session_holder: list[Session] = []
    workflow_context_holder: list[WorkflowContext] = []
    owned_execution_tasks: list[asyncio.Task[Any]] = []
    checkpoint_lingering: set[asyncio.Task[Any]] = set()
    stage_tasks: dict[str, asyncio.Task[Any]] = {}
    observed_stage_results: set[str] = set()
    environment_setup_owner: _EnvironmentSetupOwner | None = None
    checkpoint: WorktreeCheckpoint | None = None
    checkpoint_result: dict[str, Any] | None = None
    error: str | None = None
    cancellation: asyncio.CancelledError | None = None
    injected_paths: list[str] = []
    harness_artifact_paths: list[str] = []
    harness_artifact_exclusion_proven = True
    checkpoint_restore_integrity_proven = True
    test_patch_isolation_failed = False
    task_stage_integrity_proven = True
    persistence_succeeded = True
    final_snapshot_lingering: tuple[asyncio.Task[Any], ...] = ()
    finalization_state: _FinalizationState | None = None

    def remaining_task_time() -> float:
        remaining = task_deadline - time.monotonic()
        if remaining <= 0:
            raise CallerTimeoutError
        return remaining

    async def await_task_stage(
        stage_name: str,
        awaitable: Awaitable[Any],
    ) -> Any:
        nonlocal task_stage_integrity_proven
        stage_task = asyncio.ensure_future(awaitable)
        owned_execution_tasks.append(stage_task)
        stage_tasks[stage_name] = stage_task
        try:
            result = await abandon_on_timeout(
                stage_task,
                remaining_task_time(),
            )
        except CallerTimeoutError:
            task_stage_integrity_proven = False
            if not stage_task.done():
                stage_task.cancel()
            raise
        except BaseException:
            if stage_task.done():
                observed_stage_results.add(stage_name)
            raise
        observed_stage_results.add(stage_name)
        return result

    try:
        environment_setup_timeout = remaining_task_time()
        environment_setup_owner = _EnvironmentSetupOwner(
            env_factory,
            task,
            cleanup_timeout=cleanup_timeout,
        )
        owned_execution_tasks.append(environment_setup_owner.task)
        try:
            env = await environment_setup_owner.acquire(environment_setup_timeout)
        except CallerTimeoutError:
            task_stage_integrity_proven = False
            environment_setup_owner.relinquish()
            raise
        except BaseException:
            environment_setup_owner.relinquish()
            raise
        environment_setup_owner.transfer(env)
        remaining_task_time()
        tools = list(tools_factory())
        remaining_task_time()
        artifact_candidates: list[str | os.PathLike[str]] = list(task.harness_artifact_paths)
        output_relative = _workspace_relative_host_path(env, output_dir)
        if output_relative is not None and output_relative != Path("."):
            artifact_candidates.append(output_dir)
        else:
            artifact_candidates.extend(
                (
                    trajectories_dir,
                    os.path.join(output_dir, "results.jsonl"),
                    os.path.join(output_dir, RESULT_TEMP_DIRECTORY),
                )
            )
            if output_relative == Path("."):
                legacy_paths, legacy_scan_complete = _legacy_result_temp_paths(output_dir)
                artifact_candidates.extend(legacy_paths)
                harness_artifact_exclusion_proven = legacy_scan_complete
        harness_artifact_paths = _workspace_relative_artifact_paths(
            env,
            artifact_candidates,
        )
        artifact_bound_error = _mapped_artifact_path_bound_error(harness_artifact_paths)
        if artifact_bound_error:
            harness_artifact_exclusion_proven = False
            raise RuntimeError(artifact_bound_error)
        if not harness_artifact_exclusion_proven:
            raise RuntimeError("legacy result temp artifact scan exceeded its safety bound")

        if run_dir is not None and normalized_checkpoint_interval is not None:
            checkpoint = WorktreeCheckpoint(
                Path(run_dir),
                interval_seconds=normalized_checkpoint_interval,
            )
            if resume_from_checkpoint:
                restore_result = await await_task_stage(
                    "checkpoint_restore",
                    checkpoint.restore_latest(
                        env,
                        exclude_paths=harness_artifact_paths,
                    ),
                )
                checkpoint_result = {"restore": restore_result.to_dict()}
                checkpoint_restore_integrity_proven = restore_result.worktree_integrity_proven
                if not checkpoint_restore_integrity_proven:
                    raise RuntimeError("checkpoint restore left worktree integrity unproven")

        # Inject the real FAIL_TO_PASS test before the workflow, preserving any
        # uncertain rollback paths for final cleanup and diff exclusion.
        test_patch = (task.extras or {}).get("test_patch")
        if test_patch:
            try:
                injected_paths = await await_task_stage("test_patch_injection", apply_test_patch(env, test_patch))
            except TestPatchIsolationError as exc:
                injected_paths = list(dict.fromkeys(exc.touched_paths))
                test_patch_isolation_failed = True
                if exc.cancellation is not None:
                    raise exc.cancellation from exc
                raise

        if checkpoint is not None:
            remaining_task_time()
            await checkpoint.start(
                env,
                exclude_paths=(*injected_paths, *harness_artifact_paths),
            )

        # A bounded repo map saves the model its first discovery steps.
        repo_map = await await_task_stage(
            "repo_map",
            build_repo_map_via_env(env),
        )
        prompt = f"{prompt}\n\n{repo_map}" if repo_map else prompt

        execution_task = replace(task, timeout=remaining_task_time())

        if workflow is None:
            session = await _run_single_session(
                task=execution_task,
                env=env,
                tracer=tracer,
                prompt=prompt,
                tools=tools,
                model=model,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                max_steps=max_steps,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                thinking_params=thinking_params,
                session_holder=session_holder,
                owned_tasks=owned_execution_tasks,
            )
        else:
            workflow_ctx = await _run_workflow_mode(
                task=execution_task,
                env=env,
                tracer=tracer,
                prompt=prompt,
                tools=tools,
                model=model,
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                max_steps=max_steps,
                workflow=workflow,
                injected_paths=injected_paths,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                thinking_params=thinking_params,
                save_dir=run_dir,
                context_holder=workflow_context_holder,
                owned_tasks=owned_execution_tasks,
                timeout_error_seconds=task.timeout,
            )

    except asyncio.CancelledError as exc:
        # Propagate cancellation after every owned resource is released.
        cancellation = exc
        if getattr(exc, "checkpoint_restore_integrity_proven", True) is False:
            checkpoint_restore_integrity_proven = False
        error = "evaluation task cancelled"
    except CallerTimeoutError:
        error = f"Task timed out after {task.timeout}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    async def await_teardown(awaitable: Awaitable[Any]) -> Any:
        """Finish one owned teardown operation despite repeated caller cancel."""
        nonlocal cancellation, error
        owned_task = asyncio.ensure_future(awaitable)
        while not owned_task.done():
            try:
                await asyncio.wait({owned_task})
            except asyncio.CancelledError as exc:
                if cancellation is None:
                    cancellation = exc
                    if finalization_state is None:
                        error = _append_harness_error(
                            error,
                            "evaluation task cancelled",
                            RuntimeError("caller cancelled during teardown"),
                        )
                    else:
                        finalization_state.error = _append_harness_error(
                            finalization_state.error,
                            "evaluation task cancelled",
                            RuntimeError("caller cancelled during teardown"),
                        )
                continue
        if owned_task.cancelled():
            raise RuntimeError("owned teardown operation cancelled itself")
        return owned_task.result()

    if session is None and session_holder:
        session = session_holder[0]
    if workflow_ctx is None and workflow_context_holder:
        workflow_ctx = workflow_context_holder[0]
    try:
        execution_quiesced = await await_teardown(
            _wait_for_owned_execution(
                owned_execution_tasks,
                workflow_ctx,
                cleanup_timeout=cleanup_timeout,
            )
        )
    except Exception as exc:
        execution_quiesced = False
        error = _append_harness_error(
            error,
            "execution teardown failed",
            exc,
        )

    if environment_setup_owner is not None:
        for stage, disposal_error in environment_setup_owner.disposal_errors:
            error = _append_harness_error(error, stage, disposal_error)
            execution_quiesced = False

    if execution_quiesced and workflow_ctx is not None:
        try:
            (
                final_snapshots_quiesced,
                persistence_errors,
                final_snapshot_lingering,
            ) = await await_teardown(
                _finalize_eval_workflow_sessions(
                    workflow_ctx,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            persistence_succeeded = False
            final_snapshots_quiesced = False
            final_snapshot_lingering = tuple(
                task
                for task in workflow_ctx.pending_cleanup_tasks
                if isinstance(task, asyncio.Task) and not task.done()
            )
            error = _append_harness_error(
                error,
                "final workflow snapshot failed",
                exc,
            )
        else:
            for persistence_error in persistence_errors:
                persistence_succeeded = False
                error = _append_harness_error(
                    error,
                    "final workflow snapshot failed",
                    persistence_error,
                )
        if not final_snapshots_quiesced:
            persistence_succeeded = False
            execution_quiesced = False
            error = _append_harness_error(
                error,
                "final workflow snapshot timed out",
                TimeoutError("session persistence owner remained active"),
            )

    state = _FinalizationState(
        error=error,
        execution_quiesced=execution_quiesced,
        checkpoint_result=checkpoint_result,
        injected_paths=injected_paths,
        harness_artifact_exclusion_proven=harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=checkpoint_restore_integrity_proven,
        test_patch_isolation_failed=test_patch_isolation_failed,
        task_stage_integrity_proven=task_stage_integrity_proven,
        persistence_succeeded=persistence_succeeded,
        checkpoint_lingering=checkpoint_lingering,
        final_snapshot_lingering=final_snapshot_lingering,
    )
    finalization_state = state
    _collect_late_stage_outcomes(facade, state, stage_tasks, observed_stage_results)
    await _finalize_checkpoint_and_patch(
        facade,
        state,
        env=env,
        checkpoint=checkpoint,
        harness_artifact_paths=harness_artifact_paths,
        cleanup_timeout=cleanup_timeout,
        await_teardown=await_teardown,
    )
    result = await _cleanup_resources_and_build_result(
        facade,
        state,
        task=task,
        workflow=workflow,
        run_dir=run_dir,
        workflow_ctx=workflow_ctx,
        session=session,
        checkpoint=checkpoint,
        owned_execution_tasks=owned_execution_tasks,
        tracer=tracer,
        env=env,
        cleanup_timeout=cleanup_timeout,
        start=start,
        await_teardown=await_teardown,
    )
    if cancellation is not None:
        if state.error:
            add_exception_note(cancellation, f"evaluation teardown diagnostics: {state.error}")
        raise cancellation
    return result
