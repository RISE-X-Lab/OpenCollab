"""Workflow session construction and application-context wiring."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.application.workflow import WorkflowContext
from opencollab.application.workflow_registry import WorkflowSpec
from opencollab.bootstrap._workflow_runtime_state import WORKFLOW_AGENT_PROMPT
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
)
from opencollab.bootstrap.session_factory import build_session, workflow_transcript_path
from opencollab.domain.agent import Agent


class WorkflowSessionFactory:
    """``WorkflowSessionFactoryPort`` bound to the concrete ``build_session``.

    Each ``build_workflow_session`` call assembles a fresh one-shot ``Agent``
    (carrying the resolved LLM config) and a self-wiring ``Session``. ``tools``
    from the caller become the agent's toolset; ``isolation`` is accepted for
    forward-compatibility (a future worktree-backed environment) but currently
    runs in a local environment like the headless evaluator.
    """

    def __init__(
        self,
        *,
        model: str,
        provider: str,
        api_key: str | None,
        base_url: str | None,
        workspace: str | None = None,
        tracer: TracePort | None = None,
        event_sink: EventPublisherPort | None = None,
        llm_timeout: float = 600.0,
        temperature: float = DEFAULT_TEMPERATURE,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        save_dir: str | None = None,
    ) -> None:
        self._model = model
        self._provider = provider
        self._api_key = api_key
        self._base_url = base_url
        self._workspace = workspace
        self._tracer = tracer
        self._event_sink = event_sink
        self._llm_timeout = llm_timeout
        self._temperature = temperature
        self._thinking = thinking
        self._thinking_params = thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        # Run folder where each one-shot session's transcript is autosaved. When
        # set, every ``build_workflow_session`` gets its own ``<seq>_<role>.json``
        # so the AutoSaveSubscriber (wired by ``build_session`` once an
        # ``auto_save_path`` is present) persists it — the same per-role mechanism
        # chat/team sessions use. ``None`` keeps sessions ephemeral (the prior
        # behaviour).
        self._save_dir = save_dir
        self._session_seq = 0

    def _next_save_path(self, label: str | None) -> str | None:
        """Per-session transcript path: ``<save_dir>/<seq>_<role>.json``.

        Returns ``None`` when no run folder is configured. The sequence number
        orders sessions by creation and guarantees uniqueness; incrementing it
        has no ``await`` so it is atomic under the event loop's cooperative
        scheduling even when ``parallel``/``pipeline`` build many sessions
        concurrently. The caller's ``label`` (e.g. ``coder:s1r2``) is slugged
        into the name so a run folder reads as its workflow phases at a glance.
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
    ) -> Any:
        use_thinking = self._thinking if thinking is None else thinking
        agent = Agent(
            name="workflow_agent",
            system_prompt=WORKFLOW_AGENT_PROMPT,
            tools=list(tools or []),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=self._temperature,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        env = LocalEnvironment(self._workspace) if self._workspace else LocalEnvironment()
        return build_session(
            agent=agent,
            env=env,
            tracer=self._tracer,
            max_budget_tokens=budget,
            event_sink=self._event_sink,
            llm_timeout=self._llm_timeout,
            auto_save_path=self._next_save_path(label),
        )


def build_workflow_context(
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    save_dir: str | None = None,
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
    factory = WorkflowSessionFactory(
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
        workspace=workspace,
        tracer=tracer,
        event_sink=event_sink,
        llm_timeout=float(cfg.get("llm_timeout", 600.0)),
        temperature=float(cfg.get("temperature", DEFAULT_TEMPERATURE)),
        thinking=bool(cfg.get("thinking", DEFAULT_THINKING)),
        thinking_params=cfg.get("thinking_params") or dict(DEFAULT_THINKING_PARAMS),
        save_dir=save_dir,
    )
    budget_total = budget if budget is not None else cfg.get("budget")
    # Working-tree probe over the same workspace the sessions edit, so the
    # workflow can verify a real edit landed before declaring success.
    probe_env = LocalEnvironment(workspace) if workspace else LocalEnvironment()
    return WorkflowContext(
        factory,
        event_sink=event_sink,
        tracer=tracer,
        max_concurrency=max_concurrency,
        budget_total=budget_total,
        tree_probe=EnvWorkingTreeProbe(probe_env),
        workspace_root=workspace,
    )


def _resolve_spec_fn(spec_or_fn: Any) -> Any:
    """Return the callable workflow function from a spec or a raw function."""
    if isinstance(spec_or_fn, WorkflowSpec):
        return spec_or_fn.fn
    return spec_or_fn
