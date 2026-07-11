"""Owned execution stages for one evaluator task."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout


@dataclass(frozen=True)
class ExecutionConfig:
    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    output_dir: str
    prompt: str
    max_steps: int
    workflow: Any
    temperature: float
    top_p: float | None
    thinking: bool
    thinking_params: dict | None
    resume_from_checkpoint: bool


@dataclass
class ExecutionState:
    task: Any
    env: Any = None
    session: Any = None
    workflow_ctx: Any = None
    session_holder: list[Any] = field(default_factory=list)
    workflow_context_holder: list[Any] = field(default_factory=list)
    owned_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    stage_tasks: dict[str, asyncio.Task[Any]] = field(default_factory=dict)
    observed_stage_results: set[str] = field(default_factory=set)
    environment_setup_owner: Any = None
    checkpoint: Any = None
    checkpoint_result: dict[str, Any] | None = None
    error: str | None = None
    cancellation: asyncio.CancelledError | None = None
    injected_paths: list[str] = field(default_factory=list)
    harness_artifact_paths: list[str] = field(default_factory=list)
    harness_artifact_exclusion_proven: bool = True
    checkpoint_restore_integrity_proven: bool = True
    test_patch_isolation_failed: bool = False
    task_stage_integrity_proven: bool = True


class StageController:
    """Own deadline-bound stage tasks and their observation evidence."""

    def __init__(self, *, deadline: float, state: ExecutionState):
        self._deadline = deadline
        self._state = state

    def remaining_time(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise CallerTimeoutError
        return remaining

    async def run(self, stage_name: str, awaitable: Awaitable[Any]) -> Any:
        stage_task = asyncio.ensure_future(awaitable)
        self._state.owned_tasks.append(stage_task)
        self._state.stage_tasks[stage_name] = stage_task
        try:
            result = await abandon_on_timeout(stage_task, self.remaining_time())
        except CallerTimeoutError:
            self._state.task_stage_integrity_proven = False
            if not stage_task.done():
                stage_task.cancel()
            raise
        except BaseException:
            if stage_task.done():
                self._state.observed_stage_results.add(stage_name)
            raise
        self._state.observed_stage_results.add(stage_name)
        return result


async def acquire_environment(
    facade: Any,
    state: ExecutionState,
    controller: StageController,
    *,
    env_factory: Callable[[Any], Awaitable[Any]],
    cleanup_timeout: float,
) -> None:
    owner = facade._EnvironmentSetupOwner(
        env_factory,
        state.task,
        cleanup_timeout=cleanup_timeout,
    )
    state.environment_setup_owner = owner
    state.owned_tasks.append(owner.task)
    # Let the owner enter the factory before applying the caller deadline.  A
    # task cancelled before its first step cannot run the factory's cancellation
    # cleanup, which makes timeout behavior depend on scheduler load.
    await asyncio.sleep(0)
    try:
        state.env = await owner.acquire(controller.remaining_time())
    except CallerTimeoutError:
        state.task_stage_integrity_proven = False
        owner.relinquish()
        raise
    except BaseException:
        owner.relinquish()
        raise
    owner.transfer(state.env)


def resolve_harness_artifacts(
    facade: Any,
    state: ExecutionState,
    *,
    output_dir: str,
    trajectories_dir: str,
) -> None:
    candidates: list[str | os.PathLike[str]] = list(
        state.task.harness_artifact_paths
    )
    output_relative = facade._workspace_relative_host_path(state.env, output_dir)
    if output_relative is not None and output_relative != Path("."):
        candidates.append(output_dir)
    else:
        candidates.extend(
            (
                trajectories_dir,
                os.path.join(output_dir, "results.jsonl"),
                os.path.join(output_dir, facade.RESULT_TEMP_DIRECTORY),
            )
        )
        if output_relative == Path("."):
            legacy_paths, scan_complete = facade._legacy_result_temp_paths(output_dir)
            candidates.extend(legacy_paths)
            state.harness_artifact_exclusion_proven = scan_complete
    state.harness_artifact_paths = facade._workspace_relative_artifact_paths(
        state.env,
        candidates,
    )
    bound_error = facade._mapped_artifact_path_bound_error(
        state.harness_artifact_paths
    )
    if bound_error:
        state.harness_artifact_exclusion_proven = False
        raise RuntimeError(bound_error)
    if not state.harness_artifact_exclusion_proven:
        raise RuntimeError("legacy result temp artifact scan exceeded its safety bound")


async def prepare_checkpoint_and_test_patch(
    facade: Any,
    state: ExecutionState,
    controller: StageController,
    *,
    run_dir: str | None,
    checkpoint_interval: float | None,
    resume_from_checkpoint: bool,
) -> None:
    if run_dir is not None and checkpoint_interval is not None:
        state.checkpoint = facade.WorktreeCheckpoint(
            Path(run_dir),
            interval_seconds=checkpoint_interval,
        )
        if resume_from_checkpoint:
            restore_result = await controller.run(
                "checkpoint_restore",
                state.checkpoint.restore_latest(
                    state.env,
                    exclude_paths=state.harness_artifact_paths,
                ),
            )
            state.checkpoint_result = {"restore": restore_result.to_dict()}
            state.checkpoint_restore_integrity_proven = (
                restore_result.worktree_integrity_proven
            )
            if not state.checkpoint_restore_integrity_proven:
                raise RuntimeError(
                    "checkpoint restore left worktree integrity unproven"
                )
    await _inject_test_patch(facade, state, controller)
    if state.checkpoint is not None:
        controller.remaining_time()
        await state.checkpoint.start(
            state.env,
            exclude_paths=(
                *state.injected_paths,
                *state.harness_artifact_paths,
            ),
        )


async def _inject_test_patch(
    facade: Any,
    state: ExecutionState,
    controller: StageController,
) -> None:
    test_patch = (state.task.extras or {}).get("test_patch")
    if not test_patch:
        return
    try:
        state.injected_paths = await controller.run(
            "test_patch_injection",
            facade.apply_test_patch(state.env, test_patch),
        )
    except facade.TestPatchIsolationError as exc:
        state.injected_paths = list(dict.fromkeys(exc.touched_paths))
        state.test_patch_isolation_failed = True
        if exc.cancellation is not None:
            raise exc.cancellation from exc
        raise


async def run_session_or_workflow(
    facade: Any,
    state: ExecutionState,
    controller: StageController,
    config: ExecutionConfig,
    *,
    tools: list[Any],
    tracer: Any,
    run_dir: str | None,
) -> None:
    repo_map = await controller.run(
        "repo_map",
        facade.build_repo_map_via_env(state.env),
    )
    prompt = f"{config.prompt}\n\n{repo_map}" if repo_map else config.prompt
    execution_task = replace(state.task, timeout=controller.remaining_time())
    if config.workflow is None:
        state.session = await facade._run_single_session(
            task=execution_task,
            env=state.env,
            tracer=tracer,
            prompt=prompt,
            tools=tools,
            model=config.model,
            provider=config.provider,
            api_key=config.api_key,
            base_url=config.base_url,
            max_steps=config.max_steps,
            temperature=config.temperature,
            top_p=config.top_p,
            thinking=config.thinking,
            thinking_params=config.thinking_params,
            session_holder=state.session_holder,
            owned_tasks=state.owned_tasks,
        )
        return
    state.workflow_ctx = await facade._run_workflow_mode(
        task=execution_task,
        env=state.env,
        tracer=tracer,
        prompt=prompt,
        tools=tools,
        model=config.model,
        provider=config.provider,
        api_key=config.api_key,
        base_url=config.base_url,
        max_steps=config.max_steps,
        workflow=config.workflow,
        injected_paths=state.injected_paths,
        temperature=config.temperature,
        top_p=config.top_p,
        thinking=config.thinking,
        thinking_params=config.thinking_params,
        save_dir=run_dir,
        context_holder=state.workflow_context_holder,
        owned_tasks=state.owned_tasks,
        timeout_error_seconds=state.task.timeout,
    )


async def execute_eval_run(
    facade: Any,
    prepared: Any,
    config: ExecutionConfig,
    *,
    tools_factory: Callable[[], Any],
    env_factory: Callable[[Any], Awaitable[Any]],
) -> ExecutionState:
    state = ExecutionState(task=prepared.task)
    controller = StageController(deadline=prepared.task_deadline, state=state)
    try:
        await acquire_environment(
            facade,
            state,
            controller,
            env_factory=env_factory,
            cleanup_timeout=prepared.cleanup_timeout,
        )
        controller.remaining_time()
        tools = list(tools_factory())
        controller.remaining_time()
        resolve_harness_artifacts(
            facade,
            state,
            output_dir=config.output_dir,
            trajectories_dir=prepared.trajectories_dir,
        )
        await prepare_checkpoint_and_test_patch(
            facade,
            state,
            controller,
            run_dir=prepared.run_dir,
            checkpoint_interval=prepared.checkpoint_interval,
            resume_from_checkpoint=config.resume_from_checkpoint,
        )
        await run_session_or_workflow(
            facade,
            state,
            controller,
            config,
            tools=tools,
            tracer=prepared.tracer,
            run_dir=prepared.run_dir,
        )
    except asyncio.CancelledError as exc:
        state.cancellation = exc
        if getattr(exc, "checkpoint_restore_integrity_proven", True) is False:
            state.checkpoint_restore_integrity_proven = False
        state.error = "evaluation task cancelled"
    except CallerTimeoutError:
        state.error = f"Task timed out after {state.task.timeout}s"
    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"
    return state
