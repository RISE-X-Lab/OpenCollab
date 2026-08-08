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
import contextvars
import logging
import math
import operator
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from opencollab.application.async_timeout import (
    CallerTimeoutError,
)
from opencollab.application.async_timeout import (
    abandon_on_timeout as abandon_on_timeout,
)
from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
    WorkflowSessionFactoryPort,
    WorkingTreeProbe,
)
from opencollab.application.session_run import DEFAULT_COMMIT_RESERVE, ENFORCEMENT_OFF
from opencollab.application.structured_output import TOOL_NAME as STRUCTURED_OUTPUT_TOOL_NAME
from opencollab.application.submit_findings import SUBMIT_TOOL_NAME
from opencollab.application.workflow_agents import WorkflowAgentsMixin
from opencollab.application.workflow_budget import (
    WorkflowBudget,
    _BudgetLease,
    _ConcurrencyPermit,
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
from opencollab.domain.tools import validate_unique_tool_names

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 4

# Seconds of head-room kept before the run's hard wall (``deadline_monotonic``).
# Once ``time.monotonic()`` is within this margin of the deadline, ``time_low()``
# returns True so a wall-clock-aware workflow bails to a forced final write while
# it can still land a patch — the decisive fix for runs that locate the edit but
# reach the hard wall before the final write completes.
DEFAULT_DEADLINE_MARGIN_SECONDS = 120.0
# Per-agent token budget handed to a session when the workflow budget is
# unbounded (``budget_total is None``). The session still needs a finite cap.
UNBOUNDED_SESSION_BUDGET = 1_000_000

# A thunk is a zero-arg callable returning an awaitable result.
Thunk = Callable[[], Awaitable[Any]]

# A pipeline stage receives (previous result, original item, item index).
Stage = Callable[[Any, Any, int], Awaitable[Any]]


class WorkflowBudgetExceeded(Exception):
    """Raised by ``WorkflowContext.agent`` when the shared budget is exhausted
    before a session is started. The only exception a primitive lets escape.
    """


class WorkflowContext(
    WorkflowAgentsMixin,
    WorkflowStructuredMixin,
    WorkflowRuntimeMixin,
):
    """Primitives a workflow function uses to orchestrate agent sessions.

    One of two Strategies driving ``session.run_loop()``: this deterministic,
    code-driven regime and the event-driven, LLM-supervised ``Scheduler`` are
    interchangeable over the identical Session process primitive.
    """

    def __init__(
        self,
        factory: WorkflowSessionFactoryPort,
        *,
        event_sink: EventPublisherPort | None = None,
        tracer: TracePort | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        budget_total: int | None = None,
        tree_probe: WorkingTreeProbe | None = None,
        deadline_monotonic: float | None = None,
        deadline_margin_seconds: float = DEFAULT_DEADLINE_MARGIN_SECONDS,
        workspace_root: str | None = None,
    ) -> None:
        if isinstance(max_concurrency, bool):
            raise ValueError("max_concurrency must be a positive integer")
        try:
            max_concurrency = operator.index(max_concurrency)
        except TypeError as exc:
            raise ValueError("max_concurrency must be a positive integer") from exc
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self._factory = factory
        self._event_sink = event_sink
        self._tracer = tracer
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sessions: list[Any] = []
        self.budget = WorkflowBudget(budget_total, self._sessions)
        self._budget_lock = asyncio.Lock()
        self._budget_waiters = 0
        self._active_budget_lease: contextvars.ContextVar[_BudgetLease | None] = (
            contextvars.ContextVar("workflow_budget_lease", default=None)
        )
        self._active_collection_budget: contextvars.ContextVar[int | None] = (
            contextvars.ContextVar("workflow_collection_budget", default=None)
        )
        self._active_concurrency_permit: contextvars.ContextVar[
            _ConcurrencyPermit | None
        ] = contextvars.ContextVar("workflow_concurrency_permit", default=None)
        self._pending_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._active_call_tasks: set[asyncio.Task[Any]] = set()
        self._active_session_tasks: set[asyncio.Task[Any]] = set()
        self._agent_failures: list[dict[str, Any]] = []
        self._trace_failures: list[dict[str, str]] = []
        self._tree_probe = tree_probe
        # Absolute path of the repo the sessions edit/read (the workspace passed to
        # ``run_workflow``). Read-only metadata for workflows that need to run a
        # static pass over the source (e.g. the STEP-5a pre-recon fact sheet); it
        # changes no behavior on its own. ``None`` for unbounded CLI / tests.
        self.workspace_root = workspace_root
        # Absolute wall-clock deadline on the ``time.monotonic()`` clock (None =
        # unbounded: no wall, e.g. CLI runs and tests). ``time_low()`` reads it.
        self._deadline_monotonic = deadline_monotonic
        self._deadline_margin_seconds = deadline_margin_seconds

    # -- working-tree verification ---------------------------------------- #

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
        not the agent's edit); empty excludes is byte-for-byte ``tree_changed``.
        """
        if self._tree_probe is None:
            return None
        try:
            return await self._tree_probe.changed_excluding(exclude_paths)
        except Exception as exc:  # noqa: BLE001 — verification must never abort the run
            await self.log(f"source_changed probe failed: {exc}")
            return None

    async def diff(self) -> str | None:
        """Return the current working-tree diff when a probe is available."""
        if self._tree_probe is None:
            return None
        try:
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
        return tuple(dict(failure) for failure in self._agent_failures)

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

    def _record_agent_failure(self, label: str | None, exc: Exception) -> None:
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        if isinstance(status_code, bool) or not isinstance(status_code, int):
            status_code = None
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            error = body if isinstance(body, dict) else {}
        error_type = error.get("type")
        if not isinstance(error_type, str) or not error_type or len(error_type) > 128:
            error_type = None
        elif any(not (char.isalnum() or char in "._-") for char in error_type):
            error_type = None
        self._agent_failures.append(
            {
                "label": str(label or "agent")[:240],
                "exception_type": type(exc).__name__[:128],
                "status_code": status_code,
                "provider_error_type": error_type,
            }
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

    # -- wall clock -------------------------------------------------------- #

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

    # -- agent ------------------------------------------------------------- #

    async def agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
        tool_choice: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        over_budget_ok: bool = False,
        budget: int | None = None,
        enforcement_strength: str = ENFORCEMENT_OFF,
        commit_reserve: int = DEFAULT_COMMIT_RESERVE,
        harvest_fallback: str | None = None,
    ) -> str | dict | None:
        """Run one one-shot session and return its final assistant text.

        ``budget`` caps THIS call's session at a per-call token allocation
        (``max_budget_tokens``), clamped to the live global remaining so it can
        never overshoot the shared pool. ``None`` keeps the prior behaviour
        (the session may use the entire remaining pool). A per-call cap is what
        stops a single runaway session — e.g. a non-converging scout that
        snowballs its context to 700k+ — from starving every later phase.

        Returns ``None`` if the session errors — one dead agent never kills the
        fleet. Raises ``WorkflowBudgetExceeded`` only when the shared budget is
        already exhausted before the call starts, UNLESS ``over_budget_ok=True``
        is passed: the budget floor's single forced final write must be allowed to
        run even at/below zero (it is bounded instead by ``thinking=False`` plus a
        wall-clock ``timeout``), so skipping the pre-call raise is what GUARANTEES
        a patch lands rather than the call self-aborting on an exhausted meter.
        When ``schema=`` is given the
        agent must finish by calling ``structured_output``; the validated dict
        is returned (with one corrective retry on the same session before
        giving up and returning ``None``).

        ``tool_choice`` (e.g. ``"required"``) forces the model to emit a tool
        call on the non-structured path — used by a forced-write step that MUST
        land an edit. On the structured path it is unused: that path runs free
        exploration on the first pass (``tool_choice`` left at the endpoint
        default), then forces a ``structured_output`` commit on the corrective
        turn by restricting the toolset to the capture tool and pinning a
        named-function ``tool_choice`` (best-effort; an endpoint may reject it
        and degrade to ``auto``).

        ``thinking`` (free-text path only) overrides the run-wide reasoning
        default for this one session: ``None`` keeps the factory default, ``False``
        forces reasoning off so a deadline-sensitive step (the forced final write)
        generates fast and cannot blow the deadline margin. ``timeout`` bounds the
        whole session's run loop with ``asyncio.wait_for`` — pass
        ``ctx.seconds_left()`` so a near-deadline forced write is cancelled in a
        controlled way inside the workflow (its on-disk edits survive) rather than
        being truncated by the outer wall.
        """
        if isolation:
            raise ValueError("workflow agent isolation is not available")
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
        if call_task is not None:
            self._active_call_tasks.add(call_task)
        slot_acquired = False
        slot_handed_to_cleanup = False
        lease: _BudgetLease | None = None
        budget_token = None
        permit_token = None
        try:
            # Reserve before the concurrency gate so every agent declared in a
            # parallel fan-out registers with the shared allocator, even when
            # only one of them may run at a time.
            lease = await self._acquire_budget_lease(
                budget,
                over_budget_ok=over_budget_ok,
            )
            budget_token = self._active_budget_lease.set(lease)
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
            if schema is not None:
                return await self._run_structured_agent(
                    prompt, schema=schema, label=label, tools=tools,
                    isolation=isolation, timeout=timeout, budget=budget,
                )
            # Enforcement wind-down (STEP 0): the scout path injects submit_findings
            # and the structural commit brake. OFF (the default) routes to the
            # unchanged ``_run_agent``, so every existing caller is byte-for-byte
            # identical — the new path is reachable only when explicitly requested.
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
            if call_task is not None:
                self._active_call_tasks.discard(call_task)

    async def _run_agent(
        self,
        prompt: str,
        *,
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool,
        tool_choice: str | None = None,
        thinking: bool | None = None,
        timeout: float | None = None,
        budget: int | None = None,
    ) -> str | None:
        deadline = self._timeout_deadline(timeout)
        session_budget = self._capped_session_budget(budget)
        try:
            session = self._factory.build_workflow_session(
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
            return await self._run_session_turn(session, prompt, deadline=deadline)
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
        return await self._run_with_timeout(run_loop, run_timeout)

    def _internal_commit_deadline(self) -> float:
        remaining = self.seconds_left()
        if remaining <= 0:
            raise CallerTimeoutError
        timeout = DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS
        if math.isfinite(remaining):
            timeout = min(timeout, remaining)
        return time.monotonic() + timeout

    def _session_budget(self) -> int:
        lease = self._active_budget_lease.get()
        if lease is not None:
            return lease.remaining()
        remaining = self.budget.remaining()
        if remaining == float("inf"):
            return UNBOUNDED_SESSION_BUDGET
        # Clamp to zero: a concurrent agent's spend can land between agent()'s
        # budget gate and this call, driving ``remaining`` negative. A negative
        # per-session budget is nonsensical, so floor it at 0.
        return max(0, int(remaining))

    def _capped_session_budget(self, cap: int | None) -> int:
        """Session budget = the live global remaining, optionally lowered to a
        caller-supplied per-call ``cap``. ``min`` keeps a per-call allocation
        from overshooting the shared pool while the cap bounds a single runaway
        session; ``None`` reproduces the prior whole-pool behaviour."""
        base = self._session_budget()
        return min(max(0, cap), base) if cap is not None else base

    async def _acquire_budget_lease(
        self,
        cap: int | None,
        *,
        over_budget_ok: bool,
    ) -> _BudgetLease:
        """Atomically reserve one agent call's maximum token allocation."""
        self._budget_waiters += 1
        try:
            # Let sibling tasks launched by one gather register as contenders
            # before the first uncapped caller chooses its share.
            await asyncio.sleep(0)
            async with self._budget_lock:
                remaining = self.budget.remaining()
                if remaining == float("inf"):
                    total = max(0, cap) if cap is not None else UNBOUNDED_SESSION_BUDGET
                    return _BudgetLease(total=total, reserved=0, sessions=[])

                available = max(0, int(remaining))
                if available <= 0 and not over_budget_ok:
                    raise WorkflowBudgetExceeded(
                        f"workflow budget exhausted: spent {self.budget.spent()} "
                        f"of {self.budget.total}"
                    )
                if over_budget_ok and available <= 0:
                    total = max(0, cap) if cap is not None else UNBOUNDED_SESSION_BUDGET
                    return _BudgetLease(total=total, reserved=0, sessions=[])

                if cap is None:
                    collection_share = self._active_collection_budget.get()
                    if collection_share is not None:
                        total = min(collection_share, available)
                    else:
                        total = max(
                            1,
                            available // max(1, self._budget_waiters),
                        )
                else:
                    total = min(max(0, cap), available)
                lease = _BudgetLease(total=total, reserved=total, sessions=[])
                self.budget.reserve(lease)
                return lease
        finally:
            self._budget_waiters -= 1

    def _track_session(self, session: Any) -> None:
        self._sessions.append(session)
        lease = self._active_budget_lease.get()
        if lease is not None:
            lease.sessions.append(session)

    def _active_call_has_pending_cleanup(self) -> bool:
        lease = self._active_budget_lease.get()
        return bool(
            lease is not None
            and lease.pending_tasks
            and any(not task.done() for task in lease.pending_tasks)
        )

    # -- parallel ---------------------------------------------------------- #

    async def _bounded_collection(
        self,
        size: int,
        run_unit: Callable[[int], Awaitable[Any]],
    ) -> list[Any]:
        """Run indexed units with O(max_concurrency) live asyncio tasks."""
        if size == 0:
            return []

        remaining = self.budget.remaining()
        planned_budget = (
            None
            if remaining == float("inf")
            else max(0, int(remaining)) // size
        )
        results: list[Any] = [None] * size
        next_index = iter(range(size))
        terminal: list[BaseException] = []

        async def execute(index: int) -> None:
            budget_token = self._active_collection_budget.set(planned_budget)
            try:
                results[index] = await self._run_with_concurrency_permit(
                    lambda: run_unit(index)
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # preserve terminal workflow signals
                results[index] = exc
                terminal.append(exc)
            finally:
                self._active_collection_budget.reset(budget_token)

        async def worker() -> None:
            while not terminal:
                try:
                    index = next(next_index)
                except StopIteration:
                    return
                await execute(index)

        active = self._active_concurrency_permit.get()
        if active is not None and active.owner is asyncio.current_task():
            # A nested collection already owns the only slot it may need. Run
            # inline to preserve reentrancy even when max_concurrency is one.
            while not terminal:
                try:
                    index = next(next_index)
                except StopIteration:
                    break
                await execute(index)
        else:
            workers = [
                asyncio.create_task(worker())
                for _ in range(min(size, self._max_concurrency))
            ]
            await asyncio.gather(*workers)

        for result in results:
            if isinstance(result, WorkflowBudgetExceeded):
                raise result
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return results

    async def parallel(self, thunks: Sequence[Thunk]) -> list[Any]:
        """Run thunks concurrently behind the shared semaphore.

        A thunk that raises yields ``None`` in its slot, except a budget stop
        which is re-raised after every started slot has settled. Result order
        matches input order.
        """

        async def guard(thunk: Thunk) -> Any:
            try:
                return await thunk()
            except WorkflowBudgetExceeded:
                raise
            except Exception:  # noqa: BLE001 — failures localize to one slot
                return None

        return await self._bounded_collection(
            len(thunks),
            lambda index: guard(thunks[index]),
        )

    # -- pipeline ---------------------------------------------------------- #

    async def pipeline(
        self,
        items: Sequence[Any],
        *stages: Stage,
        stop_on_none: bool = True,
    ) -> list[Any]:
        """Flow each item through ``stages`` independently and concurrently.

        There is NO barrier between stages: item A may be in stage 2 while item
        B is still in stage 1. A stage raising drops that item's result to
        ``None`` and skips its remaining stages; other items are unaffected.
        Result order matches input order. A budget stop is re-raised only after
        every started item has settled.
        """

        async def flow(item: Any, idx: int) -> Any:
            result: Any = item
            for stage in stages:
                try:
                    result = await stage(result, item, idx)
                    if result is None and stop_on_none:
                        return None
                except WorkflowBudgetExceeded:
                    raise
                except Exception:  # noqa: BLE001 — drop this item, skip its rest
                    return None
            return result

        return await self._bounded_collection(
            len(items),
            lambda index: flow(items[index], index),
        )

    # -- observability ----------------------------------------------------- #

    async def phase(self, title: str) -> None:
        """Mark a workflow phase. No-op when no sink/tracer is wired."""
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
    "WorkflowEvent",
]
