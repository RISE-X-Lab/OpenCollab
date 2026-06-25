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
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.application.ports import (
    EventPublisherPort,
    TracePort,
    WorkflowSessionFactoryPort,
    WorkingTreeProbe,
)
from opencollab.application.structured_output import StructuredOutputTool

DEFAULT_MAX_CONCURRENCY = 4

# Seconds of head-room kept before the run's hard wall (``deadline_monotonic``).
# Once ``time.monotonic()`` is within this margin of the deadline, ``time_low()``
# returns True so a wall-clock-aware workflow bails to a forced final write while
# it can still land a patch — the decisive fix for runs that locate the edit but
# die on the 1800s wall before any write (django-11564).
DEFAULT_DEADLINE_MARGIN_SECONDS = 120.0

# Appended to a schema= prompt: the agent must finish by emitting structured
# output via the injected tool rather than free-text.
_STRUCTURED_INSTRUCTION = (
    "\n\nFinish by calling the `structured_output` tool — do not answer in "
    "free text."
)

# Corrective message that seeds the forced-commit retry session when the first
# run produced no valid structured payload. The retry session is restricted to
# the single capture tool with a named-function ``tool_choice`` (force exactly
# ``structured_output``) — graceful, not guaranteed: an endpoint may 400-reject
# the forced choice and degrade to ``auto``, after which the model can still
# answer in prose. This prompt leads with an explicit MUST-call / no-prose
# imperative and tells it to commit its final result NOW from what it already
# gathered rather than answer in free text.
_STRUCTURED_RETRY = (
    "You MUST call the `structured_output` tool now, exactly once, with your "
    "final result based on what you have already gathered, conforming to the "
    "required schema. Do not explore further or answer in prose."
)


def _named_tool_choice(tool_name: str) -> dict[str, Any]:
    """OpenAI-style named-function ``tool_choice`` forcing exactly ``tool_name``.

    More precise than the bare ``"required"`` (force *some* tool) string: it
    names the single tool the corrective turn must call. Stricter
    OpenAI-compatible endpoints (observed: DashScope 400-rejects a bare
    ``"required"`` for several repos and silently degrades to ``auto``) are more
    likely to honour this explicit dict. It rides through the LLM stack
    unchanged (the OpenAI SDK accepts a dict ``tool_choice``); if an endpoint
    still rejects it, ``SessionRunUseCase._complete`` degrades it ONCE to
    ``"auto"`` on a 400 exactly as it does for ``"required"`` today.
    """
    return {"type": "function", "function": {"name": tool_name}}

# Per-agent token budget handed to a session when the workflow budget is
# unbounded (``budget_total is None``). The session still needs a finite cap.
UNBOUNDED_SESSION_BUDGET = 1_000_000

# A thunk is a zero-arg callable returning an awaitable result.
Thunk = Callable[[], Awaitable[Any]]

# A pipeline stage receives (previous result, original item, item index).
Stage = Callable[[Any, Any, int], Awaitable[Any]]


def _schema_satisfied(captured: Any, schema: dict[str, Any]) -> bool:
    """Minimal acceptance check for a captured structured payload.

    The ``StructuredOutputTool`` already validated the payload against the full
    schema before storing it, so this is light hardening, not a re-validation:
    it rejects a missing capture and a dict that omits any of the schema's
    required top-level keys (e.g. an empty ``{}`` that slipped through), which
    are treated like a miss so the forced corrective turn runs.
    """
    if not isinstance(captured, dict):
        return False
    required = schema.get("required") if isinstance(schema, dict) else None
    if not required:
        # No required keys: the tool already validated the payload, so any
        # captured dict (even ``{}``) is an accepted commit, not a miss.
        return True
    return all(key in captured for key in required)


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
        tree_probe: WorkingTreeProbe | None = None,
        deadline_monotonic: float | None = None,
        deadline_margin_seconds: float = DEFAULT_DEADLINE_MARGIN_SECONDS,
    ) -> None:
        self._factory = factory
        self._event_sink = event_sink
        self._tracer = tracer
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sessions: list[Any] = []
        self.budget = WorkflowBudget(budget_total, self._sessions)
        self._tree_probe = tree_probe
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

    @property
    def sessions(self) -> Sequence[Any]:
        """Read-only view of every session created so far (newest last).

        Lets an outer-layer caller (e.g. the eval harness) aggregate token /
        step counts across all sessions a workflow produced.
        """
        return tuple(self._sessions)

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
        forced final write — the fix for runs that die on the wall after locating
        the edit but before writing it (P7 / django-11564). Always False when no
        deadline is wired, preserving today's behavior.
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
        if not over_budget_ok and self.budget.remaining() <= 0:
            raise WorkflowBudgetExceeded(
                f"workflow budget exhausted: spent {self.budget.spent()} "
                f"of {self.budget.total}"
            )

        async with self._semaphore:
            if schema is not None:
                return await self._run_structured_agent(
                    prompt, schema=schema, label=label, tools=tools,
                    isolation=isolation, budget=budget,
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
            await self.log(f"agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track the session immediately so its tokens count toward the budget
        # even if the run loop raises partway through.
        self._sessions.append(session)
        try:
            await session.add_user_message(prompt)
            # ``timeout`` (e.g. ``ctx.seconds_left()``) bounds the run loop so a
            # near-deadline forced write is cancelled here, inside the workflow,
            # rather than by the outer ``asyncio.wait_for`` wall. Any tool edits
            # already written to disk before the cancel survive in the env, so the
            # patch is still extractable. A non-positive timeout is treated as "no
            # bound" — the caller is already past the deadline; let the call run.
            if timeout is not None and timeout != float("inf") and timeout > 0:
                return await asyncio.wait_for(session.run_loop(), timeout=timeout)
            return await session.run_loop()
        except asyncio.TimeoutError:
            await self.log(f"agent timed out ({label or 'agent'}) after {timeout}s")
            return None
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
        budget: int | None = None,
    ) -> dict | None:
        """Run a schema-bound session, returning the validated payload or None.

        First pass — free exploration: the full toolset
        ``[capture_tool, *tools]`` is offered with ``tool_choice`` left at the
        endpoint default so the agent can grep/read before committing, and it is
        instructed to finish by calling ``structured_output``.

        Corrective pass — forced commit: the trigger is a genuinely-empty
        capture (``_schema_satisfied(captured)`` is False after the first pass),
        NOT a free-text stop reason — so a markup-leaked tool call that the
        parser already resolved into ``captured`` does NOT spuriously fire it.
        When it fires, a second session restricted to ONLY the capture tool with
        a named-function ``tool_choice`` (``_named_tool_choice``) is built,
        seeded with the first pass's conversation (its exploration is copied
        over) and an explicit 'you MUST call structured_output, do not answer in
        prose' instruction, so it commits from what was actually gathered rather
        than the bare prompt. This raises the odds of a commit but does NOT
        guarantee one: some OpenAI-compatible endpoints (observed: DashScope)
        400-reject a forced ``tool_choice`` and ``session_run`` degrades it once
        to ``auto``, after which the model may still answer in prose — a
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
        session_budget = self._capped_session_budget(budget)
        try:
            session = self._factory.build_workflow_session(
                prompt=seeded_prompt,
                budget=session_budget,
                tools=combined_tools,
                isolation=isolation,
                label=label,
                thinking=False,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            await self.log(f"structured agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track immediately so tokens count even if a run_loop raises midway.
        self._sessions.append(session)
        try:
            await session.add_user_message(seeded_prompt)
            await session.run_loop(capture_done)
            if _schema_satisfied(capture_tool.captured, schema):
                return capture_tool.captured
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            await self.log(f"structured agent failed ({label or 'agent'}): {exc}")
            return None

        # Corrective pass (only when the capture is genuinely empty above): force
        # the structured commit on a single-tool session pinned to a
        # named-function ``tool_choice`` — graceful, NOT guaranteed (an endpoint
        # may 400-reject it and degrade to ``auto``). Reusing the same
        # capture_tool keeps ``captured`` and the cancel event live across both
        # passes; the first session is handed in so its exploration history is
        # carried into the corrective turn.
        return await self._forced_structured_commit(
            prompt,
            session,
            capture_tool,
            capture_done,
            schema=schema,
            label=label,
            isolation=isolation,
        )

    async def _forced_structured_commit(
        self,
        prompt: str,
        prior_session: Any,
        capture_tool: StructuredOutputTool,
        capture_done: asyncio.Event,
        *,
        schema: dict[str, Any],
        label: str | None,
        isolation: bool,
    ) -> dict | None:
        """Build a single-tool, forced-``tool_choice`` corrective session.

        The session is pinned to a named-function ``tool_choice`` (force exactly
        ``structured_output``) and seeded with an explicit 'you MUST call the
        tool, do not answer in prose' instruction. This strongly pushes — but,
        on endpoints that 400-reject a forced choice and degrade to ``auto``,
        does not guarantee — a structured commit.

        The first pass's conversation (its grep/file_read tool results and the
        understanding the model built) is copied from ``prior_session`` into the
        corrective session before the retry message is added, so the forced
        commit fills the schema from real exploration rather than from the bare
        prompt — without this carry-over the ``_STRUCTURED_RETRY`` instruction to
        commit "based on what you have already gathered" would address a blank
        session that gathered nothing.
        """
        retry_prompt = prompt + "\n\n" + _STRUCTURED_RETRY
        session_budget = self._session_budget()
        try:
            session = self._factory.build_workflow_session(
                prompt=retry_prompt,
                budget=session_budget,
                tools=[capture_tool],
                isolation=isolation,
                label=label,
                tool_choice=_named_tool_choice(capture_tool.name),
                thinking=False,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            await self.log(f"structured retry build failed ({label or 'agent'}): {exc}")
            return None

        self._sessions.append(session)
        try:
            self._carry_exploration(prior_session, session)
            await session.add_user_message(retry_prompt)
            await session.run_loop(capture_done)
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            await self.log(f"structured retry failed ({label or 'agent'}): {exc}")
            return None
        return capture_tool.captured if _schema_satisfied(capture_tool.captured, schema) else None

    @staticmethod
    def _carry_exploration(prior_session: Any, session: Any) -> None:
        """Copy the first pass's conversation into the corrective session.

        The corrective session is built fresh (seeded only with the system
        prompt), so without this it would have none of the first pass's
        exploration. We copy a *shallow list copy* of the prior messages — the
        new list is independent (so the corrective turn's own appends don't
        mutate the first session's history) while the message dicts are shared,
        which is safe because neither side mutates a message in place.

        Defensive: a session shape that lacks a settable ``messages`` (e.g. the
        very first pass having failed before any message landed) must not abort
        the corrective turn — the worst case is the pre-fix bare-prompt commit.
        """
        prior = getattr(prior_session, "messages", None)
        if prior is None:
            return
        try:
            session.messages = list(prior)
        except Exception:  # noqa: BLE001 — carry-over is best-effort, never fatal
            pass

    def _session_budget(self) -> int:
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
        return min(cap, base) if cap is not None else base

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
