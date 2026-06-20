"""Bootstrap wiring for the mini workflow engine.

Binds the application-layer :class:`~opencollab.application.workflow.WorkflowContext`
to the concrete ``build_session`` machinery: :class:`WorkflowSessionFactory`
implements ``WorkflowSessionFactoryPort`` by assembling a one-shot ``Agent`` +
``Session`` per ``ctx.agent`` call, with the resolved model / provider / key /
base-url flowing through.

Also owns workflow *discovery* — loading ``@workflow``-decorated functions from a
directory of python files via importlib — and the ``run_workflow`` entry point
that builds a context, runs the workflow function, and returns its result. This
is composition-root code (it knows concrete types), so it lives in bootstrap.
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from collections.abc import Sequence
from typing import Any

from opencollab.adapters.env import LocalEnvironment
from opencollab.adapters.storage import SessionStore
from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
)
from opencollab.application.workflow import WorkflowBudgetExceeded, WorkflowContext
from opencollab.application.workflow_registry import Registry, WorkflowSpec
from opencollab.bootstrap.session_factory import build_session
from opencollab.domain.agent import Agent

# System prompt seeded into every one-shot workflow agent. Deliberately terse:
# the workflow's per-call prompt carries the actual task.
WORKFLOW_AGENT_PROMPT = (
    "You are an autonomous agent invoked as one step of a larger workflow. "
    "Complete the task described in the user message. Use your tools as needed. "
    "Be concise and finish with a clear final answer."
)

# Longest slug kept from an agent label when naming its transcript file.
_MAX_SLUG_LEN = 40


def _slug(label: str | None) -> str:
    """Filename-safe slug from an agent label (``coder:s1r2`` -> ``coder-s1r2``).

    Collapses any run of characters outside ``[A-Za-z0-9._-]`` to a single dash,
    trims separators, and caps length so a label can never produce an unsafe or
    unbounded filename. Empty / falsy labels yield ``""`` (caller omits the
    suffix entirely).
    """
    if not label:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")
    return cleaned[:_MAX_SLUG_LEN]


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
        # Run folder where each one-shot session's transcript is autosaved. When
        # set, every ``build_workflow_session`` gets its own ``session_<n>.json``
        # so the AutoSaveSubscriber (wired by ``build_session`` once an
        # ``auto_save_path`` is present) persists it — the same mechanism chat
        # sessions use. ``None`` keeps sessions ephemeral (the prior behaviour).
        self._save_dir = save_dir
        self._session_seq = 0

    def _next_save_path(self, label: str | None) -> str | None:
        """Per-session transcript path: ``<save_dir>/session_<seq>[_<label>].json``.

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
        slug = _slug(label)
        stem = f"session_{seq:03d}_{slug}" if slug else f"session_{seq:03d}"
        return os.path.join(self._save_dir, f"{stem}.json")

    def build_workflow_session(
        self,
        *,
        prompt: str,
        budget: int,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        label: str | None = None,
    ) -> Any:
        agent = Agent(
            name="workflow_agent",
            system_prompt=WORKFLOW_AGENT_PROMPT,
            tools=list(tools or []),
            model=self._model,
            provider=self._provider,
            api_key=self._api_key,
            base_url=self._base_url,
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
    ``base_url`` / ``budget`` / optional ``llm_timeout``) produced by the CLI's
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
        save_dir=save_dir,
    )
    budget_total = budget if budget is not None else cfg.get("budget")
    return WorkflowContext(
        factory,
        event_sink=event_sink,
        tracer=tracer,
        max_concurrency=max_concurrency,
        budget_total=budget_total,
    )


def _resolve_spec_fn(spec_or_fn: Any) -> Any:
    """Return the callable workflow function from a spec or a raw function."""
    if isinstance(spec_or_fn, WorkflowSpec):
        return spec_or_fn.fn
    return spec_or_fn


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
) -> Any:
    """Build a context, run the workflow function with ``args``, return its result.

    Accepts either a :class:`WorkflowSpec` or a raw ``@workflow``-decorated (or
    plain async) function.

    ``WorkflowBudgetExceeded`` — the sole exception ``WorkflowContext`` lets
    escape — is caught at this run boundary and turned into a structured result
    so the CLI prints a JSON budget report instead of a raw traceback::

        {"status": "budget_exceeded", "error": <str>,
         "tokens_spent": <int>, "budget_total": <int | None>}

    Every other exception still propagates to the caller.

    When ``save_dir`` is given, each session's transcript is autosaved there and
    a ``workflow.json`` manifest (workflow name, args, session count, spend) is
    written on completion — grouping the run's one-shot sessions the way the
    team manifest groups a chat run's agents.
    """
    ctx = build_workflow_context(
        cfg=cfg,
        workspace=workspace,
        tracer=tracer,
        event_sink=event_sink,
        budget=budget,
        max_concurrency=max_concurrency,
        save_dir=save_dir,
    )
    fn = _resolve_spec_fn(spec_or_fn)
    name = spec_or_fn.name if isinstance(spec_or_fn, WorkflowSpec) else getattr(fn, "__name__", "workflow")
    try:
        result = await fn(ctx, args)
    except WorkflowBudgetExceeded as exc:
        result = {
            "status": "budget_exceeded",
            "error": str(exc),
            "tokens_spent": ctx.budget.spent(),
            "budget_total": ctx.budget.total,
        }
    if save_dir is not None:
        _write_workflow_manifest(save_dir, name=name, args=args, ctx=ctx)
    return result


def _write_workflow_manifest(
    save_dir: str,
    *,
    name: str,
    args: dict[str, Any],
    ctx: WorkflowContext,
) -> None:
    """Write ``<save_dir>/workflow.json`` summarising the run.

    Ties the run folder's anonymous ``session_<n>.json`` transcripts to the
    workflow that produced them, mirroring the chat ``team.json`` manifest.
    """
    manifest = {
        "workflow": name,
        "args": args,
        "sessions": len(ctx.sessions),
        "tokens_spent": ctx.budget.spent(),
        "budget_total": ctx.budget.total,
    }
    SessionStore().save_manifest(os.path.join(save_dir, "workflow.json"), manifest)


def discover_workflows(directory: str) -> Registry:
    """Load every ``@workflow``-decorated function under ``directory``.

    Imports each top-level ``*.py`` file (skipping dunder/private names) via
    importlib and registers every function carrying a ``__workflow_spec__``. A
    missing directory yields an empty registry.
    """
    registry = Registry()
    if not os.path.isdir(directory):
        return registry

    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".py") or filename.startswith("_"):
            continue
        path = os.path.join(directory, filename)
        for spec in _load_specs_from_file(path):
            registry.register(spec)
    return registry


def _load_specs_from_file(path: str) -> list[WorkflowSpec]:
    """Import a single python file and collect its workflow specs."""
    module_name = f"_opencollab_workflow_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Dedupe by spec identity: a decorated function bound under more than one
    # module-level name (an alias or a re-export) carries the SAME spec object
    # under each name. Collecting both would register the same name twice and
    # abort discovery of the whole directory, so keep one entry per spec.
    found: list[WorkflowSpec] = []
    seen: set[int] = set()
    for value in vars(module).values():
        wf_spec = getattr(value, "__workflow_spec__", None)
        if isinstance(wf_spec, WorkflowSpec) and id(wf_spec) not in seen:
            seen.add(id(wf_spec))
            found.append(wf_spec)
    return found


__all__ = [
    "WORKFLOW_AGENT_PROMPT",
    "WorkflowSessionFactory",
    "build_session",
    "build_workflow_context",
    "discover_workflows",
    "run_workflow",
]
