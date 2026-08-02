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
from dataclasses import dataclass
from typing import Any

from opencollab.application.async_timeout import (
    CallerTimeoutError,
    consume_task_result,
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
from opencollab.application.workflow_agents import WorkflowAgentsMixin
from opencollab.application.workflow_structured import (
    WorkflowStructuredMixin,
)
from opencollab.application.workflow_structured import (
    _schema_satisfied as _schema_satisfied,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENCY = 4

# Seconds of head-room kept before the run's hard wall (``deadline_monotonic``).
# Once ``time.monotonic()`` is within this margin of the deadline, ``time_low()``
# returns True so a wall-clock-aware workflow bails to a forced final write while
# it can still land a patch — the decisive fix for runs that locate the edit but
# reach the hard wall before the final write completes.
DEFAULT_DEADLINE_MARGIN_SECONDS = 120.0
DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS = 120.0

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


@dataclass(frozen=True)
class WorkflowEvent:
    """Lightweight observability event emitted by ``phase`` / ``log``.

    ``kind`` is ``"phase"`` or ``"log"``; ``message`` is the title/text.
    """

    kind: str
    message: str


class WorkflowBudget:
    """Read-only view over token spend across all sessions created so far.

    ``spent`` sums each tracked session's live ``used_tokens``; ``remaining``
    has infinity semantics when ``total`` is ``None`` (unbounded).
    """

    def __init__(self, total: int | None, sessions: list[Any]) -> None:
        self._total = total
        self._sessions = sessions
        self._leases: list[_BudgetLease] = []

    @property
    def total(self) -> int | None:
        return self._total

    def spent(self) -> int:
        return sum(int(getattr(s, "used_tokens", 0)) for s in self._sessions)

    def remaining(self) -> float:
        if self._total is None:
            return float("inf")
        reserved_unspent = sum(lease.remaining() for lease in self._leases)
        return self._total - self.spent() - reserved_unspent

    def reserve(self, lease: _BudgetLease) -> None:
        self._leases.append(lease)

    def release(self, lease: _BudgetLease) -> None:
        try:
            self._leases.remove(lease)
        except ValueError:
            pass


@dataclass
class _BudgetLease:
    """A per-call token allocation held while one workflow agent is active."""

    total: int
    reserved: int
    sessions: list[Any]
    pending_tasks: list[asyncio.Task[Any]] | None = None

    def remaining(self) -> int:
        spent = sum(max(0, int(getattr(s, "used_tokens", 0))) for s in self.sessions)
        return max(0, self.total - spent)


class WorkflowContext(WorkflowAgentsMixin, WorkflowStructuredMixin):
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
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sessions: list[Any] = []
        self.budget = WorkflowBudget(budget_total, self._sessions)
        self._budget_lock = asyncio.Lock()
        self._budget_waiters = 0
        self._active_budget_lease: contextvars.ContextVar[_BudgetLease | None] = (
            contextvars.ContextVar("workflow_budget_lease", default=None)
        )
        self._pending_cleanup_tasks: set[asyncio.Task[Any]] = set()
        self._active_call_tasks: set[asyncio.Task[Any]] = set()
        self._active_session_tasks: set[asyncio.Task[Any]] = set()
        self._agent_failures: list[dict[str, Any]] = []
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

    def _record_agent_stop(self, label: str | None, reason: str) -> None:
        """Expose a controlled child-session stop as a structured failure."""
        exception_type = (
            "ContextOverflow"
            if reason.startswith("context overflow")
            else "AgentTimeout"
            if reason == "timeout"
            else "SessionStopped"
        )
        self._agent_failures.append(
            {
                "label": str(label or "agent")[:240],
                "exception_type": exception_type,
                "status_code": None,
                "provider_error_type": None,
            }
        )

    @staticmethod
    def _session_stop_reason(session: Any) -> str | None:
        reason = getattr(getattr(session, "state", None), "terminal_reason", None)
        return str(reason) if reason not in (None, "completed") else None

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
        timeout = self._normalize_timeout(timeout)
        call_task = asyncio.current_task()
        if call_task is not None:
            self._active_call_tasks.add(call_task)
        slot_acquired = False
        slot_handed_to_cleanup = False
        try:
            await self._semaphore.acquire()
            slot_acquired = True
            lease = await self._acquire_budget_lease(budget, over_budget_ok=over_budget_ok)
            token = self._active_budget_lease.set(lease)
            try:
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
                self._active_budget_lease.reset(token)
                slot_handed_to_cleanup = self._release_lease_when_quiescent(lease)
        finally:
            if slot_acquired and not slot_handed_to_cleanup:
                self._semaphore.release()
            if call_task is not None:
                self._active_call_tasks.discard(call_task)

    async def _release_call_after_tasks(
        self, lease: _BudgetLease, tasks: list[asyncio.Task[Any]]
    ) -> None:
        """Release one timed-out call's budget and slot after it is quiescent."""
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.budget.release(lease)
            self._semaphore.release()

    def _release_lease_when_quiescent(self, lease: _BudgetLease) -> bool:
        """Release now, or hand the lease and semaphore slot to cleanup."""
        pending = [task for task in (lease.pending_tasks or []) if not task.done()]
        if not pending:
            self.budget.release(lease)
            return False
        cleanup_task = asyncio.create_task(self._release_call_after_tasks(lease, pending))
        self._track_pending_cleanup(cleanup_task)
        return True

    def _track_pending_cleanup(self, task: asyncio.Task[Any]) -> None:
        """Own a background cleanup task and always consume its final result."""
        if task.done():
            consume_task_result(task)
            return
        self._pending_cleanup_tasks.add(task)
        task.add_done_callback(self._pending_cleanup_done)

    def _pending_cleanup_done(self, task: asyncio.Task[Any]) -> None:
        self._pending_cleanup_tasks.discard(task)
        consume_task_result(task)

    def _active_session_done(self, task: asyncio.Task[Any]) -> None:
        self._active_session_tasks.discard(task)
        consume_task_result(task)

    @staticmethod
    def _normalize_timeout(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if isinstance(timeout, bool):
            raise ValueError("workflow timeout must be positive, finite, infinity, or None")
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "workflow timeout must be positive, finite, infinity, or None"
            ) from exc
        if math.isnan(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                "workflow timeout must be positive, finite, infinity, or None"
            )
        if math.isinf(timeout_seconds):
            return None
        return timeout_seconds

    async def _run_with_timeout(self, awaitable: Awaitable[Any], timeout: float | None) -> Any:
        timeout_seconds = self._normalize_timeout(timeout)
        task = asyncio.ensure_future(awaitable)
        self._active_session_tasks.add(task)
        task.add_done_callback(self._active_session_done)
        try:
            if timeout_seconds is None:
                return await asyncio.shield(task)
            done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
            if task in done:
                return task.result()
            task.cancel()
            raise CallerTimeoutError
        except (CallerTimeoutError, asyncio.CancelledError):
            if not task.done():
                task.cancel()
            lease = self._active_budget_lease.get()
            if not task.done():
                self._track_pending_cleanup(task)
            if lease is not None and not task.done():
                if lease.pending_tasks is None:
                    lease.pending_tasks = []
                lease.pending_tasks.append(task)
            raise

    @staticmethod
    def _timeout_deadline(timeout: float | None) -> float | None:
        timeout_seconds = WorkflowContext._normalize_timeout(timeout)
        if timeout_seconds is None:
            return None
        return time.monotonic() + timeout_seconds

    @staticmethod
    def _remaining_timeout(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CallerTimeoutError
        return remaining

    def _internal_commit_deadline(self) -> float:
        remaining = self.seconds_left()
        if remaining <= 0:
            raise CallerTimeoutError
        timeout = DEFAULT_INTERNAL_COMMIT_TIMEOUT_SECONDS
        if math.isfinite(remaining):
            timeout = min(timeout, remaining)
        return time.monotonic() + timeout

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
            result = await self._run_session_turn(session, prompt, deadline=deadline)
            terminal_reason = self._session_stop_reason(session)
            if terminal_reason is not None:
                self._record_agent_stop(label, terminal_reason)
            return result
        except CallerTimeoutError:
            self._record_agent_stop(label, "timeout")
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
                    total = max(1, available // max(1, self._budget_waiters))
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

    async def parallel(self, thunks: Sequence[Thunk]) -> list[Any]:
        """Run thunks concurrently behind the shared semaphore.

        A thunk that raises yields ``None`` in its slot; the gather always
        completes. Result order matches input order.
        """

        async def guard(thunk: Thunk) -> Any:
            try:
                return await thunk()
            except Exception:  # noqa: BLE001 — failures localize to one slot
                return None

        return list(await asyncio.gather(*(guard(t) for t in thunks)))

    # -- pipeline ---------------------------------------------------------- #

    async def pipeline(self, items: Sequence[Any], *stages: Stage) -> list[Any]:
        """Flow each item through ``stages`` independently and concurrently.

        There is NO barrier between stages: item A may be in stage 2 while item
        B is still in stage 1. A stage raising drops that item's result to
        ``None`` and skips its remaining stages; other items are unaffected.
        Result order matches input order.
        """

        async def flow(item: Any, idx: int) -> Any:
            result: Any = item
            for stage in stages:
                try:
                    result = await stage(result, item, idx)
                except Exception:  # noqa: BLE001 — drop this item, skip its rest
                    return None
            return result

        return list(
            await asyncio.gather(*(flow(item, i) for i, item in enumerate(items)))
        )

    # -- observability ----------------------------------------------------- #

    async def phase(self, title: str) -> None:
        """Mark a workflow phase. No-op when no sink/tracer is wired."""
        await self._emit("phase", title)

    async def log(self, message: str) -> None:
        """Emit a log line. No-op when no sink/tracer is wired."""
        await self._emit("log", message)

    async def _emit(self, kind: str, message: str) -> None:
        if self._tracer is not None:
            try:
                self._tracer.log_step(
                    step_type=f"workflow_{kind}", payload={"message": message}
                )
            except Exception as exc:
                logger.error("workflow %s trace failed: %s", kind, exc)
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
