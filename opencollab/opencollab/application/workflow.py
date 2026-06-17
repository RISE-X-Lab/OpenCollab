"""WorkflowContext — a deterministic mini workflow engine.

A workflow is a plain async function ``async def fn(ctx, args) -> Any`` in which
ordinary Python code (not an LLM lead) orchestrates one-shot agent sessions.
This is a structured generalization of ``harness.evaluator.run_eval_task`` — it
does not touch the Scheduler's pending-row / wake machinery.

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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
    WorkflowSessionFactoryPort,
)
from opencollab.application.structured_output import StructuredOutputTool

DEFAULT_MAX_CONCURRENCY = 4

# Appended to a schema= prompt: the agent must finish by emitting structured
# output via the injected tool rather than free-text.
_STRUCTURED_INSTRUCTION = (
    "\n\nYou MUST finish by calling the `structured_output` tool exactly once "
    "with your final result. Do not answer in free text."
)

# Corrective message appended to the same session before the single retry when
# the first run produced no valid structured payload.
_STRUCTURED_RETRY = (
    "You did not produce a valid structured_output result. Call the "
    "`structured_output` tool now with arguments that conform to the schema."
)

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

    @property
    def total(self) -> int | None:
        return self._total

    def spent(self) -> int:
        return sum(int(getattr(s, "used_tokens", 0)) for s in self._sessions)

    def remaining(self) -> float:
        if self._total is None:
            return float("inf")
        return self._total - self.spent()


class WorkflowContext:
    """Primitives a workflow function uses to orchestrate agent sessions."""

    def __init__(
        self,
        factory: WorkflowSessionFactoryPort,
        *,
        event_sink: EventPublisherPort | None = None,
        tracer: TracePort | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        budget_total: int | None = None,
    ) -> None:
        self._factory = factory
        self._event_sink = event_sink
        self._tracer = tracer
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sessions: list[Any] = []
        self.budget = WorkflowBudget(budget_total, self._sessions)

    @property
    def sessions(self) -> Sequence[Any]:
        """Read-only view of every session created so far (newest last).

        Lets an outer-layer caller (e.g. the eval harness) aggregate token /
        step counts across all sessions a workflow produced.
        """
        return tuple(self._sessions)

    # -- agent ------------------------------------------------------------- #

    async def agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        label: str | None = None,
        tools: Sequence[Any] | None = None,
        isolation: bool = False,
    ) -> str | dict | None:
        """Run one one-shot session and return its final assistant text.

        Returns ``None`` if the session errors — one dead agent never kills the
        fleet. Raises ``WorkflowBudgetExceeded`` only when the shared budget is
        already exhausted before the call starts. When ``schema=`` is given the
        agent must finish by calling ``structured_output``; the validated dict
        is returned (with one corrective retry on the same session before
        giving up and returning ``None``).
        """
        if self.budget.remaining() <= 0:
            raise WorkflowBudgetExceeded(
                f"workflow budget exhausted: spent {self.budget.spent()} "
                f"of {self.budget.total}"
            )

        async with self._semaphore:
            if schema is not None:
                return await self._run_structured_agent(
                    prompt, schema=schema, label=label, tools=tools, isolation=isolation
                )
            return await self._run_agent(prompt, label=label, tools=tools, isolation=isolation)

    async def _run_agent(
        self,
        prompt: str,
        *,
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool,
    ) -> str | None:
        session_budget = self._session_budget()
        try:
            session = self._factory.build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=tools,
                isolation=isolation,
                label=label,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            await self.log(f"agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track the session immediately so its tokens count toward the budget
        # even if the run loop raises partway through.
        self._sessions.append(session)
        try:
            await session.add_user_message(prompt)
            return await session.run_loop()
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            await self.log(f"agent failed ({label or 'agent'}): {exc}")
            return None

    async def _run_structured_agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool,
    ) -> dict | None:
        """Run a schema-bound session, returning the validated payload or None.

        Injects a ``StructuredOutputTool`` into the toolset and instructs the
        agent to finish by calling it. After the first ``run_loop`` a missing or
        invalid capture triggers ONE corrective retry on the same session; a
        still-missing capture yields ``None``.

        A successful capture sets a cancel event that the session's precheck
        observes before each LLM call, halting the loop immediately. Without
        it, a model that keeps re-calling structured_output after acceptance
        burns the whole session budget (observed live: one valid capture
        followed by 28 wasted calls until budget death).
        """
        capture_done = asyncio.Event()
        capture_tool = StructuredOutputTool(schema, on_capture=capture_done.set)
        seeded_prompt = prompt + _STRUCTURED_INSTRUCTION
        combined_tools = [capture_tool, *(tools or [])]
        session_budget = self._session_budget()
        try:
            session = self._factory.build_workflow_session(
                prompt=seeded_prompt,
                budget=session_budget,
                tools=combined_tools,
                isolation=isolation,
                label=label,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            await self.log(f"structured agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track immediately so tokens count even if a run_loop raises midway.
        self._sessions.append(session)
        try:
            await session.add_user_message(seeded_prompt)
            await session.run_loop(capture_done)
            if capture_tool.captured is not None:
                return capture_tool.captured
            # Single corrective retry on the same session.
            await session.add_user_message(_STRUCTURED_RETRY)
            await session.run_loop(capture_done)
            return capture_tool.captured
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            await self.log(f"structured agent failed ({label or 'agent'}): {exc}")
            return None

    def _session_budget(self) -> int:
        remaining = self.budget.remaining()
        if remaining == float("inf"):
            return UNBOUNDED_SESSION_BUDGET
        # Clamp to zero: a concurrent agent's spend can land between agent()'s
        # budget gate and this call, driving ``remaining`` negative. A negative
        # per-session budget is nonsensical, so floor it at 0.
        return max(0, int(remaining))

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
            self._tracer.log_step(step_type=f"workflow_{kind}", payload={"message": message})
        if self._event_sink is not None:
            await self._event_sink.emit(WorkflowEvent(kind=kind, message=message))


__all__ = [
    "WorkflowBudget",
    "WorkflowBudgetExceeded",
    "WorkflowContext",
    "WorkflowEvent",
]
