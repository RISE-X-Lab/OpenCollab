"""Evidence-preserving finalization for one evaluator task."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from opencollab.application.exception_notes import add_exception_note
from opencollab.harness.evaluator_patch import cleanup_injected_paths_and_extract_patch
from opencollab.harness.evaluator_task_resources import (
    build_eval_result,
    collect_live_dependencies,
    persist_manifest,
    release_resources,
    settle_execution,
)


@dataclass
class FinalizationState:
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


class TeardownGuard:
    """Finish owned teardown operations despite repeated caller cancellation."""

    def __init__(
        self,
        facade: Any,
        *,
        error: str | None,
        cancellation: asyncio.CancelledError | None,
    ):
        self._facade = facade
        self.error = error
        self.cancellation = cancellation
        self._state: FinalizationState | None = None

    def bind(self, state: FinalizationState) -> None:
        self._state = state
        state.error = self.error

    def _record_cancellation(self, cancellation: asyncio.CancelledError) -> None:
        if self.cancellation is not None:
            return
        self.cancellation = cancellation
        error = self._facade._append_harness_error(
            self.error if self._state is None else self._state.error,
            "evaluation task cancelled",
            RuntimeError("caller cancelled during teardown"),
        )
        self.error = error
        if self._state is not None:
            self._state.error = error

    async def wait(self, awaitable: Awaitable[Any]) -> Any:
        owned_task = asyncio.ensure_future(awaitable)
        while not owned_task.done():
            try:
                await asyncio.wait({owned_task})
            except asyncio.CancelledError as exc:
                self._record_cancellation(exc)
        if owned_task.cancelled():
            raise RuntimeError("owned teardown operation cancelled itself")
        return owned_task.result()


def _mark_late_stage_failure(state: FinalizationState, stage_name: str) -> None:
    if stage_name == "checkpoint_restore":
        state.checkpoint_restore_integrity_proven = False
    elif stage_name == "test_patch_injection":
        state.test_patch_isolation_failed = True


def _record_late_stage_value(
    state: FinalizationState,
    stage_name: str,
    late_value: Any,
) -> None:
    if stage_name == "checkpoint_restore":
        if state.checkpoint_result is None:
            state.checkpoint_result = {}
        state.checkpoint_result["restore"] = late_value.to_dict()
        state.checkpoint_restore_integrity_proven = False
    elif stage_name == "test_patch_injection":
        state.injected_paths = list(
            dict.fromkeys((*state.injected_paths, *late_value))
        )
        state.test_patch_isolation_failed = True


def _adopt_late_stage(
    facade: Any,
    state: FinalizationState,
    stage_name: str,
    stage_task: asyncio.Task[Any],
) -> None:
    if stage_task.cancelled():
        _mark_late_stage_failure(state, stage_name)
        return
    try:
        late_value = stage_task.result()
    except facade.TestPatchIsolationError as exc:
        if stage_name == "test_patch_injection":
            state.injected_paths = list(
                dict.fromkeys((*state.injected_paths, *exc.touched_paths))
            )
        _mark_late_stage_failure(state, stage_name)
        state.error = facade._append_harness_error(
            state.error,
            f"late {stage_name} failed",
            exc,
        )
    except asyncio.CancelledError:
        _mark_late_stage_failure(state, stage_name)
    except BaseException as exc:
        state.error = facade._append_harness_error(
            state.error,
            f"late {stage_name} failed",
            exc,
        )
        _mark_late_stage_failure(state, stage_name)
    else:
        _record_late_stage_value(state, stage_name, late_value)


def collect_late_stage_outcomes(
    facade: Any,
    state: FinalizationState,
    stage_tasks: dict[str, asyncio.Task[Any]],
    observed_stage_results: set[str],
) -> None:
    if not state.execution_quiesced:
        return
    for stage_name, stage_task in stage_tasks.items():
        if stage_name not in observed_stage_results:
            observed_stage_results.add(stage_name)
            _adopt_late_stage(facade, state, stage_name, stage_task)


async def _abort_checkpoint(
    facade: Any,
    state: FinalizationState,
    checkpoint: Any,
    guard: TeardownGuard,
    cleanup_timeout: float,
) -> bool:
    try:
        return await guard.wait(checkpoint.abort(timeout=cleanup_timeout))
    except Exception as exc:
        state.error = facade._append_harness_error(
            state.error,
            "checkpoint abort failed",
            exc,
        )
        return False


def _record_checkpoint_abort_timeout(facade: Any, state: FinalizationState) -> None:
    state.error = facade._append_harness_error(
        state.error,
        "checkpoint abort timed out",
        TimeoutError("periodic checkpoint capture remained active"),
    )


async def _handle_nonquiescent_execution(
    facade: Any,
    state: FinalizationState,
    *,
    env: Any,
    checkpoint: Any,
    guard: TeardownGuard,
    cleanup_timeout: float,
) -> None:
    if state.execution_quiesced:
        return
    state.error = facade._append_harness_error(
        state.error,
        "execution cleanup timed out",
        TimeoutError(
            "owned execution did not quiesce after cancellation; patch extraction skipped"
        ),
    )
    if checkpoint is not None:
        quiesced = await _abort_checkpoint(
            facade, state, checkpoint, guard, cleanup_timeout
        )
        if state.checkpoint_result is None:
            state.checkpoint_result = {}
        state.checkpoint_result["abort"] = {
            "status": (
                "aborted_non_quiescent_execution"
                if quiesced
                else "checkpoint_abort_timed_out"
            )
        }
        if not quiesced:
            _record_checkpoint_abort_timeout(facade, state)
    if env is not None:
        env._aborted = True


async def _handle_unsafe_checkpoint(
    facade: Any,
    state: FinalizationState,
    *,
    env: Any,
    checkpoint: Any,
    guard: TeardownGuard,
    cleanup_timeout: float,
) -> bool:
    unsafe = (
        state.test_patch_isolation_failed
        or not state.checkpoint_restore_integrity_proven
    )
    if not (state.execution_quiesced and env and checkpoint is not None and unsafe):
        return unsafe
    quiesced = await _abort_checkpoint(
        facade, state, checkpoint, guard, cleanup_timeout
    )
    if state.checkpoint_result is None:
        state.checkpoint_result = {}
    skipped = (
        "skipped_test_patch_isolation_failure"
        if state.test_patch_isolation_failed
        else "skipped_checkpoint_restore_integrity_failure"
    )
    state.checkpoint_result["final"] = {
        "status": skipped if quiesced else "checkpoint_abort_timed_out"
    }
    if not quiesced:
        _record_checkpoint_abort_timeout(facade, state)
        state.execution_quiesced = False
    return unsafe


async def _finalize_safe_checkpoint(
    facade: Any,
    state: FinalizationState,
    *,
    env: Any,
    checkpoint: Any,
    harness_artifact_paths: list[str],
    guard: TeardownGuard,
    cleanup_timeout: float,
) -> None:
    try:
        finalized, final_checkpoint, lingering = await guard.wait(
            facade._stop_checkpoint_bounded(
                checkpoint,
                env,
                exclude_paths=(*state.injected_paths, *harness_artifact_paths),
                cleanup_timeout=cleanup_timeout,
            )
        )
        state.checkpoint_lingering.update(lingering)
        if state.checkpoint_result is None:
            state.checkpoint_result = {}
        if finalized:
            state.checkpoint_result["final"] = final_checkpoint.to_dict()
            return
        state.checkpoint_result["final"] = {
            "status": "checkpoint_finalization_timed_out"
        }
        state.error = facade._append_harness_error(
            state.error,
            "checkpoint finalization timed out",
            TimeoutError("checkpoint stop remained active"),
        )
        if not await _abort_checkpoint(
            facade, state, checkpoint, guard, cleanup_timeout
        ):
            _record_checkpoint_abort_timeout(facade, state)
        env._aborted = True
        state.execution_quiesced = False
    except Exception as exc:
        state.error = facade._append_harness_error(
            state.error,
            "checkpoint finalization failed",
            exc,
        )


async def finalize_checkpoint_and_patch(
    facade: Any,
    state: FinalizationState,
    *,
    env: Any,
    checkpoint: Any,
    harness_artifact_paths: list[str],
    cleanup_timeout: float,
    guard: TeardownGuard,
    defer_patch_extraction: bool,
) -> None:
    await _handle_nonquiescent_execution(
        facade,
        state,
        env=env,
        checkpoint=checkpoint,
        guard=guard,
        cleanup_timeout=cleanup_timeout,
    )
    unsafe = await _handle_unsafe_checkpoint(
        facade,
        state,
        env=env,
        checkpoint=checkpoint,
        guard=guard,
        cleanup_timeout=cleanup_timeout,
    )
    if state.execution_quiesced and env and checkpoint is not None and not unsafe:
        await _finalize_safe_checkpoint(
            facade,
            state,
            env=env,
            checkpoint=checkpoint,
            harness_artifact_paths=harness_artifact_paths,
            guard=guard,
            cleanup_timeout=cleanup_timeout,
        )
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
        await_teardown=guard.wait,
        defer_patch_extraction=defer_patch_extraction,
    )


async def finalize_eval_run(
    facade: Any,
    prepared: Any,
    execution: Any,
    *,
    workflow: Any,
    defer_patch_extraction: bool = False,
) -> Any:
    guard = TeardownGuard(
        facade,
        error=execution.error,
        cancellation=execution.cancellation,
    )
    quiesced, persistence_succeeded, final_lingering = await settle_execution(
        facade,
        execution,
        cleanup_timeout=prepared.cleanup_timeout,
        guard=guard,
    )
    state = FinalizationState(
        error=guard.error,
        execution_quiesced=quiesced,
        checkpoint_result=execution.checkpoint_result,
        injected_paths=execution.injected_paths,
        harness_artifact_exclusion_proven=execution.harness_artifact_exclusion_proven,
        checkpoint_restore_integrity_proven=execution.checkpoint_restore_integrity_proven,
        test_patch_isolation_failed=execution.test_patch_isolation_failed,
        task_stage_integrity_proven=execution.task_stage_integrity_proven,
        persistence_succeeded=persistence_succeeded,
        final_snapshot_lingering=final_lingering,
    )
    guard.bind(state)
    collect_late_stage_outcomes(
        facade,
        state,
        execution.stage_tasks,
        execution.observed_stage_results,
    )
    await finalize_checkpoint_and_patch(
        facade,
        state,
        env=execution.env,
        checkpoint=execution.checkpoint,
        harness_artifact_paths=execution.harness_artifact_paths,
        cleanup_timeout=prepared.cleanup_timeout,
        guard=guard,
        defer_patch_extraction=defer_patch_extraction,
    )
    await persist_manifest(
        facade,
        state,
        task=execution.task,
        workflow=workflow,
        run_dir=prepared.run_dir,
        workflow_ctx=execution.workflow_ctx,
        cleanup_timeout=prepared.cleanup_timeout,
        guard=guard,
    )
    dependencies = collect_live_dependencies(state, execution=execution)
    await release_resources(
        facade,
        state,
        dependencies=dependencies,
        tracer=prepared.tracer,
        env=execution.env,
        cleanup_timeout=prepared.cleanup_timeout,
        guard=guard,
    )
    result = build_eval_result(
        facade,
        state,
        task=execution.task,
        workflow_ctx=execution.workflow_ctx,
        session=execution.session,
        tracer=prepared.tracer,
        duration=time.monotonic() - prepared.start,
    )
    if guard.cancellation is not None:
        if state.error:
            add_exception_note(
                guard.cancellation,
                f"evaluation teardown diagnostics: {state.error}",
            )
        raise guard.cancellation
    return result
