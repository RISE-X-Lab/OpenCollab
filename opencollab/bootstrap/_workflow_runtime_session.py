"""Workflow session construction and application-context wiring."""

from __future__ import annotations

import contextvars
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.providers import RESPONSES
from opencollab.adapters.llm.retry import RetryTimeBudget
from opencollab.adapters.llm.types import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    model_capabilities,
)
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap._workflow_runtime_state import WORKFLOW_AGENT_PROMPT
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_TOP_P,
    resolve_thinking_params,
)
from opencollab.bootstrap.session_factory import (
    build_session,
    validate_responses_model_controls,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent

_WORKFLOW_ENV_OVERRIDE: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "workflow_environment_override",
    default=None,
)


class WorkflowSessionFactory:
    """``WorkflowSessionFactoryPort`` bound to the concrete ``build_session``.

    Each ``build_workflow_session`` call assembles a fresh one-shot ``Agent``
    (carrying the resolved LLM config) and a self-wiring ``Session``. ``tools``
    from the caller become the agent's toolset. ``isolation=True`` is served by
    ``acquire_isolated_env``, which lends the session a worktree of its own from
    the same pool the scheduler uses for teammates.
    """

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        wire_protocol: str = "chat_completions",
        api_key: str | None,
        base_url: str | None,
        workspace: str | None = None,
        tracer: TracePort | None = None,
        event_sink: EventPublisherPort | None = None,
        llm_timeout: float = 600.0,
        max_steps: int = 100,
        system_prompt: str = WORKFLOW_AGENT_PROMPT,
        temperature: float = DEFAULT_TEMPERATURE,
        top_p: float | None = DEFAULT_TOP_P,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        context_window: int | None = None,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        reasoning_effort: str | None = None,
        llm_max_retries: int = 3,
        llm_connect_timeout: float = 30.0,
        llm_first_event_timeout: float = 180.0,
        llm_stream_idle_timeout: float = 180.0,
        provider_error_time_budget: float = 0.0,
        save_dir: str | None = None,
        env: Any | None = None,
        llm_stream_chat: bool = False,
    ) -> None:
        self._model = model
        self._provider = provider
        self._wire_protocol = wire_protocol
        self._api_key = api_key
        self._base_url = base_url
        self._workspace = workspace
        self._tracer = tracer
        self._event_sink = event_sink
        self._llm_timeout = llm_timeout
        self._max_steps = max_steps
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._top_p = top_p
        self._max_output_tokens = max_output_tokens
        self._context_window = context_window
        self._thinking = thinking
        self._thinking_params = resolve_thinking_params(thinking_params)
        self._reasoning_effort = reasoning_effort
        self._llm_max_retries = llm_max_retries
        self._llm_connect_timeout = llm_connect_timeout
        self._llm_first_event_timeout = llm_first_event_timeout
        self._llm_stream_idle_timeout = llm_stream_idle_timeout
        self._llm_stream_chat = llm_stream_chat
        self._provider_error_time_budget = provider_error_time_budget
        self._provider_retry_budget = (
            RetryTimeBudget(provider_error_time_budget)
            if provider_error_time_budget > 0
            else None
        )
        # Run folder where each one-shot session's transcript is autosaved. When
        # set, every ``build_workflow_session`` gets its own ``<seq>_<role>.json``
        # so the AutoSaveSubscriber (wired by ``build_session`` once an
        # ``auto_save_path`` is present) persists it — the same per-role mechanism
        # chat/team sessions use. ``None`` keeps sessions ephemeral (the prior
        # behaviour).
        self._save_dir = save_dir
        self._env = env
        self._session_seq = 0
        # Created on the first ``isolation=True`` build, so a run that isolates
        # nobody never touches git. The pool is the same one the scheduler lends
        # teammate worktrees from, which is the point: an isolated workflow agent
        # and an isolated teammate get the same kind of workspace, so a handoff
        # between two agents means the same thing in either arm.
        self._worktree_pool: WorktreePool | None = None

    def _next_aid(self) -> int:
        """Allocate this session's agent id.

        One integer per session, handed to ``build_session`` as ``aid`` and
        reused as the transcript's ``seq`` prefix so a run folder's filenames
        and its trace records name the same agent. Allocation happens on every
        build, not only when a run folder is configured: without it every
        workflow agent would keep ``build_session``'s ``aid=-1`` default and a
        ``tool_exec`` record could not be traced back to the agent that ran it.

        Incrementing has no ``await`` so it is atomic under the event loop's
        cooperative scheduling even when ``parallel``/``pipeline`` build many
        sessions concurrently.
        """
        aid = self._session_seq
        self._session_seq += 1
        return aid

    def _save_path(self, aid: int, label: str | None) -> str | None:
        """Per-session transcript path: ``<save_dir>/<aid>_<role>.json``.

        Returns ``None`` when no run folder is configured. The caller's
        ``label`` (e.g. ``coder:s1r2``) is slugged into the name so a run folder
        reads as its workflow phases at a glance.
        """
        if self._save_dir is None:
            return None
        return workflow_transcript_path(self._save_dir, aid, label)

    async def acquire_isolated_env(self, *, label: str | None = None) -> Any:
        """Check out a working tree this agent alone edits.

        A linked worktree, so it shares ``.git/objects`` with the workspace it
        came from: a commit made in here is reachable from every other agent's
        tree the moment it exists, which is what lets one agent hand its work to
        another by naming a sha instead of by leaving files where the other one
        will find them.

        ``label`` names the branch, so a leftover tree says which agent made it.
        A run without a workspace has nothing to branch from, and the pool hands
        back a plain local environment instead of failing — the same fallback it
        gives a team told not to use worktrees.
        """
        if self._worktree_pool is None:
            self._worktree_pool = WorktreePool(
                self._workspace or ".",
                use_worktrees=self._workspace is not None,
            )
        return await self._worktree_pool.acquire(label or "workflow-agent")

    async def release_isolated_envs(self) -> None:
        """Tear down every worktree this factory handed out. Safe to call twice."""
        pool = self._worktree_pool
        if pool is None:
            return
        self._worktree_pool = None
        await pool.release()

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
        env: Any | None = None,
    ) -> Any:
        use_thinking = self._thinking if thinking is None else thinking
        use_reasoning_effort = None if thinking is False else self._reasoning_effort
        reasoning_effort_policy = "suppressed" if thinking is False else "configured"
        capabilities = model_capabilities(self._model)
        if self._thinking and not capabilities.honors_workflow_thinking_override:
            use_thinking = True
            use_reasoning_effort = self._reasoning_effort
            reasoning_effort_policy = "configured"
        resolved_tools = list(tools or [])
        if self._wire_protocol == RESPONSES:
            validate_responses_model_controls(
                self._model,
                role_name="workflow_agent",
                tools_present=bool(resolved_tools),
                top_p=self._top_p,
                reasoning_effort=use_reasoning_effort,
            )
        agent = Agent(
            name="workflow_agent",
            system_prompt=self._system_prompt,
            tools=resolved_tools,
            model=self._model,
            provider=self._provider,
            wire_protocol=self._wire_protocol,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            top_p=self._top_p,
            max_tokens_per_step=self._max_output_tokens,
            context_window=self._context_window,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            reasoning_effort=use_reasoning_effort,
            reasoning_effort_policy=reasoning_effort_policy,
            llm_max_retries=self._llm_max_retries,
            llm_connect_timeout=self._llm_connect_timeout,
            llm_first_event_timeout=self._llm_first_event_timeout,
            llm_stream_idle_timeout=self._llm_stream_idle_timeout,
            llm_stream_chat=self._llm_stream_chat,
            provider_error_time_budget=self._provider_error_time_budget,
            tool_choice=tool_choice,
        )
        # An isolated agent brings its own tree; everyone else shares the run's.
        session_env = env if env is not None else self._env
        if session_env is None:
            session_env = (
                LocalEnvironment(self._workspace)
                if self._workspace
                else LocalEnvironment()
            )
        aid = self._next_aid()
        return build_session(
            agent=agent,
            env=session_env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            max_steps=self._max_steps,
            event_sink=self._event_sink,
            llm_timeout=self._llm_timeout,
            provider_retry_budget=self._provider_retry_budget,
            aid=aid,
            auto_save_path=self._save_path(aid, label),
        )


def build_workflow_context(
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    task_concurrency: int | None = None,
    max_steps: int = 100,
    system_prompt: str = WORKFLOW_AGENT_PROMPT,
    save_dir: str | None = None,
    env: Any | None = None,
    source_root: str | None = None,
    deadline_monotonic: float | None = None,
    deadline_margin_seconds: float = 120.0,
) -> WorkflowContext:
    """Build a :class:`WorkflowContext` wired to the concrete session factory.

    ``cfg`` is the resolved config dict (``model`` / ``provider`` / ``api_key`` /
    ``base_url`` / ``budget`` / optional ``llm_timeout`` / ``temperature``)
    produced by the CLI's
    file-first config resolution — so a stale shell ``ANTHROPIC_API_KEY`` cannot
    shadow the configured key. ``budget`` overrides ``cfg['budget']`` when given;
    ``None`` for an unbounded workflow. ``save_dir``, when given, is the run
    folder each session's transcript is autosaved into; ``None`` keeps sessions
    ephemeral.
    """
    environment = env if env is not None else _WORKFLOW_ENV_OVERRIDE.get()
    factory = WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        wire_protocol=cfg.get("wire_protocol", "chat_completions"),
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        workspace=workspace,
        tracer=tracer,
        event_sink=event_sink,
        llm_timeout=float(cfg.get("llm_timeout", 600.0)),
        max_steps=max_steps,
        system_prompt=system_prompt,
        temperature=float(cfg.get("temperature", DEFAULT_TEMPERATURE)),
        top_p=cfg.get("top_p", DEFAULT_TOP_P),
        max_output_tokens=int(cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)),
        context_window=cfg.get("context_window"),
        thinking=bool(cfg.get("thinking", DEFAULT_THINKING)),
        thinking_params=resolve_thinking_params(cfg.get("thinking_params")),
        reasoning_effort=cfg.get("reasoning_effort"),
        llm_max_retries=int(cfg.get("llm_max_retries", 3)),
        llm_connect_timeout=float(cfg.get("llm_connect_timeout", 30.0)),
        llm_first_event_timeout=float(cfg.get("llm_first_event_timeout", 180.0)),
        llm_stream_idle_timeout=float(cfg.get("llm_stream_idle_timeout", 180.0)),
        llm_stream_chat=bool(cfg.get("llm_stream_chat", False)),
        provider_error_time_budget=float(cfg.get("provider_error_time_budget", 0.0)),
        save_dir=save_dir,
        env=environment,
    )
    budget_total = budget if budget is not None else cfg.get("budget")
    # Working-tree probe over the same workspace the sessions edit, so the
    # workflow can verify a real edit landed before declaring success.
    probe_env = environment
    if probe_env is None:
        probe_env = LocalEnvironment(workspace) if workspace else LocalEnvironment()
    return WorkflowContext(
        factory,
        event_sink=event_sink,
        tracer=tracer,
        max_concurrency=max_concurrency,
        task_concurrency=task_concurrency,
        budget_total=budget_total,
        tree_probe=EnvWorkingTreeProbe(probe_env),
        workspace_root=source_root if source_root is not None else workspace,
        deadline_monotonic=deadline_monotonic,
        deadline_margin_seconds=deadline_margin_seconds,
    )


def _resolve_spec_fn(spec_or_fn: Any) -> Any:
    """Return the callable workflow function from a spec or a raw function."""
    if isinstance(spec_or_fn, WorkflowSpec):
        return spec_or_fn.fn
    return spec_or_fn
