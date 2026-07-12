"""Public orchestration entry point for one evaluator task."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from typing import Any

from opencollab.harness.evaluator_task_execution import (
    ExecutionConfig,
    execute_eval_run,
)
from opencollab.harness.evaluator_task_finalization import finalize_eval_run
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
    max_output_tokens: int,
    thinking: bool,
    thinking_params: dict | None,
    checkpoint_interval_seconds: float | None,
    resume_from_checkpoint: bool,
    cancellation_cleanup_timeout: float,
    defer_patch_extraction: bool,
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
    config = ExecutionConfig(
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        output_dir=output_dir,
        prompt=prompt,
        max_steps=prepared.max_steps,
        workflow=workflow,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params,
        resume_from_checkpoint=resume_from_checkpoint,
    )
    execution = await execute_eval_run(
        facade,
        prepared,
        config,
        tools_factory=tools_factory,
        env_factory=env_factory,
    )
    return await finalize_eval_run(
        facade,
        prepared,
        execution,
        workflow=workflow,
        defer_patch_extraction=defer_patch_extraction,
    )
