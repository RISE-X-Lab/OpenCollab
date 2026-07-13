"""Bootstrap wiring and compatibility facade for the workflow engine."""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import contextvars
import copy
import importlib
import inspect
import math
import os
import stat
import sys
import types
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.llm.types import DEFAULT_MAX_OUTPUT_TOKENS
from opencollab.adapters.safe_files import read_regular_text
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.trace import Tracer
from opencollab.adapters.working_tree import EnvWorkingTreeProbe
from opencollab.application.async_timeout import isolate_tasks_from_shutdown
from opencollab.application.autosave import AutoSaveSubscriber
from opencollab.application.ports import EventPublisherPort, TracePort
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap import (
    _workflow_runtime_cleanup,
    _workflow_runtime_discovery,
    _workflow_runtime_execution,
    _workflow_runtime_manifest,
    _workflow_runtime_session,
    _workflow_runtime_state,
)
from opencollab.bootstrap._workflow_runtime_cleanup import (
    _abort_session_environments,
    _add_failure_note,
    _await_cleanup_despite_cancellation,
    _await_manifest_despite_cancellation,
    _close_tracer_after_late_cleanup,
    _close_tracer_capture,
    _consume_task_result,
    _defer_owned_tracer_close,
    _inspect_tracer,
    _late_tracer_owner_done,
    _merge_failure,
    _persist_workflow_manifest_owned,
    _positive_cleanup_timeout,
    _quiesce_and_finalize_workflow_context,
    _quiesce_workflow_context,
    _session_environments,
    _session_persistence_succeeded,
    _sticky_tracer_failure,
    _wait_for_context_cleanup,
    _wait_for_late_quiescence,
    _workflow_manifest_owner_done,
)
from opencollab.bootstrap._workflow_runtime_discovery import (
    _load_specs_from_file,
    discover_workflows,
)
from opencollab.bootstrap._workflow_runtime_execution import (
    run_workflow as _run_workflow_with_integrity,
)
from opencollab.bootstrap._workflow_runtime_manifest import (
    _workflow_manifest_payload,
    _write_workflow_manifest,
)
from opencollab.bootstrap._workflow_runtime_session import _resolve_spec_fn
from opencollab.bootstrap._workflow_runtime_state import (
    _LATE_TRACER_FAILURES,
    _LATE_TRACER_OWNER_TASKS,
    _WORKFLOW_MANIFEST_OWNER_TASKS,
    DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    MAX_WORKFLOW_DIRECTORY_ENTRIES,
    MAX_WORKFLOW_FILES,
    MAX_WORKFLOW_SOURCE_BYTES,
    WORKFLOW_AGENT_PROMPT,
)
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.session_factory import (
    ORCHESTRATION_FILENAME,
    WORKFLOW_MANIFEST_FILENAME,
    build_session,
    slug_label,
    workflow_transcript_path,
)
from opencollab.domain.agent import Agent

_slug = slug_label
_WORKFLOW_ENV_OVERRIDE: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "workflow_environment_override",
    default=None,
)


class WorkflowSessionFactory:
    """Build one-shot workflow sessions with the fully resolved LLM config."""

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
        top_p: float | None = DEFAULT_TOP_P,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        thinking: bool = DEFAULT_THINKING,
        thinking_params: dict | None = None,
        save_dir: str | None = None,
        env: Any | None = None,
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
        self._top_p = top_p
        self._max_output_tokens = max_output_tokens
        self._thinking = thinking
        self._thinking_params = thinking_params if thinking_params is not None else dict(DEFAULT_THINKING_PARAMS)
        self._save_dir = save_dir
        self._session_seq = 0
        self._env = env

    def _next_save_path(self, label: str | None) -> str | None:
        if self._save_dir is None:
            return None
        sequence = self._session_seq
        self._session_seq += 1
        return workflow_transcript_path(self._save_dir, sequence, label)

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
            top_p=self._top_p,
            max_tokens_per_step=self._max_output_tokens,
            thinking=use_thinking,
            thinking_params=self._thinking_params,
            tool_choice=tool_choice,
        )
        environment = self._env or (LocalEnvironment(self._workspace) if self._workspace else LocalEnvironment())
        return build_session(
            agent=agent,
            env=environment,
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
    env: Any | None = None,
    source_root: str | None = None,
    deadline_monotonic: float | None = None,
    deadline_margin_seconds: float = 120.0,
) -> WorkflowContext:
    """Build a workflow context without dropping provider or environment config."""
    environment = env if env is not None else _WORKFLOW_ENV_OVERRIDE.get()
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
        top_p=cfg.get("top_p", DEFAULT_TOP_P),
        max_output_tokens=int(cfg.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)),
        thinking=bool(cfg.get("thinking", DEFAULT_THINKING)),
        thinking_params=cfg.get("thinking_params") or dict(DEFAULT_THINKING_PARAMS),
        save_dir=save_dir,
        env=environment,
    )
    budget_total = budget if budget is not None else cfg.get("budget")
    probe_environment = environment or (LocalEnvironment(workspace) if workspace else LocalEnvironment())
    return WorkflowContext(
        factory,
        event_sink=event_sink,
        tracer=tracer,
        max_concurrency=max_concurrency,
        budget_total=budget_total,
        tree_probe=EnvWorkingTreeProbe(probe_environment),
        workspace_root=source_root if source_root is not None else workspace,
        deadline_monotonic=deadline_monotonic,
        deadline_margin_seconds=deadline_margin_seconds,
    )


async def run_workflow(
    spec_or_fn: Any,
    args: dict[str, Any],
    *,
    cfg: dict[str, Any],
    workspace: str | None = None,
    tracer: TracePort | None = None,
    event_sink: EventPublisherPort | None = None,
    budget: int | None = None,
    max_concurrency: int = 4,
    save_dir: str | None = None,
    trace: bool = True,
    env: Any | None = None,
    cleanup_timeout: float = DEFAULT_WORKFLOW_CLEANUP_TIMEOUT_SECONDS,
    source_root: str | None = None,
    deadline_monotonic: float | None = None,
    deadline_margin_seconds: float = 120.0,
) -> Any:
    """Run through the hardened lifecycle while carrying an injected environment."""
    token = _WORKFLOW_ENV_OVERRIDE.set(env)
    try:
        return await _run_workflow_with_integrity(
            spec_or_fn,
            args,
            cfg=cfg,
            workspace=workspace,
            tracer=tracer,
            event_sink=event_sink,
            budget=budget,
            max_concurrency=max_concurrency,
            save_dir=save_dir,
            trace=trace,
            cleanup_timeout=cleanup_timeout,
            env=env,
            source_root=source_root,
            deadline_monotonic=deadline_monotonic,
            deadline_margin_seconds=deadline_margin_seconds,
        )
    finally:
        _WORKFLOW_ENV_OVERRIDE.reset(token)


# The split execution module resolves this global at call time. Point it at the
# compatibility-aware builder so direct and facade calls share identical config.
_workflow_runtime_session.WorkflowSessionFactory = WorkflowSessionFactory
_workflow_runtime_session.build_workflow_context = build_workflow_context
_workflow_runtime_execution.build_workflow_context = build_workflow_context

_COMPATIBILITY_MODULES = (
    _workflow_runtime_state,
    _workflow_runtime_session,
    _workflow_runtime_manifest,
    _workflow_runtime_cleanup,
    _workflow_runtime_execution,
    _workflow_runtime_discovery,
)


class _WorkflowRuntimeFacade(types.ModuleType):
    """Mirror compatibility patches into the focused runtime modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for module in _COMPATIBILITY_MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _WorkflowRuntimeFacade

__all__ = [
    "WORKFLOW_AGENT_PROMPT",
    "WorkflowSessionFactory",
    "build_session",
    "build_workflow_context",
    "discover_workflows",
    "run_workflow",
]
