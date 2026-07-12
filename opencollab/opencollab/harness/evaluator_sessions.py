from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from opencollab.adapters.env import Environment
from opencollab.adapters.llm.types import DEFAULT_MAX_OUTPUT_TOKENS
from opencollab.adapters.tools.base import Tool
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.async_timeout import CallerTimeoutError, abandon_on_timeout
from opencollab.application.session import Session
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import WorkflowFn
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.session_factory import workflow_transcript_path
from opencollab.domain.agent import Agent

if TYPE_CHECKING:
    from opencollab.harness.evaluator import EvalTask


def _evaluator_module():
    return sys.modules["opencollab.harness.evaluator"]


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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
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
        self._max_output_tokens = max_output_tokens
        self._thinking = thinking
        self._thinking_params = thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
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
            max_tokens_per_step=self._max_output_tokens,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        return _evaluator_module().build_session(
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
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
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
        max_output_tokens=max_output_tokens,
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
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    session_holder: list[Session] | None = None,
    owned_tasks: list[asyncio.Task[Any]] | None = None,
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
        max_tokens_per_step=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS),
    )
    session = _evaluator_module().build_session(
        agent=agent,
        env=env,
        tracer=tracer,
        max_budget_tokens=task.max_tokens,
        max_steps=max_steps,
    )
    if session_holder is not None:
        session_holder.append(session)
    deadline = time.monotonic() + task.timeout
    add_message_task = asyncio.create_task(session.add_user_message(task.description))
    if owned_tasks is not None:
        owned_tasks.append(add_message_task)
    await abandon_on_timeout(add_message_task, task.timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CallerTimeoutError
    run_task = asyncio.create_task(session.run_loop())
    if owned_tasks is not None:
        owned_tasks.append(run_task)
    await abandon_on_timeout(run_task, remaining)
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
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    thinking: bool = DEFAULT_THINKING,
    thinking_params: dict | None = None,
    save_dir: str | None = None,
    context_holder: list[WorkflowContext] | None = None,
    owned_tasks: list[asyncio.Task[Any]] | None = None,
    timeout_error_seconds: float | None = None,
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
    factory = _evaluator_module()._build_eval_session_factory(
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
        max_output_tokens=max_output_tokens,
        thinking=thinking,
        thinking_params=thinking_params,
        save_dir=save_dir,
    )
    # Wall-clock deadline on the monotonic clock: the workflow checks
    # ``ctx.time_low()`` and bails to a forced final write before the
    # the caller deadline below truncates the run (P7). ``task.timeout`` is
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
    if context_holder is not None:
        context_holder.append(ctx)
    ctx.env = env  # type: ignore[attr-defined] — harness seam for workflows
    args = dict(task.extras or {})
    args.update({"task_id": task.task_id, "description": task.description})
    # Forward benchmark passthrough (e.g. SWE-bench fail_to_pass ids + the paths
    # of any injected test files) so the workflow can scope to the target tests.
    args.pop("injected_test_paths", None)
    # Preserve every declared FAIL_TO_PASS id even when no test patch was
    # supplied or injection produced no paths. The workflow must execute the
    # exact targets before it may report PASS; unavailable targets therefore
    # remain a technical failure instead of silently bypassing the hard gate.
    if injected_paths:
        args["injected_test_paths"] = list(injected_paths)
    # ALWAYS return the ctx, even when the workflow ends abnormally. The ctx is
    # already fully built (above) and its ``.sessions`` accumulate token+step
    # metrics as agents run, so by the time the body raises it holds the real
    # cost of the run AND a partial patch sits on disk. Letting the exception
    # propagate to the caller would leave ``workflow_ctx`` None there and zero out
    # both — the regression that lost django-11564 (an outer-wall timeout) and the
    # sympy budget-floor runs. Catch the controlled-stop cases here and return ctx.
    workflow_task = asyncio.create_task(workflow(ctx, args))
    if owned_tasks is not None:
        owned_tasks.append(workflow_task)
    try:
        ctx.workflow_result = await abandon_on_timeout(workflow_task, task.timeout)  # type: ignore[attr-defined]
    except WorkflowBudgetExceeded as exc:
        # Budget floor stopping the run is BY DESIGN, not a failure: prior coder
        # rounds / the forced final write have already written a real patch, and
        # ctx holds every session's metrics. Not surfaced as an error.
        await ctx.log(f"workflow stopped at budget floor — {exc}")
    except CallerTimeoutError:
        reported_timeout = task.timeout if timeout_error_seconds is None else timeout_error_seconds
        ctx.workflow_error = f"Task timed out after {reported_timeout}s"  # type: ignore[attr-defined]
        await ctx.log(f"workflow ended early — {ctx.workflow_error}")
    except Exception as exc:  # noqa: BLE001 — the harness must never lose a run
        # Provider/session failures keep the ctx so
        # the partial on-disk patch + accumulated metrics survive. Record the cause
        # for observability; patch_produced stays honest off the real on-disk diff.
        ctx.workflow_error = f"{type(exc).__name__}: {exc}"  # type: ignore[attr-defined]
        await ctx.log(f"workflow ended early — {ctx.workflow_error}")
    return ctx


def _aggregate_tokens(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "used_tokens", 0)) for s in sessions)


def _aggregate_steps(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "step_count", 0)) for s in sessions)


def _aggregate_markup_recovery(sessions: Sequence[Any]) -> int:
    return sum(int(getattr(s, "markup_recovered", 0)) for s in sessions)
