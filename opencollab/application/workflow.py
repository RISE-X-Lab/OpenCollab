"""WorkflowContext — a deterministic mini workflow engine.

A workflow is a plain async function ``async def fn(ctx, args) -> Any`` in which
ordinary Python code (not an LLM lead) orchestrates one-shot agent sessions.
It composes reusable session primitives without touching the Scheduler's
pending-row / wake machinery.

The context exposes a handful of primitives:

* ``agent`` — run one one-shot session, return its final assistant text.
* ``parallel`` — fan out thunks through a shared concurrency semaphore.
* ``pipeline`` — flow each item through ordered stages with no inter-stage
  barrier (item A may be in stage 2 while item B is still in stage 1).
* ``phase`` / ``log`` — observability, no-ops when no sink/tracer is wired.
* ``budget`` — live token accounting across every session created so far.

All failure handling is local: a single dead agent, thunk, or pipeline stage
yields ``None`` for that unit of work and never aborts the fleet. The sole
exception that escapes is ``WorkflowBudgetExceeded``, raised by ``agent`` only
when the shared budget is already exhausted *before* a call starts.

Pure application layer: domain + stdlib imports only.
"""

from __future__ import annotations

import asyncio
import builtins
import contextvars
import logging
import math
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from typing import Any

from opencollab.application._session_run_shared import _FILTERED_OUTPUT_REASON, _RequestBudgetRecovery
from opencollab.application.async_timeout import (
    CallerTimeoutError,
)
from opencollab.application.async_timeout import (
    abandon_on_timeout as abandon_on_timeout,
)
from opencollab.application.isolation import isolation_name
from opencollab.application.ports import (
    CandidateWorkspacePort,
    EventPublisherPort,
    ProviderTerminalEventError,
    TracePort,
    WorkflowSessionFactoryPort,
    WorkingTreeProbe,
)
from opencollab.application.provider_failure import provider_http_failure_metadata
from opencollab.application.session_run import DEFAULT_COMMIT_RESERVE as DEFAULT_COMMIT_RESERVE
from opencollab.application.session_run import ENFORCEMENT_OFF
from opencollab.application.structured_output import TOOL_NAME as STRUCTURED_OUTPUT_TOOL_NAME
from opencollab.application.submit_findings import SUBMIT_TOOL_NAME
from opencollab.application.workflow_agent_run import WorkflowAgentRunMixin
from opencollab.application.workflow_agents import WorkflowAgentsMixin
from opencollab.application.workflow_budget import UNBOUNDED_SESSION_BUDGET as UNBOUNDED_SESSION_BUDGET
from opencollab.application.workflow_budget import (
    WorkflowBudget,
    WorkflowBudgetMixin,
    _BudgetEscapeState,
    _BudgetLease,
    _ConcurrencyPermit,
    _positive_concurrency,
)
from opencollab.application.workflow_candidates import WorkflowCandidatesMixin
from opencollab.application.workflow_collections import Stage as Stage
from opencollab.application.workflow_collections import Thunk as Thunk
from opencollab.application.workflow_collections import (
    WorkflowBudgetExceeded as WorkflowBudgetExceeded,
)
from opencollab.application.workflow_collections import (
    WorkflowCollectionsMixin,
    _CollectionBudget,
    _TaskConcurrencyPermit,
)
from opencollab.application.workflow_collections import (
    WorkflowEnvironmentRevoked as WorkflowEnvironmentRevoked,
)
from opencollab.application.workflow_events import WorkflowEvent
from opencollab.application.workflow_runtime import (
    DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS,
    WorkflowRuntimeMixin,
)
from opencollab.application.workflow_structured import WorkflowStructuredMixin
from opencollab.application.workflow_structured import (
    _schema_satisfied as _schema_satisfied,
)
from opencollab.domain.session import SessionPhase
from opencollab.domain.tools import validate_unique_tool_names

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 4

# Leave time before the hard deadline for a workflow's forced final write
# to finish and produce an extractable patch.
DEFAULT_DEADLINE_MARGIN_SECONDS = 120.0

def _failure_exception_chain(error: BaseException) -> list[dict[str, Any]]:
    """Retain bounded error identity and codes without provider messages or paths."""
    pending = [error]
    seen: set[int] = set()
    records: list[dict[str, Any]] = []
    group_type = getattr(builtins, "BaseExceptionGroup", ())
    while pending and len(records) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        record: dict[str, Any] = {
            "type": type(current).__name__[:128],
            "module": type(current).__module__[:256],
        }
        for field in ("errno", "status_code"):
            value = getattr(current, field, None)
            if field == "status_code" and (isinstance(value, bool) or not isinstance(value, int)):
                value = getattr(getattr(current, "response", None), field, None)
            if isinstance(value, int) and not isinstance(value, bool):
                record[field] = value
        records.append(record)
        if isinstance(current, group_type):
            pending.extend(reversed(current.exceptions[:32]))
        if current.__context__ is not None and not current.__suppress_context__:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return records

class WorkflowContext(
    WorkflowAgentRunMixin,
    WorkflowAgentsMixin,
    WorkflowBudgetMixin,
    WorkflowCandidatesMixin,
    WorkflowStructuredMixin,
    WorkflowCollectionsMixin,
    WorkflowRuntimeMixin,
):
    """Primitives a workflow function uses to orchestrate agent sessions.

    One of two Strategies driving ``session.run_loop()``: this deterministic,
    code-driven regime and the event-driven, LLM-supervised ``Scheduler`` are
    interchangeable over the identical Session process primitive.

    ``max_concurrency`` limits active agent sessions only. ``task_concurrency``
    limits active ``parallel`` and ``pipeline`` units across this workflow,
    including candidate child workflows, and inherits ``max_concurrency`` when
    omitted. The limits are independent, so
    mixed work may have up to their sum active at once.
    """

    def __init__(
        self,
        factory: WorkflowSessionFactoryPort,
        *,
        event_sink: EventPublisherPort | None = None,
        tracer: TracePort | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        task_concurrency: int | None = None,
        budget_total: int | None = None,
        tree_probe: WorkingTreeProbe | None = None,
        candidate_workspace: CandidateWorkspacePort | None = None,
        deadline_monotonic: float | None = None,
        deadline_margin_seconds: float = DEFAULT_DEADLINE_MARGIN_SECONDS,
        workspace_root: str | None = None,
        host_workspace: str | None = None,
        limit_mode: str = "environment",
        run_id: str | None = None,
    ) -> None:
        max_concurrency = _positive_concurrency(
            max_concurrency,
            "max_concurrency",
        )
        if task_concurrency is None:
            task_concurrency = max_concurrency
        else:
            task_concurrency = _positive_concurrency(
                task_concurrency,
                "task_concurrency",
            )
        if limit_mode not in {"environment", "explicit"}:
            raise ValueError("limit_mode must be environment or explicit")
        self._limit_mode = limit_mode
        self.run_id = run_id
        self._agent_run_environment_unsafe = False
        self._factory = factory
        self._event_sink = event_sink
        self._tracer = tracer
        self._max_concurrency = max_concurrency
        self._task_concurrency = task_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._task_semaphore = asyncio.Semaphore(task_concurrency)
        self._sessions: list[Any] = []
        # Attribute later agents to the most recently declared workflow phase.
        self._phase_title: str | None = None
        self.budget = WorkflowBudget(budget_total, self._sessions)
        self._budget_lock = asyncio.Lock()
        self._budget_waiters = 0
        # Candidate child contexts share this state so the escape remains one
        # workflow-wide forced write instead of one escape per candidate.
        self._budget_escape_state = _BudgetEscapeState(lock=asyncio.Lock())
        self._active_budget_lease: contextvars.ContextVar[_BudgetLease | None] = (
            contextvars.ContextVar("workflow_budget_lease", default=None)
        )
        self._active_collection_budget: contextvars.ContextVar[_CollectionBudget | None] = (
            contextvars.ContextVar("workflow_collection_budget", default=None)
        )
        self._active_concurrency_permit: contextvars.ContextVar[
            _ConcurrencyPermit | None
        ] = contextvars.ContextVar("workflow_concurrency_permit", default=None)
        self._active_task_concurrency_permit: contextvars.ContextVar[
            _TaskConcurrencyPermit | None
        ] = contextvars.ContextVar("workflow_task_concurrency_permit", default=None)
        self._pending_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._workspace_release_lock = asyncio.Lock()
        self._workflow_imports: ExitStack | None = None
        self._active_call_tasks: set[asyncio.Task[Any]] = set()
        self._active_session_tasks: set[asyncio.Task[Any]] = set()
        self._agent_failures: list[dict[str, Any]] = []
        self._trace_failures: list[dict[str, str]] = []
        self._tree_probe = tree_probe
        self._candidate_workspace = candidate_workspace
        # Expose the workspace path for workflows that inspect repository sources.
        self.workspace_root = workspace_root
        self.host_workspace = host_workspace
        # Absolute time.monotonic deadline, or None for an unbounded run.
        self._deadline_monotonic = deadline_monotonic
        self._deadline_margin_seconds = deadline_margin_seconds

    async def execute_verification(
        self,
        tool: Any,
        params: Mapping[str, object],
    ) -> str:
        """Run one verification tool in this workflow's bound environment."""
        self._raise_if_environment_revoked()
        if not callable(getattr(tool, "execute_with_runtime", None)):
            raise TypeError("tool must satisfy the verification tool contract")
        if not isinstance(params, Mapping):
            raise TypeError("verification params must be a mapping")
        with self._own_workflow_call():
            return await self._factory.execute_verification(tool, dict(params))

    async def tree_changed(self) -> bool | None:
        """Whether the working tree has uncommitted changes.

        Returns ``True``/``False`` when a :class:`WorkingTreeProbe` is wired, or
        ``None`` ("cannot verify") when none is — callers must treat ``None`` as
        unknown and never hard-block on it. A probe error is swallowed to
        ``None`` so a flaky git call never aborts a workflow.
        """
        if self._tree_probe is None:
            return None
        try:
            return await self._tree_probe.changed()
        except Exception as exc:  # noqa: BLE001 — verification must never abort the run
            await self.log(f"tree_changed probe failed: {exc}")
            return None

    async def source_changed(self, exclude_paths: Sequence[str] = ()) -> bool | None:
        """Whether the tree has SOURCE changes — changes outside ``exclude_paths``.

        Same True/False/None ("cannot verify") contract as :meth:`tree_changed`:
        ``None`` when no probe is wired or a probe error is swallowed, so callers
        must never hard-block on it. ``exclude_paths`` lets a workflow ignore
        harness-injected test files (which dirty the tree the whole run but are
        not the agent's edit). Paths are relative to the exposed workspace;
        remaining changes throughout its repository stay visible. Empty
        excludes is byte-for-byte ``tree_changed``.
        """
        if self._tree_probe is None:
            return None
        try:
            return await self._tree_probe.changed_excluding(exclude_paths)
        except Exception as exc:  # noqa: BLE001 — verification must never abort the run
            await self.log(f"source_changed probe failed: {exc}")
            return None

    async def diff(self, exclude_paths: Sequence[str] = ()) -> str | None:
        """Return the repository diff with workspace-relative path exclusions."""
        if self._tree_probe is None:
            return None
        try:
            if exclude_paths:
                if self._candidate_workspace is not None:
                    return await self._candidate_workspace.source_diff(exclude_paths)
                diff_excluding = getattr(self._tree_probe, "diff_excluding", None)
                if callable(diff_excluding):
                    return await diff_excluding(exclude_paths)
                raise RuntimeError("source diff exclusions require a candidate workspace")
            return await self._tree_probe.diff()
        except Exception as exc:  # noqa: BLE001 — inspection must never abort the run
            await self.log(f"diff probe failed: {exc}")
            return None

    def tokens_spent(self) -> int:
        """Return live token usage across workflow sessions."""
        return self.budget.spent()

    def tokens_remaining(self) -> float:
        """Return unspent and unreserved workflow tokens."""
        return self.budget.remaining()

    @property
    def sessions(self) -> Sequence[Any]:
        """Read-only view of every session created so far (newest last).

        Lets an outer-layer caller (e.g. the eval harness) aggregate token /
        step counts across all sessions a workflow produced.
        """
        return tuple(self._sessions)

    @property
    def agent_failures(self) -> tuple[dict[str, Any], ...]:
        """Safe structured summaries for child-agent exceptions."""
        return tuple(deepcopy(failure) for failure in self._agent_failures)

    @property
    def trace_failures(self) -> tuple[dict[str, str], ...]:
        """Sticky, payload-free diagnostics for failed trace writes."""
        return tuple(dict(failure) for failure in self._trace_failures)

    def _trace_step(self, step_type: str, payload: dict[str, Any]) -> None:
        """Best-effort trace emission that never overturns workflow results."""
        if self._tracer is None:
            return
        try:
            self._tracer.log_step(step_type=step_type, payload=payload)
        except Exception as exc:  # noqa: BLE001 — observability is non-authoritative
            self._trace_failures.append(
                {
                    "step_type": str(step_type)[:240],
                    "exception_type": type(exc).__name__[:128],
                }
            )
            logger.error("workflow %s trace failed: %s", step_type, exc)

    async def _build_workflow_session(
        self,
        *,
        label: str | None,
        budget: int | None,
        tools: Sequence[Any] | None = None,
        isolation: bool | str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Build one one-shot session and record that this agent existed.

        Every workflow agent is created here, so this is where a run says who
        ran. It has to be said out loud: a workflow keeps its sessions in a
        plain list, and the only other place a script's own name for an agent
        survives is the transcript filename — which exists only when the run was
        given a folder to save into. An ephemeral run left no way to tell one
        agent from another at all.

        The ``aid`` is the same integer the agent's ``llm_call``, ``tool_exec``
        and ``session_terminal`` records carry, so this record is what joins
        those to a role: it names the agent the script asked for, the phase it
        was asked for in, and what it was given to work with.

        Emitted after the build, because an agent that failed to build has no
        id to attribute anything to; the caller records that failure instead.
        """
        default_isolation = getattr(self._factory, "default_isolation", "shared")
        explicit_strategy = isinstance(isolation, str) or default_isolation != "shared"
        strategy = isolation_name(isolation, default=default_isolation)
        acquire = getattr(self._factory, "acquire_environment", None)
        if callable(acquire):
            env = await acquire(isolation=strategy, label=label)
        elif strategy in {"shared", "worktree"}:
            env = await self._factory.acquire_isolated_env(label=label) if strategy == "worktree" else None
        else:
            raise ValueError(f"session factory does not support isolation strategy {strategy!r}")
        isolation = strategy != "shared"
        session = self._factory.build_workflow_session(
            label=label,
            budget=budget,
            tools=tools,
            isolation=isolation,
            env=env,
            **kwargs,
        )
        state = getattr(session, "state", None)
        self._trace_step(
            "workflow_agent_started",
            {
                "aid": getattr(state, "aid", None),
                "label": label,
                "phase": self._phase_title,
                "tools": [
                    name
                    for tool in tools or ()
                    if isinstance((name := getattr(tool, "name", None)), str)
                ],
                "budget": budget,
                # An isolated writer needs a recorded handoff because siblings cannot see
                # its edits through the shared filesystem.
                "isolated": bool(isolation),
                **({"isolation": strategy} if explicit_strategy else {}),
            },
        )
        return session

    async def release_isolated_workspaces(self) -> None:
        """Give back every workspace the factory lent to an isolated agent.

        Called once the run's sessions are closed, so a tree is never removed
        while an agent still holds it. Duck-typed on purpose: a factory that
        never hands out isolated workspaces needs no such method, and a run that
        asked for no isolation must not start caring that one exists.
        """
        release = getattr(self._factory, "release_isolated_envs", None)
        if callable(release):
            async with self._workspace_release_lock:
                await release()

    def _record_agent_failure(self, label: str | None, exc: Exception) -> None:
        status_code, error_type = provider_http_failure_metadata(exc)
        if self._agent_failure_filtered(exc):
            error_type = "content_filter"
        record = {
            "label": str(label or "agent")[:240],
            "exception_type": type(exc).__name__[:128],
            "status_code": status_code,
            "provider_error_type": error_type,
        }
        chain = _failure_exception_chain(exc)
        if len(chain) > 1 or any("errno" in item for item in chain):
            record["exception_chain"] = chain
        self._agent_failures.append(record)

    @staticmethod
    def _agent_failure_filtered(exc: Exception) -> bool:
        return isinstance(exc, ProviderTerminalEventError) and exc.code == "content_filter"

    @staticmethod
    def _agent_output_filtered(session: Any) -> bool:
        state = getattr(session, "state", None)
        return (
            getattr(state, "phase", None) is SessionPhase.STOPPED
            and getattr(state, "terminal_reason", None) == _FILTERED_OUTPUT_REASON
        )

    async def wait_for_pending_cleanup(self) -> None:
        """Wait until every context-owned call and session task is quiescent.

        This includes active background ``agent`` calls, their current session
        awaitables, and abandoned cancellation cleanup. Boundary owners call it
        before reading artifacts or releasing the shared environment.
        """
        saw_empty = False
        while True:
            pending = set(self.pending_cleanup_tasks)
            if not pending:
                if saw_empty:
                    return
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    @property
    def pending_cleanup_tasks(self) -> tuple[asyncio.Task[Any], ...]:
        """Snapshot of all active or cleaning context-owned execution tasks."""
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        subscriber_tasks: set[asyncio.Task[Any]] = set()
        for session in self._sessions:
            for task in getattr(session, "pending_cleanup_tasks", ()):
                if isinstance(task, asyncio.Task):
                    subscriber_tasks.add(task)
        owned = (
            self._pending_cleanup_tasks
            | self._active_call_tasks
            | self._active_session_tasks
            | subscriber_tasks
        )
        return tuple(
            task
            for task in owned
            if not task.done() and task is not current
        )

    def seconds_left(self) -> float:
        """Seconds remaining until the hard deadline; ``inf`` when unbounded.

        Uses ``time.monotonic()`` (immune to wall-clock jumps). ``inf`` whenever
        no deadline was wired (CLI / tests), so callers that gate on it behave
        exactly as before.
        """
        if self._deadline_monotonic is None:
            return float("inf")
        return self._deadline_monotonic - time.monotonic()

    def time_low(self) -> bool:
        """True once within ``deadline_margin_seconds`` of the hard deadline.

        A wall-clock-aware workflow checks this alongside its token-budget floor
        and, when True, abandons further loops to spend its head-room on one
        forced final write. This preserves time to persist work discovered near
        the deadline. Always false when no deadline is wired.
        """
        return self.seconds_left() <= self._deadline_margin_seconds

    async def agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        tools: Sequence[Any] | None = None,
        isolation: bool | str | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        over_budget_ok: bool = False,
        budget: int | None = None,
        enforcement_strength: str = ENFORCEMENT_OFF,
        commit_reserve: int | None = None,
        harvest_fallback: str | None = None,
    ) -> str | dict | None:
        """Run one session, returning text, a validated payload, or None on session failure.

        ``budget`` requests a per-call allowance from the remaining shared pool.
        None shares the available pool. An exhausted pool raises WorkflowBudgetExceeded
        before session creation unless ``over_budget_ok`` permits the call.

        ``schema`` requires structured_output and returns its validated dictionary.
        An unsatisfied capture gets one corrective retry carrying gathered history.
        ``tool_choice`` applies to free-text sessions. Schema sessions instead explore
        with the endpoint default and then request the capture tool on retry, with
        fallback to auto if the endpoint rejects forced choice.

        ``thinking`` overrides reasoning only on the free-text path, with None
        inheriting the factory default. ``timeout`` bounds the session run loop.
        Passing ctx.seconds_left() allows a deadline-sensitive call to stop within
        the workflow while retaining its on-disk edits.

        ``commit_reserve=None`` uses 25,000 tokens for enforcement. A smaller approved
        budget scales this default to one quarter of the allowance, with a minimum
        of one token. Explicit reserves are validated against the approved budget.
        """
        self._raise_if_environment_revoked()
        supplied_tool_names = [
            name
            for tool in tools or ()
            if isinstance((name := getattr(tool, "name", None)), str)
        ]
        validate_unique_tool_names(
            supplied_tool_names,
            reserved={STRUCTURED_OUTPUT_TOOL_NAME, SUBMIT_TOOL_NAME},
        )
        timeout = self._normalize_timeout(timeout)
        call_task = asyncio.current_task()
        already_owned = call_task in self._active_call_tasks
        if call_task is not None:
            self._active_call_tasks.add(call_task)
        slot_acquired = False
        slot_handed_to_cleanup = False
        lease: _BudgetLease | None = None
        budget_token = None
        permit_token = None
        try:
            permit = self._active_concurrency_permit.get()
            if permit is not None and permit.owner is call_task:
                await asyncio.gather(
                    *(asyncio.shield(task) for task in permit.pending_cleanup_tasks if not task.done()),
                    return_exceptions=True,
                )
            # Reserve before the concurrency gate so every agent declared in a
            # parallel fan-out registers with the shared allocator, even when
            # only one of them may run at a time.
            lease = await self._acquire_budget_lease(
                budget,
                over_budget_ok=over_budget_ok,
                label=label,
            )
            permit = self._active_concurrency_permit.get()
            if permit is None or permit.owner is not call_task:
                await self._semaphore.acquire()
                slot_acquired = True
                permit_token = self._active_concurrency_permit.set(
                    _ConcurrencyPermit(
                        owner=call_task,
                        pending_cleanup_tasks=[],
                    )
                )
            self._raise_if_environment_revoked()
            lease = await self._refresh_budget_lease(
                lease, cap=budget, over_budget_ok=over_budget_ok, label=label,
            )
            budget_token = self._active_budget_lease.set(lease)
            if schema is not None:
                return await self._run_structured_agent(
                    prompt, schema=schema, label=label, tools=tools,
                    isolation=isolation, timeout=timeout, budget=budget,
                )
            # Enforcement supplies submit_findings and the protected submit turn.
            if enforcement_strength != ENFORCEMENT_OFF:
                return await self._run_enforced_agent(
                    prompt,
                    label=label,
                    tools=tools,
                    isolation=isolation,
                    tool_choice=tool_choice,
                    thinking=thinking,
                    timeout=timeout,
                    budget=budget,
                    enforcement_strength=enforcement_strength,
                    commit_reserve=commit_reserve,
                    harvest_fallback=harvest_fallback,
                )
            return await self._run_agent(
                prompt,
                label=label,
                tools=tools,
                isolation=isolation,
                tool_choice=tool_choice,
                thinking=thinking,
                timeout=timeout,
                budget=budget,
            )
        finally:
            if budget_token is not None:
                self._active_budget_lease.reset(budget_token)
            if lease is not None:
                slot_handed_to_cleanup = self._release_lease_when_quiescent(
                    lease,
                    release_slot=slot_acquired,
                )
            if slot_acquired and not slot_handed_to_cleanup:
                self._semaphore.release()
            if permit_token is not None:
                self._active_concurrency_permit.reset(permit_token)
            if call_task is not None and not already_owned:
                self._active_call_tasks.discard(call_task)

    async def _run_agent(
        self,
        prompt: str,
        *,
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool | str | None,
        tool_choice: Any = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        budget: int | None = None,
    ) -> str | None:
        deadline = self._timeout_deadline(timeout)
        session_budget = self._capped_session_budget(budget)
        try:
            session = await self._build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=tools,
                isolation=isolation,
                label=label,
                tool_choice=tool_choice,
                thinking=thinking,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track the session immediately so its tokens count toward the budget
        # even if the run loop raises partway through.
        self._track_session(session)
        try:
            # ``timeout`` (e.g. ``ctx.seconds_left()``) bounds the run loop so a
            # near-deadline forced write is cancelled here, inside the workflow,
            # rather than by the outer ``asyncio.wait_for`` wall. Any tool edits
            # already written to disk before the cancel survive in the env, so the
            # patch is still extractable.
            return await self._run_session_turn(session, prompt, deadline=deadline, label=label)
        except CallerTimeoutError:
            await self.log(f"agent timed out ({label or 'agent'}) after {timeout}s")
            return None
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"agent failed ({label or 'agent'}): {exc}")
            return None

    async def _run_session_turn(
        self,
        session: Any,
        prompt: str,
        *,
        deadline: float | None,
        label: str | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> str:
        """Add one user turn and run it within a shared absolute deadline."""
        add_timeout = self._remaining_timeout(deadline)
        await self._run_with_timeout(
            session.add_user_message(prompt),
            add_timeout,
        )
        run_timeout = self._remaining_timeout(deadline)
        run_loop = session.run_loop() if cancel_event is None else session.run_loop(cancel_event)
        output = await self._run_with_timeout(run_loop, run_timeout)
        if self._agent_output_filtered(session):
            role = str(label or "agent")[:240]
            self._agent_failures.append({
                "label": role,
                "exception_type": "AgentStopped",
                "status_code": None,
                "provider_error_type": "content_filter",
            })
            self._trace_step("workflow_log", {"message": f"agent stopped ({role}): {_FILTERED_OUTPUT_REASON}"})
        return output

    def _internal_commit_deadline(self) -> float:
        remaining = self.seconds_left()
        if remaining <= 0:
            raise CallerTimeoutError
        timeout = DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS
        if math.isfinite(remaining):
            timeout = min(timeout, remaining)
        return time.monotonic() + timeout

    def _track_session(self, session: Any, *, continue_budget: bool = False) -> None:
        self.budget._track_session(session)
        lease = self._active_budget_lease.get()
        if lease is not None:
            previous_spend = lease.spent()
            lease.sessions.append(session)
            runner = getattr(session, "runner", None)
            if lease.reserved and runner is not None:
                # Other leases keep their allocation when a sibling overspends.
                runner.budget_reservations.append(lambda: max(0, -int(self.budget.remaining())))
                if (
                    (lease.recovery_limit is not None or self.budget._parent_allocation is not None)
                    and (len(lease.sessions) == 1 or continue_budget)
                    and lease.total is not None
                    and previous_spend <= lease.total
                    and getattr(runner, "hard_budget_tokens", None) == lease.total - previous_spend
                    and hasattr(runner, "_request_budget_recovery")
                ):
                    runner._request_budget_recovery = _RequestBudgetRecovery(
                        available=lambda: self.budget._recovery_room(lease),
                        claim=lambda minimum, preferred: self.budget._claim_recovered_budget(
                            lease, minimum, preferred, previous_spend=previous_spend,
                        ),
                    )

    def _active_call_has_pending_cleanup(self) -> bool:
        lease = self._active_budget_lease.get()
        if lease is None:
            return False
        pending = set(lease.pending_tasks or ())
        for session in lease.sessions:
            execution = getattr(session, "pending_execution_tasks", None)
            if execution is None:
                execution = getattr(session, "pending_cleanup_tasks", ())
            pending.update(execution)
        return any(not task.done() for task in pending)

    def _raise_if_environment_revoked(self) -> None:
        if self._agent_run_environment_unsafe or bool(getattr(self._factory, "environment_revoked", False)):
            raise WorkflowEnvironmentRevoked(
                "shared workflow execution environment has been revoked"
            )

    async def phase(self, title: str) -> None:
        """Mark a workflow phase, and carry it onto the agents started next."""
        self._phase_title = title
        await self._emit("phase", title)

    async def log(self, message: str) -> None:
        """Emit a log line. No-op when no sink/tracer is wired."""
        await self._emit("log", message)

    async def _emit(self, kind: str, message: str) -> None:
        self._trace_step(f"workflow_{kind}", {"message": message})
        if self._event_sink is not None:
            try:
                await self._event_sink.emit(WorkflowEvent(kind=kind, message=message))
            except Exception as exc:
                logger.error("workflow %s event failed: %s", kind, exc)

__all__ = [
    "WorkflowBudget",
    "WorkflowBudgetExceeded",
    "WorkflowContext",
    "WorkflowEnvironmentRevoked",
    "WorkflowEvent",
]
