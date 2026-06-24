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
import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.adapters.env import DockerEnvironment, Environment, LocalEnvironment
from opencollab.adapters.repo_map import build_repo_map_via_env
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.tools.apply_patch import ApplyPatchTool
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.adapters.tools.git_diff import GitDiffTool
from opencollab.adapters.tools.run_tests import RunTestsTool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap import build_session
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent
from opencollab.harness.test_injection import apply_test_patch

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
    # Observability: count of LLM calls whose kimi tool-call markup was recovered
    # from literal text (P6), summed across every session of the run. Surfaces in
    # metrics.jsonl via asdict() — a regression alarm if it spikes (provider
    # quirk worsening) or drops to zero unexpectedly (recovery silently broke).
    markup_recovered: int = 0


@dataclass
class EvalTask:
    """A single evaluation task (e.g., one SWE-bench instance)."""

    task_id: str
    description: str  # Issue/problem description
    repo_path: str | None = None  # Path to repo (for local env)
    docker_image: str | None = None  # Docker image (for container env)
    timeout: float = 600.0  # Max seconds per task
    max_tokens: int = 100_000
    # Generic benchmark passthrough — never interpreted by the harness core, only
    # forwarded into the workflow args. SWE-bench uses it to thread the
    # ``test_patch`` (injected before the run) and parsed ``fail_to_pass`` ids.
    extras: dict | None = None


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

    When ``save_dir`` is set, each session's conversation is autosaved per role
    (``<seq>_<role>.json``) into the task's run folder — mirroring the team /
    CLI-workflow layout so an eval workflow reads as its roles, not one flat
    trajectory. ``None`` keeps sessions ephemeral (the prior behaviour).
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
        top_p: float | None = DEFAULT_TOP_P,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        save_dir: str | None = None,
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
        self._top_p = top_p
        self._thinking = thinking
        self._thinking_params = (
            thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        )
        self._save_dir = save_dir
        self._session_seq = 0

    def _next_save_path(self, label: str | None) -> str | None:
        """Per-session transcript path within the task run folder, or ``None``.

        ``<save_dir>/<seq>_<role>.json``. The sequence counter orders sessions
        by creation and disambiguates a role that runs more than once; bumping
        it has no ``await`` so it is atomic under cooperative scheduling even
        when ``parallel``/``pipeline`` build many sessions concurrently.
        """
        if self._save_dir is None:
            return None
        seq = self._session_seq
        self._session_seq += 1
        return workflow_transcript_path(self._save_dir, seq, label)

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: str | None = None,
        thinking: bool | None = None,
    ) -> Session:
        # ``thinking`` None -> run-wide default; an explicit value (False for the
        # schema-only structured agents) overrides it to shorten their slow
        # reasoning generations.
        use_thinking = self._thinking if thinking is None else thinking
        agent = Agent(
            name="eval_agent",
            system_prompt=self._prompt,
            tools=list(tools) if tools is not None else list(self._default_toolset),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            top_p=self._top_p,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        return build_session(
            agent=agent,
            env=self._env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            max_steps=self._max_steps,
            auto_save_path=self._next_save_path(label),
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
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    save_dir: str | None = None,
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
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
        save_dir=save_dir,
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
    top_p: float | None = DEFAULT_TOP_P,
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
        top_p=top_p,
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
    injected_paths: Sequence[str] | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    top_p: float | None = DEFAULT_TOP_P,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    save_dir: str | None = None,
) -> WorkflowContext:
    """Run ``workflow`` over a task-bound context; return the context.

    The context's session factory is bound to the shared task env, so each
    workflow agent sees cumulative changes and the final ``git diff`` aggregates
    them. The shared env is attached as ``ctx.env`` (a harness convention) so
    harness-layer workflows can read the working-tree diff. ``tokens_used`` /
    ``steps`` are aggregated by the caller across every session created.

    ``save_dir`` (the task's run folder) is threaded to the factory so each
    session's conversation is autosaved per role (``<seq>_<role>.json``).
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
        top_p=top_p,
        thinking=thinking,
        thinking_params=thinking_params,
        save_dir=save_dir,
    )
    # Wall-clock deadline on the monotonic clock: the workflow checks
    # ``ctx.time_low()`` and bails to a forced final write before the
    # ``asyncio.wait_for`` wall below truncates the run (P7). ``task.timeout`` is
    # the hard wall (1800s for analyst-solve); the workflow leaves itself
    # ``DEFAULT_DEADLINE_MARGIN_SECONDS`` of head-room inside it.
    deadline = time.monotonic() + task.timeout
    ctx = WorkflowContext(
        factory,
        tracer=tracer,
        budget_total=task.max_tokens,
        tree_probe=EnvWorkingTreeProbe(env),
        deadline_monotonic=deadline,
    )
    ctx.env = env  # type: ignore[attr-defined] — harness seam for workflows
    args = {"task_id": task.task_id, "description": task.description}
    # Forward benchmark passthrough (e.g. SWE-bench fail_to_pass ids + the paths
    # of any injected test files) so the workflow can scope to the target tests.
    if task.extras:
        args.update(task.extras)
    # The F2P hard-gate (D2) keys on ``fail_to_pass`` non-emptiness and demands the
    # agent run those exact node-ids. That is only satisfiable when the tests were
    # actually injected: if injection FAILED (``injected_paths`` empty) the tests
    # do not exist at the base commit, so forwarding the ids would make the gate
    # unsatisfiable rather than bypassed. Couple the two — drop ``fail_to_pass``
    # when nothing was injected so the gate falls back to the trusted-verdict path
    # its docstring describes.
    if not injected_paths:
        args.pop("fail_to_pass", None)
    else:
        args["injected_test_paths"] = list(injected_paths)
    # ALWAYS return the ctx, even when the workflow ends abnormally. The ctx is
    # already fully built (above) and its ``.sessions`` accumulate token+step
    # metrics as agents run, so by the time the body raises it holds the real
    # cost of the run AND a partial patch sits on disk. Letting the exception
    # propagate to the caller would leave ``workflow_ctx`` None there and zero out
    # both — the regression that lost django-11564 (an outer-wall timeout) and the
    # sympy budget-floor runs. Catch the controlled-stop cases here and return ctx.
    try:
        await asyncio.wait_for(workflow(ctx, args), timeout=task.timeout)
    except WorkflowBudgetExceeded as exc:
        # Budget floor stopping the run is BY DESIGN, not a failure: prior coder
        # rounds / the forced final write have already written a real patch, and
        # ctx holds every session's metrics. Not surfaced as an error.
        await ctx.log(f"workflow stopped at budget floor — {exc}")
    except Exception as exc:  # noqa: BLE001 — the harness must never lose a run
        # Outer wall (asyncio.TimeoutError) or an unexpected crash: keep the ctx so
        # the partial on-disk patch + accumulated metrics survive. Record the cause
        # for observability; patch_produced stays honest off the real on-disk diff.
        ctx.workflow_error = (  # type: ignore[attr-defined] — harness seam
            f"Task timed out after {task.timeout}s"
            if isinstance(exc, asyncio.TimeoutError)
            else f"{type(exc).__name__}: {exc}"
        )
        await ctx.log(f"workflow ended early — {ctx.workflow_error}")
    return ctx


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "used_tokens", 0)) for s in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "step_count", 0)) for s in sessions)


def _aggregate_markup_recovery(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "markup_recovered", 0)) for s in sessions)


def _write_eval_workflow_manifest(
    run_dir: str,
    *,
    task: EvalTask,
    workflow: WorkflowFn,
    ctx: WorkflowContext,
) -> None:
    """Write ``<run_dir>/workflow.json`` summarising an eval workflow run.

    Best-effort: a manifest-write failure must never drop the result we just
    computed, so any error is swallowed (the trajectory + transcripts are
    already on disk).
    """
    manifest = {
        "workflow": getattr(workflow, "__name__", "workflow"),
        "task_id": task.task_id,
        "sessions": len(ctx.sessions),
        "tokens_spent": ctx.budget.spent(),
        "budget_total": ctx.budget.total,
    }
    try:
        SessionStore().save_manifest(
            os.path.join(run_dir, WORKFLOW_MANIFEST_FILENAME), manifest
        )
    except Exception:  # noqa: BLE001 — manifest is observability, never fatal
        pass


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
    top_p: float | None = DEFAULT_TOP_P,
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
    trajectories_dir = os.path.join(output_dir, "trajectories")
    # Workflow mode gets its own per-task run folder (mirroring a team / CLI
    # workflow run): per-role ``<seq>_<role>.json`` conversations + one
    # ``orchestration.jsonl`` for the scheduling/step signals + a ``workflow.json``
    # manifest. Single-session mode is unchanged — one flat ``<task_id>.jsonl``.
    run_dir: str | None = None
    if workflow is None:
        tracer = Tracer(run_id=task.task_id, output_dir=trajectories_dir)
    else:
        run_dir = os.path.join(trajectories_dir, task.task_id)
        tracer = Tracer(
            run_id=task.task_id, output_dir=run_dir, filename=ORCHESTRATION_FILENAME
        )

    env: Environment | None = None
    session: Session | None = None
    workflow_ctx: WorkflowContext | None = None
    error: str | None = None
    patch = ""
    # Paths of any injected benchmark test files — checked out before patch
    # extraction so they never contaminate the submitted model_patch.
    injected_paths: list[str] = []

    try:
        # Create environment (inside try — docker setup can fail)
        env = await env_factory(task)
        tools = list(tools_factory())

        # SWE-bench test injection: apply the real FAIL_TO_PASS test into the
        # workspace BEFORE the workflow runs so the agent can verify against it.
        # Guarded on extras so single-session / non-SWE-bench paths are
        # unaffected. A bad patch never aborts the run (apply_test_patch returns
        # [] on failure).
        test_patch = (task.extras or {}).get("test_patch")
        if test_patch:
            injected_paths = await apply_test_patch(env, test_patch)

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
                top_p=top_p,
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
                injected_paths=injected_paths,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                thinking_params=thinking_params,
                save_dir=run_dir,
            )

    except asyncio.TimeoutError:
        error = f"Task timed out after {task.timeout}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    # Diff-exclusion (load-bearing): revert any injected benchmark test files
    # before extracting the patch so the submitted model_patch NEVER contains the
    # test edits. The grader applies its own test_patch; leaving these in would
    # cause a double-apply / conflict at grading time.
    #
    # Done per-path so a single failure can't abort the rest, and a test_patch
    # that ADDS a new file (the common SWE-bench case) is handled too. The real
    # SWE-bench driver extracts via ``git add -A && git diff --cached``, so an
    # untracked injected file would otherwise be staged and leak: ``git checkout``
    # only restores TRACKED paths and errors on an untracked one, so we follow it
    # with ``git clean -fq`` to remove any still-untracked (newly added) injected
    # file. ``git clean`` is a no-op on a path with no untracked content, so it is
    # safe to run unconditionally for both modified-existing and added files.
    if env and injected_paths:
        for path in injected_paths:
            quoted = shlex.quote(path)
            try:
                await env.exec_cmd(f"git checkout -- {quoted}")
                await env.exec_cmd(f"git clean -fq -- {quoted}")
            except Exception:
                pass

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
        markup_recovered = _aggregate_markup_recovery(sessions)
        # Tie the run folder's per-role transcripts to the workflow that produced
        # them, mirroring the team ``team.json`` / CLI ``workflow.json`` manifest.
        if run_dir is not None:
            _write_eval_workflow_manifest(run_dir, task=task, workflow=workflow, ctx=workflow_ctx)
        # _run_workflow_mode now swallows abnormal endings to preserve metrics; it
        # stashes any genuine fault here so it still surfaces in the result.
        if error is None:
            error = getattr(workflow_ctx, "workflow_error", None)
    else:
        tokens_used = session.used_tokens if session else 0
        steps = session.step_count if session else 0
        markup_recovered = getattr(session, "markup_recovered", 0) if session else 0

    return EvalResult(
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
