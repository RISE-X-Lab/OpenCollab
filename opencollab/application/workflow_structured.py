"""Structured-output forcing for :class:`WorkflowContext`.

The "make the agent commit a schema-valid payload" concern peeled out of
``workflow.py``: a first free-exploration pass, then — only when the capture is
genuinely empty — one corrective pass restricted to the capture tool with a
named-function ``tool_choice``. ``WorkflowStructuredMixin`` is mixed into
``WorkflowContext`` (it reaches the engine — ``_factory`` / ``_run_session_turn``
/ ``_capped_session_budget`` / ``log`` — via ``self``); ``_named_tool_choice``
also serves the evidence-preserving workflow mixin, which imports it from here.

Pure application layer: domain + stdlib imports only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError
from opencollab.application.structured_output import StructuredOutputTool

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

# The corrective session has one tool and no exploration responsibility. Give
# it enough time for one reasoning turn without letting an endpoint that
# degrades forced tool choice to ``auto`` consume the caller's full role budget.
DEFAULT_STRUCTURED_RETRY_TIMEOUT_SECONDS = 60.0


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


def _structured_retry_timeout(remaining: float | None) -> float:
    if remaining is None:
        return DEFAULT_STRUCTURED_RETRY_TIMEOUT_SECONDS
    return min(remaining, DEFAULT_STRUCTURED_RETRY_TIMEOUT_SECONDS)


class WorkflowStructuredMixin:
    """``schema=`` structured-output primitives mixed into ``WorkflowContext``."""

    async def _run_structured_agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool,
        timeout: float | None = None,
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
        deadline = self._timeout_deadline(timeout)
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
            self._record_agent_failure(label, exc)
            await self.log(f"structured agent build failed ({label or 'agent'}): {exc}")
            return None

        # Track immediately so tokens count even if a run_loop raises midway.
        self._track_session(session)
        try:
            await self._run_session_turn(
                session,
                seeded_prompt,
                deadline=deadline,
                cancel_event=capture_done,
            )
            if _schema_satisfied(capture_tool.captured, schema):
                return capture_tool.captured
        except CallerTimeoutError:
            await self.log(f"structured agent timed out ({label or 'agent'}) after {timeout}s")
            return None
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"structured agent failed ({label or 'agent'}): {exc}")
            if _schema_satisfied(capture_tool.captured, schema):
                return capture_tool.captured

        # Corrective pass (only when the capture is genuinely empty above): force
        # the structured commit on a single-tool session pinned to a
        # named-function ``tool_choice`` — graceful, NOT guaranteed (an endpoint
        # may 400-reject it and degrade to ``auto``). Reusing the same
        # capture_tool keeps ``captured`` and the cancel event live across both
        # passes; the first session is handed in so its exploration history is
        # carried into the corrective turn.
        try:
            retry_timeout = _structured_retry_timeout(
                self._remaining_timeout(deadline)
            )
        except CallerTimeoutError:
            await self.log(
                f"structured agent timed out ({label or 'agent'}) after {timeout}s"
            )
            return None
        return await self._forced_structured_commit(
            prompt,
            session,
            capture_tool,
            capture_done,
            schema=schema,
            label=label,
            isolation=isolation,
            timeout=retry_timeout,
            budget=budget,
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
        timeout: float | None,
        budget: int | None,
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
        deadline = self._timeout_deadline(timeout)
        retry_prompt = prompt + "\n\n" + _STRUCTURED_RETRY
        session_budget = self._capped_session_budget(budget)
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
            self._record_agent_failure(label, exc)
            await self.log(f"structured retry build failed ({label or 'agent'}): {exc}")
            return None

        self._track_session(session)
        try:
            if not self._carry_exploration(prior_session, session):
                await self.log(
                    f"structured retry could not carry exploration "
                    f"({label or 'agent'}); continuing from the retry prompt"
                )
            await self._run_session_turn(
                session,
                retry_prompt,
                deadline=deadline,
                cancel_event=capture_done,
            )
        except CallerTimeoutError:
            await self.log(f"structured retry timed out ({label or 'agent'}) after {timeout}s")
            return None
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"structured retry failed ({label or 'agent'}): {exc}")
            return None
        return capture_tool.captured if _schema_satisfied(capture_tool.captured, schema) else None

    @staticmethod
    def _carry_exploration(prior_session: Any, session: Any) -> bool:
        """Copy the first pass's conversation into the corrective session.

        The corrective session is built fresh (seeded only with the system
        prompt), so without this it would have none of the first pass's
        exploration. We copy a *shallow list copy* of the prior messages — the
        new list is independent (so the corrective turn's own appends don't
        mutate the first session's history) while the message dicts are shared,
        which is safe because neither side mutates a message in place.

        The workflow-session port promises ``state.messages``. A top-level
        ``messages`` property remains a compatibility fallback for older custom
        factories. Failure is reported to the caller so it can emit a visible
        recovery-degradation event without aborting the corrective turn.
        """
        prior_state = getattr(prior_session, "state", None)
        prior = getattr(prior_state, "messages", None)
        if prior is None:
            prior = getattr(prior_session, "messages", None)
        if prior is None:
            return False

        session_state = getattr(session, "state", None)
        try:
            if session_state is not None and hasattr(session_state, "messages"):
                session_state.messages = list(prior)
            else:
                session.messages = list(prior)
        except Exception:  # noqa: BLE001 — carry-over is best-effort, never fatal
            return False
        return True
