from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from opencollab.adapters.env import Environment
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowContext
from opencollab.harness.evaluator_patch import cleanup_injected_paths_and_extract_patch
from opencollab.harness.evaluator_task_setup import prepare_eval_run


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
    _abort_environment = facade._abort_environment
    _aggregate_markup_recovery = facade._aggregate_markup_recovery
    _aggregate_steps = facade._aggregate_steps
    _aggregate_tokens = facade._aggregate_tokens
    _append_harness_error = facade._append_harness_error
    _cleanup_environment_bounded = facade._cleanup_environment_bounded
    _defer_eval_resource_cleanup = facade._defer_eval_resource_cleanup
    _finalize_eval_workflow_sessions = facade._finalize_eval_workflow_sessions
    _legacy_result_temp_paths = facade._legacy_result_temp_paths
    _mapped_artifact_path_bound_error = facade._mapped_artifact_path_bound_error
    _persist_eval_workflow_manifest_owned = facade._persist_eval_workflow_manifest_owned
    _run_single_session = facade._run_single_session
    _run_workflow_mode = facade._run_workflow_mode
    _stop_checkpoint_bounded = facade._stop_checkpoint_bounded
    _wait_for_owned_execution = facade._wait_for_owned_execution
    _workspace_relative_artifact_paths = facade._workspace_relative_artifact_paths
    _workspace_relative_host_path = facade._workspace_relative_host_path
    apply_test_patch = facade.apply_test_patch
    build_repo_map_via_env = facade.build_repo_map_via_env
    EvalResult = facade.EvalResult
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
    patch = ""
    injected_paths: list[str] = []
    harness_artifact_paths: list[str] = []
    harness_artifact_exclusion_proven = True
    checkpoint_restore_integrity_proven = True
    test_patch_isolation_failed = False
    task_stage_integrity_proven = True
    persistence_succeeded = True
    final_snapshot_lingering: tuple[asyncio.Task[Any], ...] = ()
    manifest_lingering: tuple[asyncio.Task[Any], ...] = ()
    environment_revocation_quiesced = False

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

        # SWE-bench test injection: apply the real FAIL_TO_PASS test into the
        # workspace BEFORE the workflow runs so the agent can verify against it.
        # Guarded on extras so single-session / non-SWE-bench paths are
        # unaffected. Preflight failures skip injection. An uncertain rollback
        # stops before agent execution and preserves known paths for final
        # cleanup and temporary-index exclusion.
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

        # Orientation up front: a bounded repo map in the system prompt saves
        # the model its first N steps of ls/find exploration.
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
        # Finish the same checkpoint/diff/tracer/environment teardown below,
        # then propagate cancellation after every owned resource is released.
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
                    error = _append_harness_error(
                        error,
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

    # A stage may consume cancellation and return a resource or mutation record
    # after its caller-side deadline. Adopt every such result before deciding
    # what must be cleaned or excluded from the candidate diff.
    if execution_quiesced:
        for stage_name, stage_task in stage_tasks.items():
            if stage_name in observed_stage_results:
                continue
            observed_stage_results.add(stage_name)
            if stage_task.cancelled():
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
                continue
            try:
                late_value = stage_task.result()
            except TestPatchIsolationError as exc:
                if stage_name == "test_patch_injection":
                    injected_paths = list(dict.fromkeys((*injected_paths, *exc.touched_paths)))
                    test_patch_isolation_failed = True
                error = _append_harness_error(
                    error,
                    f"late {stage_name} failed",
                    exc,
                )
            except asyncio.CancelledError:
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
            except BaseException as exc:
                error = _append_harness_error(
                    error,
                    f"late {stage_name} failed",
                    exc,
                )
                if stage_name == "checkpoint_restore":
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    test_patch_isolation_failed = True
            else:
                if stage_name == "checkpoint_restore":
                    restore_result = late_value
                    if checkpoint_result is None:
                        checkpoint_result = {}
                    checkpoint_result["restore"] = restore_result.to_dict()
                    checkpoint_restore_integrity_proven = False
                elif stage_name == "test_patch_injection":
                    injected_paths = list(dict.fromkeys((*injected_paths, *late_value)))
                    test_patch_isolation_failed = True

    if not execution_quiesced:
        failure = TimeoutError("owned execution did not quiesce after cancellation; patch extraction skipped")
        error = _append_harness_error(error, "execution cleanup timed out", failure)
        if checkpoint is not None:
            try:
                checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
            except Exception as exc:
                checkpoint_quiesced = False
                error = _append_harness_error(
                    error,
                    "checkpoint abort failed",
                    exc,
                )
            if checkpoint_result is None:
                checkpoint_result = {}
            checkpoint_result["abort"] = {
                "status": ("aborted_non_quiescent_execution" if checkpoint_quiesced else "checkpoint_abort_timed_out")
            }
            if not checkpoint_quiesced:
                error = _append_harness_error(
                    error,
                    "checkpoint abort timed out",
                    TimeoutError("periodic checkpoint capture remained active"),
                )
        if env is not None:
            # Revoke new public operations immediately. Adapter teardown waits
            # until the still-owned workflow/checkpoint tasks stop using it.
            env._aborted = True

    if (
        execution_quiesced
        and env
        and checkpoint is not None
        and (test_patch_isolation_failed or not checkpoint_restore_integrity_proven)
    ):
        try:
            checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
        except Exception as exc:
            checkpoint_quiesced = False
            error = _append_harness_error(error, "checkpoint abort failed", exc)
        if checkpoint_result is None:
            checkpoint_result = {}
        checkpoint_result["final"] = {
            "status": (
                (
                    "skipped_test_patch_isolation_failure"
                    if test_patch_isolation_failed
                    else "skipped_checkpoint_restore_integrity_failure"
                )
                if checkpoint_quiesced
                else "checkpoint_abort_timed_out"
            )
        }
        if not checkpoint_quiesced:
            error = _append_harness_error(
                error,
                "checkpoint abort timed out",
                TimeoutError("periodic checkpoint capture remained active"),
            )
            execution_quiesced = False

    if (
        execution_quiesced
        and env
        and checkpoint is not None
        and not test_patch_isolation_failed
        and checkpoint_restore_integrity_proven
    ):
        try:
            (
                checkpoint_finalized,
                final_checkpoint,
                stop_lingering,
            ) = await await_teardown(
                _stop_checkpoint_bounded(
                    checkpoint,
                    env,
                    exclude_paths=(*injected_paths, *harness_artifact_paths),
                    cleanup_timeout=cleanup_timeout,
                )
            )
            checkpoint_lingering.update(stop_lingering)
            if checkpoint_result is None:
                checkpoint_result = {}
            if checkpoint_finalized:
                checkpoint_result["final"] = final_checkpoint.to_dict()
            else:
                checkpoint_result["final"] = {"status": "checkpoint_finalization_timed_out"}
                error = _append_harness_error(
                    error,
                    "checkpoint finalization timed out",
                    TimeoutError("checkpoint stop remained active"),
                )
                try:
                    checkpoint_quiesced = await await_teardown(checkpoint.abort(timeout=cleanup_timeout))
                    if not checkpoint_quiesced:
                        error = _append_harness_error(
                            error,
                            "checkpoint abort timed out",
                            TimeoutError("periodic checkpoint capture remained active"),
                        )
                except Exception as exc:
                    error = _append_harness_error(
                        error,
                        "checkpoint abort failed",
                        exc,
                    )
                env._aborted = True
                execution_quiesced = False
        except Exception as exc:
            error = _append_harness_error(error, "checkpoint finalization failed", exc)

    (
        injected_path_cleanup_proven,
        patch,
        patch_extraction_succeeded,
        error,
    ) = await cleanup_injected_paths_and_extract_patch(
        facade,
        env=env,
        execution_quiesced=execution_quiesced,
        injected_paths=injected_paths,
        harness_artifact_paths=harness_artifact_paths,
        cleanup_timeout=cleanup_timeout,
        error=error,
        test_patch_isolation_failed=test_patch_isolation_failed,
        harness_artifact_exclusion_proven=harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=checkpoint_restore_integrity_proven,
        task_stage_integrity_proven=task_stage_integrity_proven,
        await_teardown=await_teardown,
    )

    if execution_quiesced and workflow_ctx is not None and workflow is not None and run_dir is not None:
        try:
            (
                manifest_quiesced,
                manifest_error,
                manifest_lingering,
            ) = await await_teardown(
                _persist_eval_workflow_manifest_owned(
                    run_dir,
                    task=task,
                    workflow=workflow,
                    ctx=workflow_ctx,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            persistence_succeeded = False
            error = _append_harness_error(
                error,
                "workflow manifest failed",
                exc,
            )
        else:
            if manifest_error is not None:
                persistence_succeeded = False
                error = _append_harness_error(
                    error,
                    "workflow manifest failed",
                    manifest_error,
                )
            if not manifest_quiesced:
                persistence_succeeded = False
                execution_quiesced = False
                error = _append_harness_error(
                    error,
                    "workflow manifest timed out",
                    TimeoutError("manifest persistence owner remained active"),
                )

    live_resource_dependencies: set[asyncio.Task[Any]] = {
        task
        for task in (
            *owned_execution_tasks,
            *final_snapshot_lingering,
            *manifest_lingering,
            *checkpoint_lingering,
        )
        if not task.done()
    }
    if checkpoint is not None:
        live_resource_dependencies.update(
            task
            for task in getattr(checkpoint, "pending_tasks", ())
            if isinstance(task, asyncio.Task) and not task.done()
        )
    if workflow_ctx is not None:
        live_resource_dependencies.update(
            task for task in workflow_ctx.pending_cleanup_tasks if isinstance(task, asyncio.Task) and not task.done()
        )
    if session is not None:
        live_resource_dependencies.update(
            task
            for task in getattr(session, "pending_cleanup_tasks", ())
            if isinstance(task, asyncio.Task) and not task.done()
        )
    resources_deferred = bool(live_resource_dependencies)
    if resources_deferred:
        execution_quiesced = False
        if env is not None:
            try:
                environment_revocation_quiesced = await await_teardown(
                    _abort_environment(
                        env,
                        cleanup_timeout=cleanup_timeout,
                    )
                )
            except Exception as exc:
                error = _append_harness_error(
                    error,
                    "environment abort failed",
                    exc,
                )
            if not environment_revocation_quiesced:
                error = _append_harness_error(
                    error,
                    "environment abort timed out",
                    TimeoutError("environment abort hook remained active"),
                )
        _defer_eval_resource_cleanup(
            tuple(live_resource_dependencies),
            tracer=tracer,
            env=(env if not environment_revocation_quiesced else None),
        )
        error = _append_harness_error(
            error,
            "resource cleanup deferred",
            TimeoutError("owned persistence or execution task remained active"),
        )

    duration = time.monotonic() - start
    if not resources_deferred:
        try:
            tracer.close()
        except Exception as exc:
            error = _append_harness_error(error, "tracer close failed", exc)
    tracer_write_error = getattr(tracer, "write_error", None)
    if tracer_write_error:
        error = _append_harness_error(
            error,
            "tracer write failed",
            RuntimeError(str(tracer_write_error)),
        )

    if env and (not resources_deferred or environment_revocation_quiesced):
        environment_cleaned = False
        cleanup_raised = False
        try:
            environment_cleaned = await await_teardown(
                _cleanup_environment_bounded(
                    env,
                    cleanup_timeout=cleanup_timeout,
                )
            )
        except Exception as exc:
            cleanup_raised = True
            error = _append_harness_error(error, "environment cleanup failed", exc)
        if not environment_cleaned:
            execution_quiesced = False
            patch = ""
            patch_extraction_succeeded = False
            if not cleanup_raised:
                error = _append_harness_error(
                    error,
                    "environment cleanup timed out",
                    TimeoutError("environment cleanup hook remained active"),
                )
            try:
                abort_quiesced = await await_teardown(
                    _abort_environment(
                        env,
                        cleanup_timeout=cleanup_timeout,
                    )
                )
                if not abort_quiesced:
                    error = _append_harness_error(
                        error,
                        "environment abort timed out",
                        TimeoutError("environment abort hook remained active"),
                    )
            except Exception as exc:
                error = _append_harness_error(error, "environment abort failed", exc)

    if workflow_ctx is not None:
        sessions = workflow_ctx.sessions
        tokens_used = _aggregate_tokens(sessions)
        steps = _aggregate_steps(sessions)
        markup_recovered = _aggregate_markup_recovery(sessions)
        workflow_error = getattr(workflow_ctx, "workflow_error", None)
        if workflow_error:
            error = f"{workflow_error}; {error}" if error else workflow_error
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0
        markup_recovered = getattr(session, "markup_recovered", 0) if session else 0

    result = EvalResult(
        task_id=task.task_id,
        patch=patch,
        # An on-disk diff is a real, submittable patch regardless of how the run
        # ended — a budget-floor stop or an outer-wall timeout still produces the
        # patch we grade (django-11564 was graded RESOLVED yet reported
        # patch_produced=false under the old ``and error is None`` guard).
        patch_produced=bool(patch.strip()),
        tokens_used=tokens_used,
        steps=steps,
        duration=duration,
        error=error,
        trajectory_path=tracer.path,
        markup_recovered=markup_recovered,
        workflow_result=getattr(workflow_ctx, "workflow_result", None) if workflow_ctx else None,
        checkpoint_result=checkpoint_result,
        test_patch_isolation_failed=test_patch_isolation_failed,
        execution_quiesced=execution_quiesced,
        patch_extraction_succeeded=patch_extraction_succeeded,
        injected_path_cleanup_proven=injected_path_cleanup_proven,
        harness_artifact_exclusion_proven=harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=(checkpoint_restore_integrity_proven),
        task_stage_integrity_proven=task_stage_integrity_proven,
        submission_eligible=(
            execution_quiesced
            and patch_extraction_succeeded
            and injected_path_cleanup_proven
            and harness_artifact_exclusion_proven
            and checkpoint_restore_integrity_proven
            and task_stage_integrity_proven
            and persistence_succeeded
            and not test_patch_isolation_failed
        ),
    )
    if cancellation is not None:
        if error:
            add_exception_note(
                cancellation,
                f"evaluation teardown diagnostics: {error}",
            )
        raise cancellation
    return result
