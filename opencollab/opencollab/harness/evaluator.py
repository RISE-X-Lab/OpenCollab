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

from opencollab.adapters.env import DockerEnvironment, Environment, LocalEnvironment
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.trace import Tracer
from opencollab.application.session import Session
from opencollab.bootstrap import build_session
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
    """Build the default tool set for an eval agent."""
    return [BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]


async def default_env_factory(task: EvalTask) -> Environment:
    """Build the default environment for a task (docker if imaged, else local)."""
    if task.docker_image:
        env = DockerEnvironment(image=task.docker_image)
        await env.setup(mount_dir=task.repo_path)
        return env
    return LocalEnvironment(workspace=task.repo_path or ".")


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
) -> EvalResult:
    """Run a single evaluation task.

    Returns an EvalResult with the git patch of all changes made.
    """
    os.makedirs(output_dir, exist_ok=True)
    start = time.monotonic()
    tracer = Tracer(run_id=task.task_id, output_dir=os.path.join(output_dir, "trajectories"))

    env: Environment | None = None
    session: Session | None = None
    error: str | None = None
    patch = ""

    try:
        # Create environment (inside try — docker setup can fail)
        env = await env_factory(task)

        agent = Agent(
            name="eval_agent",
            system_prompt=prompt,
            tools=list(tools_factory()),
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
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

    return EvalResult(
        task_id=task.task_id,
        patch=patch,
        patch_produced=bool(patch.strip()) and error is None,
        tokens_used=session.used_tokens if session else 0,
        steps=session.step_count if session else 0,
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
