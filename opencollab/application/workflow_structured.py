"""Explore for a structured payload, then retry a missing capture once.

The corrective session retains prior exploration but exposes only the capture
tool. ``_named_tool_choice`` is also used by findings capture.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from opencollab.application._anthropic_history import _strip_anthropic_thinking
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

def _named_tool_choice(tool_name: str) -> dict[str, Any]:
    """Force one named function rather than any available tool.

    Some endpoints reject forced choice; the runner can fall back once to auto,
    so this is not a guarantee of submission.
    """
    return {"type": "function", "function": {"name": tool_name}}


def _schema_satisfied(captured: Any, schema: dict[str, Any]) -> bool:
    """Accept a validated capture with all required top-level keys.

    Full schema validation belongs to StructuredOutputTool; a missing capture
    or missing required keys triggers correction.
    """
    if not isinstance(captured, dict):
        return False
    required = schema.get("required") if isinstance(schema, dict) else None
    if not required:
        # No required keys: the tool already validated the payload, so any
        # captured dict (even ``{}``) is an accepted commit, not a miss.
        return True
    return all(key in captured for key in required)


def _structured_retry_timeout(remaining: float | None) -> float | None:
    return remaining


class WorkflowStructuredMixin:
    """``schema=`` structured-output primitives mixed into ``WorkflowContext``."""

    async def _run_structured_agent(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool | str | None,
        timeout: float | None = None,
        budget: int | None = None,
    ) -> dict | None:
        """Return a schema-valid payload, or None after unsuccessful correction.

        Explore with the full toolset first. Retry only a missing valid capture,
        not a prose stop if markup recovery already captured a result. Correction
        uses the capture tool alone and carries prior exploration into its session.
        A valid capture cancels further model calls to prevent repeated submissions.
        Forced tool choice may degrade to auto on an endpoint that rejects it.
        """
        deadline = self._timeout_deadline(timeout)
        capture_done = asyncio.Event()
        capture_tool = StructuredOutputTool(schema, on_capture=capture_done.set)
        seeded_prompt = prompt + _STRUCTURED_INSTRUCTION
        combined_tools = [capture_tool, *(tools or [])]
        session_budget = self._capped_session_budget(budget)
        try:
            session = await self._build_workflow_session(
                prompt=seeded_prompt,
                budget=session_budget,
                tools=combined_tools,
                isolation=isolation,
                label=label,
                # Inherit the run-wide reasoning setting rather than forcing it
                # off. Hard-coding it off here exempted every schema-bound agent
                # from a setting the run had declared, which reads back from the
                # trajectory as a per-arm difference in how the model was
                # configured -- on the one axis an experiment turning reasoning
                # on is trying to hold equal.
                thinking=None,
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
                label=label,
                cancel_event=capture_done,
            )
            if self._agent_output_filtered(session):
                return None
            if _schema_satisfied(capture_tool.captured, schema):
                return capture_tool.captured
            terminal_reason = str(getattr(getattr(session, "state", None), "terminal_reason", "") or "")
            if terminal_reason.startswith("context overflow:"):
                await self.log(f"structured agent input exceeded provider limits ({label or 'agent'})")
                return None
        except CallerTimeoutError:
            await self.log(f"structured agent timed out ({label or 'agent'}) after {timeout}s")
            return None
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"structured agent failed ({label or 'agent'}): {exc}")
            if self._agent_failure_filtered(exc):
                return None
            if _schema_satisfied(capture_tool.captured, schema):
                return capture_tool.captured

        if self._active_call_has_pending_cleanup():
            await self.log(f"structured retry skipped while prior execution drains ({label or 'agent'})")
            return None

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
        isolation: bool | str | None,
        timeout: float | None,
        budget: int | None,
    ) -> dict | None:
        """Build a capture-only corrective session carrying the earlier exploration.

        Named tool choice can degrade to auto; a submission is still not guaranteed.
        Without copied exploration the retry would ask for evidence it never received.
        """
        deadline = self._timeout_deadline(timeout)
        retry_prompt = prompt + "\n\n" + _STRUCTURED_RETRY
        session_budget = self._capped_session_budget(budget)
        try:
            session = await self._build_workflow_session(
                prompt=retry_prompt,
                budget=session_budget,
                tools=[capture_tool],
                isolation=isolation,
                label=label,
                tool_choice=_named_tool_choice(capture_tool.name),
                # Inherit here too, for the same reason as the first pass. The
                # original rationale for forcing it off -- that reasoning makes
                # the model answer in free text instead of calling the capture
                # tool -- was measured against the endpoint in use and did not
                # reproduce: at the highest reasoning tier the model called the
                # tool on every attempt. What remains is a latency cost, which
                # the caller's own timeout already bounds.
                thinking=None,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"structured retry build failed ({label or 'agent'}): {exc}")
            return None

        self._track_session(session, continue_budget=True)
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
                label=label,
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
        """Append prior exploration after the corrective session's own system messages.

        Copy the list, sharing message dicts that neither side mutates in place.
        Read ``state.messages`` with a legacy top-level ``messages`` fallback.
        Report copy failure so the caller can trace degradation and still try correction.
        """
        prior_state = getattr(prior_session, "state", None)
        prior = getattr(prior_state, "messages", None)
        if prior is None:
            prior = getattr(prior_session, "messages", None)
        if prior is None:
            return False

        session_state = getattr(session, "state", None)
        try:
            current = getattr(session_state, "messages", None)
            if current is None:
                current = getattr(session, "messages", None)
            system = [message for message in current or [] if message.get("role") == "system"]
            carried = (
                system + [message for message in prior if message.get("role") != "system"]
                if system else list(prior)
            )
            # The corrective session changes system/tools. Its exploration
            # evidence remains useful; prior native thinking binds to the old
            # prefix and must stay out of this new conversation.
            if getattr(prior_state, "anthropic_history", None) is not None:
                carried = _strip_anthropic_thinking(carried)
            if session_state is not None and hasattr(session_state, "messages"):
                session_state.messages = carried
            else:
                session.messages = carried
        except Exception:  # noqa: BLE001 — carry-over is best-effort, never fatal
            return False
        return True
