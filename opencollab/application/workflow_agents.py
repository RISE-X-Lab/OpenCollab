"""Evidence-preserving agent runners for :class:`WorkflowContext`.

The "force a scout to commit findings, salvage it if it dies" concern peeled out
of ``workflow.py``: a submit_findings capture tool + the runner wind-down brake,
a harvest that never yields a bare "(scout died)", the bounded dead-scout
synthesizer, and the commit-first ``draft_findings`` draft. ``WorkflowAgentsMixin``
is mixed into ``WorkflowContext`` and reaches the engine (``_factory`` /
``_run_session_turn`` / ``_track_session`` / budget+lease+semaphore / ``log``)
via ``self``.

Pure application layer: domain + stdlib imports only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError
from opencollab.application.submit_findings import (
    SUBMIT_TOOL_NAME,
    SubmitFindingsTool,
    build_dead_scout_synthesis_prompt,
    commitment_terminus_payload,
    format_findings_report,
    harvest_findings,
)
from opencollab.application.workflow_structured import _named_tool_choice


class WorkflowAgentsMixin:
    """Enforcement wind-down + commit-first runners mixed into ``WorkflowContext``."""

    async def _run_enforced_agent(
        self,
        prompt: str,
        *,
        label: str | None,
        tools: Sequence[Any] | None,
        isolation: bool,
        tool_choice: str | None,
        thinking: bool | None,
        timeout: float | None,
        budget: int | None,
        enforcement_strength: str,
        commit_reserve: int,
        harvest_fallback: str | None = None,
    ) -> str | None:
        """Run a scout under the enforcement wind-down (STEP 0).

        Mirrors ``_run_agent`` but injects a ``submit_findings`` capture tool, arms
        the runner's structural commit brake, and HARVESTS a usable report: the
        captured payload if present, else the final text, else a "(partial …)"
        salvage from the transcript — so a chopped scout never yields a bare
        "(scout died)". A successful capture sets the cancel event so the loop halts
        at once (commit-first friendly), exactly as the structured path does. Emits
        one ``commitment_terminus`` metric per scout to the orchestration trace.
        """
        deadline = self._timeout_deadline(timeout)
        session_budget = self._capped_session_budget(budget)
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        combined_tools = [*(tools or []), submit_tool]
        try:
            session = self._factory.build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=combined_tools,
                isolation=isolation,
                label=label,
                tool_choice=tool_choice,
                thinking=thinking,
            )
        except Exception as exc:  # noqa: BLE001 — factory failure must not abort the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"agent build failed ({label or 'agent'}): {exc}")
            return None

        self._track_session(session)
        self._configure_session_enforcement(
            session, enforcement_strength, commit_reserve
        )
        failures_before = len(self._agent_failures)
        text: str | None = None
        try:
            text = await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                cancel_event=capture_done,
            )
        except CallerTimeoutError:
            if submit_tool.captured is None:
                self._record_agent_stop(label, "timeout")
            await self.log(f"agent timed out ({label or 'agent'}) after {timeout}s")
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            self._record_agent_failure(label, exc)
            await self.log(f"agent failed ({label or 'agent'}): {exc}")

        terminal_reason = self._session_stop_reason(session)
        if (
            terminal_reason is not None
            and submit_tool.captured is None
            and len(self._agent_failures) == failures_before
        ):
            self._record_agent_stop(label, terminal_reason)

        # Harvest is the backstop even on a timeout/exception: whatever the scout
        # already gathered (captured payload, prose, or the runtime-authored
        # evidence ledger) is salvaged — never a bare "(scout died)".
        ledger = self._scout_ledger(session)
        report = harvest_findings(
            submit_tool.captured, text or "", self._session_messages(session), ledger=ledger,
            draft=harvest_fallback,
        )
        # STEP 2 (rare-case backstop): a DEAD scout — no structured commit
        # (``captured is None``) yet a non-empty ledger of what it gathered —
        # triggers ONE bounded transcript-only synthesizer call (submit_findings
        # only, forced, cite-or-abstain). With STEP 0's wind-down live this fires
        # seldom (scouts are force-committed at ~80%); it salvages the chopped /
        # errored / strayed tail. Gated by construction: this method only runs when
        # enforcement is on.
        if (
            submit_tool.captured is None
            and ledger
            and not self._active_call_has_pending_cleanup()
        ):
            synthesized = await self._synthesize_dead_scout(
                session, label, commit_reserve=commit_reserve
            )
            if synthesized and synthesized.strip():
                report = synthesized
        self._emit_commitment_terminus(session, label, submit_tool, report)
        return report if report else text

    async def _synthesize_dead_scout(
        self, dead_session: Any, label: str | None, *, commit_reserve: int
    ) -> str | None:
        """Salvage a dead/empty scout with ONE bounded transcript-only LLM call.

        Its ONLY input is the scout's runtime-authored evidence ledger + raw tool
        results; its ONLY tool is ``submit_findings`` with a forced (named-function)
        ``tool_choice`` and the cite-or-abstain post-validation — NO exploration
        tools, so the salvage cannot wander or fabricate. Returns the formatted
        findings (or a valid ``insufficient_evidence`` abstention) on a successful
        capture, else ``None`` so the caller keeps the harvested partial. Bounded by
        ``commit_reserve`` (the reserve sized for a single submit turn) and clamped
        to the live global remaining.
        """
        ledger = self._scout_ledger(dead_session)
        messages = self._session_messages(dead_session)
        prompt = build_dead_scout_synthesis_prompt(ledger, messages)
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        synth_label = f"{label}:synth" if label else "synth"
        session_budget = self._capped_session_budget(commit_reserve)
        try:
            session = self._factory.build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=[submit_tool],
                isolation=False,
                label=synth_label,
                tool_choice=_named_tool_choice(SUBMIT_TOOL_NAME),
                thinking=False,
            )
        except Exception as exc:  # noqa: BLE001 — a failed salvage must not abort the fleet
            await self.log(f"dead-scout synth build failed ({synth_label}): {exc}")
            return None

        self._track_session(session)
        try:
            deadline = self._internal_commit_deadline()
            await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                cancel_event=capture_done,
            )
        except Exception as exc:  # noqa: BLE001 — one dead salvage never kills the fleet
            await self.log(f"dead-scout synth failed ({synth_label}): {exc}")
            return None

        captured = submit_tool.captured
        report = format_findings_report(captured) if captured is not None else ""
        self._emit_dead_scout_synthesis(synth_label, ledger, captured, bool(report.strip()))
        return report if report.strip() else None

    async def draft_findings(
        self, prompt: str, *, label: str | None = None, budget: int | None = None
    ) -> dict[str, Any] | None:
        """STEP 5b commit-first: ONE bounded submit-only call that commits a
        structured ``submit_findings`` DRAFT from STATIC context (the pre-recon fact
        sheet) BEFORE any exploration, returning the captured payload (or ``None``).

        Reuses the validated dead-scout-synth wiring exactly — ``tools=[submit_findings]``
        only, a named-function (forced) ``tool_choice``, ``thinking=False`` — so the
        draft cannot wander or fabricate and the call is a single constrained turn.
        It touches NO part of the session FSM: the exploring scout that consumes this
        draft runs the unchanged capture→cancel→harvest path. Cost is one bounded
        call per scout, clamped to ``budget`` (sized to ``commit_reserve``) and to the
        live global remaining. Skips gracefully (``None``) if the shared pool is spent
        or the factory/session errors, so a failed draft never aborts the fleet.
        """
        # Local import breaks the compose cycle: ``workflow.py`` imports this
        # mixin to build ``WorkflowContext``, so this module cannot import from
        # it at load time. ``WorkflowBudgetExceeded`` is raised by the core
        # ``_acquire_budget_lease`` and defined there (SDK-exported from there).
        from opencollab.application.workflow import WorkflowBudgetExceeded

        call_task = asyncio.current_task()
        if call_task is not None:
            self._active_call_tasks.add(call_task)
        slot_acquired = False
        slot_handed_to_cleanup = False
        try:
            await self._semaphore.acquire()
            slot_acquired = True
            try:
                lease = await self._acquire_budget_lease(budget, over_budget_ok=False)
            except WorkflowBudgetExceeded:
                return None
            token = self._active_budget_lease.set(lease)
            try:
                return await self._draft_findings_with_lease(
                    prompt,
                    label=label,
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

    async def _draft_findings_with_lease(
        self, prompt: str, *, label: str | None, budget: int | None
    ) -> dict[str, Any] | None:
        session_budget = self._capped_session_budget(budget)
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        try:
            session = self._factory.build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=[submit_tool],
                isolation=False,
                label=label,
                tool_choice=_named_tool_choice(SUBMIT_TOOL_NAME),
                thinking=False,
            )
        except Exception as exc:  # noqa: BLE001 — a failed draft must not abort the fleet
            await self.log(f"draft build failed ({label or 'draft'}): {exc}")
            return None
        self._track_session(session)
        try:
            deadline = self._internal_commit_deadline()
            await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                cancel_event=capture_done,
            )
        except Exception as exc:  # noqa: BLE001 — one dead draft never kills the fleet
            await self.log(f"draft failed ({label or 'draft'}): {exc}")
            return None
        return submit_tool.captured

    @staticmethod
    def _scout_ledger(session: Any) -> list[dict[str, Any]]:
        """The scout's runtime-authored evidence ledger, or [] when a
        duck-typed session/state does not carry one."""
        state = getattr(session, "state", None)
        turn = getattr(state, "turn", None)
        ledger = getattr(turn, "scout_ledger", None)
        return list(ledger) if ledger else []

    def _emit_dead_scout_synthesis(
        self, label: str | None, ledger: list[dict[str, Any]], captured: dict | None, salvaged: bool
    ) -> None:
        """Trace one ``dead_scout_synthesis`` event (no-op without a tracer) so the
        rare salvage is auditable: how big the ledger was, whether a payload was
        captured, and the anchor count of the salvaged findings."""
        if self._tracer is None:
            return
        findings = (captured or {}).get("findings") or []
        self._tracer.log_step(
            step_type="dead_scout_synthesis",
            payload={
                "role": label,
                "ledger_size": len(ledger),
                "salvaged": salvaged,
                "insufficient_evidence": bool((captured or {}).get("insufficient_evidence")),
                "evidence_anchor_count": sum(
                    1 for f in findings if str(f.get("evidence_anchor") or "").strip()
                ),
            },
        )

    @staticmethod
    def _configure_session_enforcement(
        session: Any,
        enforcement_strength: str,
        commit_reserve: int,
    ) -> None:
        """Arm the session runner's wind-down post-build (the agent already carries
        the submit tool). Defensive: a duck-typed session without a configurable
        runner is left as-is rather than aborting the scout."""
        runner = getattr(session, "runner", None)
        configure = getattr(runner, "configure_enforcement", None)
        if callable(configure):
            configure(
                enforcement_strength=enforcement_strength,
                commit_reserve=commit_reserve,
            )

    @staticmethod
    def _session_messages(session: Any) -> list[dict[str, Any]]:
        state = getattr(session, "state", None)
        messages = getattr(state, "messages", None)
        if messages is None:
            messages = getattr(session, "messages", None)
        return list(messages) if messages else []

    def _emit_commitment_terminus(
        self, session: Any, label: str | None, submit_tool: SubmitFindingsTool, report: str | None
    ) -> None:
        """Emit one ``commitment_terminus`` event per scout to orchestration.jsonl
        (no-op when no tracer is wired)."""
        if self._tracer is None:
            return
        state = getattr(session, "state", None)
        payload = commitment_terminus_payload(
            role=label,
            captured=submit_tool.captured,
            wind_down_done=bool(getattr(state, "wind_down_done", False)),
            used_tokens=int(getattr(state, "used_tokens", 0) or 0),
            max_budget_tokens=int(getattr(session, "max_budget_tokens", 0) or 0),
            wind_down_token_mark=int(getattr(state, "wind_down_token_mark", 0) or 0),
            artifact=report or "",
        )
        self._tracer.log_step(step_type="commitment_terminus", payload=payload)
