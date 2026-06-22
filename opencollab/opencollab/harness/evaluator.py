"""Headless Evaluation Runner — for SWE-bench and research benchmarks.

Provides a pure, non-interactive entry point for batch evaluation.
Each task runs in an isolated environment, produces a git patch, and
records a full trajectory for analysis.

Ref:
- Design doc: run_eval_task with Environment + issue_desc → patch
- Harness Engineering: standardized output, sandboxed execution, trajectory recording
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.adapters.env import DockerEnvironment, Environment, LocalEnvironment
from opencollab.adapters.repo_map import build_repo_map_via_env
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap import build_session
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
)
from opencollab.domain.agent import Agent

EnvFactory = Callable[["EvalTask"], Awaitable[Environment]]
ToolFactory = Callable[[], Sequence[Tool]]


@dataclass
class EvalResult:
    """Result of a single evaluation task."""

    task_id: str
    patch: str  # Git diff output
    patch_produced: bool
    tokens_used: int
    steps: int
    duration: float
    error: str | None = None
    trajectory_path: str | None = None


@dataclass
class EvalTask:
    """A single evaluation task (e.g., one SWE-bench instance)."""

    task_id: str
    description: str  # Issue/problem description
    repo_path: str | None = None  # Path to repo (for local env)
    docker_image: str | None = None  # Docker image (for container env)
    timeout: float = 600.0  # Max seconds per task
    max_tokens: int = 100_000


EVAL_AGENT_PROMPT = """\
You are an autonomous coding agent. Complete the following task by modifying the code.

Rules:
- Read relevant files before making changes.
- Make minimal, targeted changes to fix the issue.
- After making changes, verify them (run tests if available).
- When done, make sure all changes are saved. Do NOT commit.
"""

DEFAULT_MAX_STEPS = 80


def default_tools() -> list[Tool]:
    """Build the default tool set for an eval agent.

    Mirrors the curated single-agent surface used by team roles (coder +
    reviewer tools) so headless eval exercises the same toolset: the bash
    description deflects to run_tests/git_diff/grep, and apply_patch is the
    fallback when str_replace edits fail to match.
    """
    return [
        BashTool(),
        FileReadTool(),
        FileWriteTool(),
        ApplyPatchTool(),
        RunTestsTool(),
        GitDiffTool(),
        GrepTool(),
    ]


async def default_env_factory(task: EvalTask) -> Environment:
    """Build the default environment for a task (docker if imaged, else local)."""
    if task.docker_image:
        env = DockerEnvironment(image=task.docker_image)
        await env.setup(mount_dir=task.repo_path)
        return env
    return LocalEnvironment(workspace=task.repo_path or ".")


class _EvalSessionFactory:
    """``WorkflowSessionFactoryPort`` bound to one eval task's shared env.

    Every ``build_workflow_session`` call assembles a fresh one-shot ``Agent`` on
    the *same* task ``Environment`` (so each workflow agent sees the cumulative
    working-tree changes and the final ``git diff`` aggregates them) and the same
    tracer. The caller's ``tools`` override the default eval toolset when given.
    """

    def __init__(
        self,
        *,
        env: Environment,
        tracer: Tracer,
        prompt: str,
        model: str,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        max_steps: int,
        default_toolset: Sequence[Tool],
        temperature: float = DEFAULT_TEMPERATURE,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
    ) -> None:
        self._env = env
        self._tracer = tracer
        self._prompt = prompt
        self._model = model
        self._provider = provider
        self._api_key = api_key
        self._base_url = base_url
        self._max_steps = max_steps
        self._default_toolset = list(default_toolset)
        self._temperature = temperature
        self._thinking = thinking
        self._thinking_params = (
            thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        )

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
    ) -> Session:
        agent = Agent(
            name="eval_agent",
            system_prompt=self._prompt,
            tools=list(tools) if tools is not None else list(self._default_toolset),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            thinking=self._thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        return build_session(
            agent=agent,
            env=self._env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            max_steps=self._max_steps,
        )


def _build_eval_session_factory(
    *,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    default_toolset: Sequence[Tool],
    temperature: float = DEFAULT_TEMPERATURE,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
) -> _EvalSessionFactory:
    """Construct the per-task workflow session factory (seam for tests)."""
    return _EvalSessionFactory(
        env=env,
        tracer=tracer,
        prompt=prompt,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        default_toolset=default_toolset,
        temperature=temperature,
        thinking=thinking,
        thinking_params=thinking_params,
    )


async def _run_single_session(
    *,
    task: EvalTask,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    tools: Sequence[Tool],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    temperature: float = DEFAULT_TEMPERATURE,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
) -> Session:
    """Drive the unchanged single-session eval loop and return the session."""
    agent = Agent(
        name="eval_agent",
        system_prompt=prompt,
        tools=list(tools),
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        thinking=thinking,
        thinking_params=thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS),
    )
    session = build_session(
        agent=agent,
        env=env,
        tracer=tracer,
        max_budget_tokens=task.max_tokens,
        max_steps=max_steps,
    )
    await session.add_user_message(task.description)
    await asyncio.wait_for(session.run_loop(), timeout=task.timeout)
    return session


async def _run_workflow_mode(
    *,
    task: EvalTask,
    env: Environment,
    tracer: Tracer,
    prompt: str,
    tools: Sequence[Tool],
    model: str,
    provider: str,
    api_key: str | None,
    base_url: str | None,
    max_steps: int,
    workflow: WorkflowFn,
    temperature: float = DEFAULT_TEMPERATURE,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
) -> WorkflowContext:
    """Run ``workflow`` over a task-bound context; return the context.

    The context's session factory is bound to the shared task env, so each
    workflow agent sees cumulative changes and the final ``git diff`` aggregates
    them. The shared env is attached as ``ctx.env`` (a harness convention) so
    harness-layer workflows can read the working-tree diff. ``tokens_used`` /
    ``steps`` are aggregated by the caller across every session created.
    """
    factory = _build_eval_session_factory(
        env=env,
        tracer=tracer,
        prompt=prompt,
        model=model,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        max_steps=max_steps,
        default_toolset=tools,
        temperature=temperature,
        thinking=thinking,
        thinking_params=thinking_params,
    )
    ctx = WorkflowContext(
        factory,
        tracer=tracer,
        budget_total=task.max_tokens,
        tree_probe=EnvWorkingTreeProbe(env),
    )
    ctx.env = env  # type: ignore[attr-defined] — harness seam for workflows
    args = {"task_id": task.task_id, "description": task.description}
    await asyncio.wait_for(workflow(ctx, args), timeout=task.timeout)
    return ctx


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "used_tokens", 0)) for s in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "step_count", 0)) for s in sessions)


async def run_eval_task(
    task: EvalTask,
    model: str = "gpt-4o",
    provider: str = "openai",
    api_key: str | None = None,
    base_url: str | None = None,
    output_dir: str = "eval_results",
    prompt: str = EVAL_AGENT_PROMPT,
    tools_factory: ToolFactory = default_tools,
    env_factory: EnvFactory = default_env_factory,
    max_steps: int = DEFAULT_MAX_STEPS,
    workflow: WorkflowFn | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
) -> EvalResult:
    """Run a single evaluation task.

    With ``workflow=None`` (default) this drives one agent session — behavior is
    byte-for-byte unchanged. When a ``workflow`` is given, the task is instead
    orchestrated by that workflow function over a ``WorkflowContext`` whose
    session factory is bound to the task env / budget. ``tokens_used`` and
    ``steps`` then aggregate across *all* sessions the workflow created (sum
    semantics — a workflow's cost is the cost of every agent it ran). Patch
    extraction, timeout handling, and the ``EvalResult`` shape are identical in
    both modes.
    """
    os.makedirs(output_dir, exist_ok=True)
    start = time.monotonic()
    tracer = Tracer(run_id=task.task_id, output_dir=os.path.join(output_dir, "trajectories"))

    env: Environment | None = None
    session: Session | None = None
    workflow_ctx: WorkflowContext | None = None
    error: str | None = None
    patch = ""

    try:
        # Create environment (inside try — docker setup can fail)
        env = await env_factory(task)
        tools = list(tools_factory())

        # Orientation up front: a bounded repo map in the system prompt saves
        # the model its first N steps of ls/find exploration.
        repo_map = await build_repo_map_via_env(env)
        prompt = f"{prompt}\n\n{repo_map}" if repo_map else prompt

        if workflow is None:
            session = await _run_single_session(
                task=task,
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
                thinking=thinking,
                thinking_params=thinking_params,
            )
        else:
            workflow_ctx = await _run_workflow_mode(
                task=task,
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
                temperature=temperature,
                thinking=thinking,
                thinking_params=thinking_params,
            )

    except asyncio.TimeoutError:
        error = f"Task timed out after {task.timeout}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    # Extract git patch (best-effort even after errors)
    if env:
        try:
            patch_result = await env.exec_cmd("git diff")
            patch = patch_result.stdout
            if not patch.strip():
                patch_result = await env.exec_cmd("git diff HEAD")
                patch = patch_result.stdout
        except Exception:
            pass

    duration = time.monotonic() - start
    tracer.close()

    if env:
        try:
            await env.cleanup()
        except Exception:
            pass

    if workflow_ctx is not None:
        sessions = workflow_ctx.sessions
        tokens_used = _aggregate_tokens(sessions)
        steps = _aggregate_steps(sessions)
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0

    return EvalResult(
        task_id=task.task_id,
        patch=patch,
        patch_produced=bool(patch.strip()) and error is None,
        tokens_used=tokens_used,
        steps=steps,
        duration=duration,
        error=error,
        trajectory_path=tracer.path,
    )


async def run_eval_batch(
    tasks: list[EvalTask],
    concurrency: int = 4,
    **kwargs,
) -> list[EvalResult]:
    """Run multiple evaluation tasks with controlled concurrency.

    Individual task failures produce an EvalResult with error set,
    rather than aborting the entire batch.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(task: EvalTask) -> EvalResult:
        async with semaphore:
            try:
                return await run_eval_task(task, **kwargs)
            except Exception as e:
                return EvalResult(
                    task_id=task.task_id,
                    patch="",
                    patch_produced=False,
                    tokens_used=0,
                    steps=0,
                    duration=0.0,
                    error=f"Unhandled: {type(e).__name__}: {e}",
                )

    coros = [run_one(t) for t in tasks]
    results = await asyncio.gather(*coros)
    return results


def save_results(results: list[EvalResult], output_path: str) -> None:
    """Save evaluation results as JSONL."""
    with open(output_path, "w") as f:
        for r in results:
            record = {
                "task_id": r.task_id,
                "patch_produced": r.patch_produced,
                "tokens_used": r.tokens_used,
                "steps": r.steps,
                "duration": round(r.duration, 2),
                "error": r.error,
                "patch_lines": len(r.patch.splitlines()),
                "trajectory": r.trajectory_path,
            }
            f.write(json.dumps(record) + "\n")
