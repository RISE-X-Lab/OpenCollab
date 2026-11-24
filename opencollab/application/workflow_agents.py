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
from opencollab.application.session_run import DEFAULT_COMMIT_RESERVE
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
        isolation: bool | str | None,
        tool_choice: Any,
        thinking: bool | None,
        timeout: float | None,
        budget: int | None,
        enforcement_strength: str,
        commit_reserve: int | None,
        harvest_fallback: str | None = None,
    ) -> str | None:
        """Run a scout with submit capture and the enforcement wind-down.

        Successful capture sets the cancellation event. Harvest captured findings,
        final text or transcript evidence, and emit one ``commitment_terminus`` metric.
        """
        deadline = self._timeout_deadline(timeout)
        session_budget = self._capped_session_budget(budget)
        if commit_reserve is None:
            commit_reserve = DEFAULT_COMMIT_RESERVE
            if session_budget is not None and session_budget < commit_reserve:
                commit_reserve = max(1, session_budget // 4)
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        combined_tools = [*(tools or []), submit_tool]
        try:
            session = await self._build_workflow_session(
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
            return harvest_fallback or None

        self._track_session(session)
        self._configure_session_enforcement(
            session, enforcement_strength, commit_reserve
        )
        text: str | None = None
        filtered_failure = False
        try:
            text = await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                label=label,
                cancel_event=capture_done,
            )
        except CallerTimeoutError:
            await self.log(f"agent timed out ({label or 'agent'}) after {timeout}s")
        except Exception as exc:  # noqa: BLE001 — one dead agent never kills the fleet
            filtered_failure = self._agent_failure_filtered(exc)
            self._record_agent_failure(label, exc)
            await self.log(f"agent failed ({label or 'agent'}): {exc}")

        # Salvage gathered evidence even after a timeout or exception.
        ledger = self._scout_ledger(session)
        report = harvest_findings(
            submit_tool.captured, text or "", self._session_messages(session), ledger=ledger,
            draft=harvest_fallback,
        )
        # A scout with evidence but no structured commit gets one bounded salvage
        # call with submit_findings as its only tool. The original deadline still
        # bounds recovery, and every finding must cite existing evidence or abstain.
        if (
            submit_tool.captured is None
            and ledger
            and not filtered_failure
            and not self._agent_output_filtered(session)
            and not self._active_call_has_pending_cleanup()
        ):
            synthesized = await self._synthesize_dead_scout(
                session,
                label,
                commit_reserve=commit_reserve,
                caller_deadline=deadline,
            )
            if synthesized and synthesized.strip():
                report = synthesized
        self._emit_commitment_terminus(session, label, submit_tool, report)
        return report if report else text

    async def _synthesize_dead_scout(
        self,
        dead_session: Any,
        label: str | None,
        *,
        commit_reserve: int,
        caller_deadline: float | None,
    ) -> str | None:
        """Recover an empty scout report with one bounded submission call.

        Seed it with the evidence ledger and raw tool results, offering only
        ``submit_findings`` with a named tool choice and cite-or-abstain validation.
        Return formatted findings or an insufficient-evidence abstention on capture,
        otherwise ``None``. Clamp ``commit_reserve`` to the remaining pool and use
        the earlier of the caller deadline and internal commit deadline.
        """
        ledger = self._scout_ledger(dead_session)
        messages = self._session_messages(dead_session)
        prompt_chars = max(2_000, min(16_000, commit_reserve * 4))
        prompt = build_dead_scout_synthesis_prompt(
            ledger,
            messages,
            max_chars=prompt_chars,
        )
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        synth_label = f"{label}:synth" if label else "synth"
        session_budget = self._capped_session_budget(commit_reserve)
        try:
            session = await self._build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=[submit_tool],
                isolation=False,
                label=synth_label,
                tool_choice=_named_tool_choice(SUBMIT_TOOL_NAME),
                thinking=None,
            )
        except Exception as exc:  # noqa: BLE001 — a failed salvage must not abort the fleet
            self._record_agent_failure(synth_label, exc)
            await self.log(f"dead-scout synth build failed ({synth_label}): {exc}")
            return None

        self._track_session(session)
        try:
            deadline = self._internal_commit_deadline()
            if caller_deadline is not None:
                deadline = min(deadline, caller_deadline)
            await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                label=synth_label,
                cancel_event=capture_done,
            )
        except Exception as exc:  # noqa: BLE001 — one dead salvage never kills the fleet
            self._record_agent_failure(synth_label, exc)
            await self.log(f"dead-scout synth failed ({synth_label}): {exc}")
            return None

        captured = submit_tool.captured
        report = format_findings_report(captured) if captured is not None else ""
        self._emit_dead_scout_synthesis(synth_label, ledger, captured, bool(report.strip()))
        return report if report.strip() else None

    async def draft_findings(
        self, prompt: str, *, label: str | None = None, budget: int | None = None
    ) -> dict[str, Any] | None:
        """Capture a structured draft from static context before scout exploration.

        Use one submit-only call with a named tool choice and the configured
        reasoning policy. Clamp its budget to the shared pool. Return ``None``
        on exhausted budget or factory/session failure so other scouts can proceed.
        """
        # Import locally because workflow composes this mixin and defines the
        # SDK-exported WorkflowBudgetExceeded raised by _acquire_budget_lease.
        from opencollab.application.workflow import WorkflowBudgetExceeded

        call_task = asyncio.current_task()
        already_owned = call_task in self._active_call_tasks
        if call_task is not None:
            self._active_call_tasks.add(call_task)

        async def run_with_lease() -> dict[str, Any] | None:
            try:
                lease = await self._acquire_budget_lease(
                    budget, over_budget_ok=False, label=label
                )
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
                self._release_lease_when_quiescent(
                    lease,
                    release_slot=False,
                )

        try:
            return await self._run_with_concurrency_permit(run_with_lease)
        finally:
            if call_task is not None and not already_owned:
                self._active_call_tasks.discard(call_task)

    async def _draft_findings_with_lease(
        self, prompt: str, *, label: str | None, budget: int | None
    ) -> dict[str, Any] | None:
        session_budget = self._capped_session_budget(budget)
        capture_done = asyncio.Event()
        submit_tool = SubmitFindingsTool(on_capture=capture_done.set)
        draft_label = f"{label}:draft" if label else "draft"
        try:
            session = await self._build_workflow_session(
                prompt=prompt,
                budget=session_budget,
                tools=[submit_tool],
                isolation=False,
                label=label,
                tool_choice=_named_tool_choice(SUBMIT_TOOL_NAME),
                thinking=None,
            )
        except Exception as exc:  # noqa: BLE001 — a failed draft must not abort the fleet
            self._record_agent_failure(draft_label, exc)
            await self.log(f"draft build failed ({label or 'draft'}): {exc}")
            return None
        self._track_session(session)
        try:
            deadline = self._internal_commit_deadline()
            await self._run_session_turn(
                session,
                prompt,
                deadline=deadline,
                label=draft_label,
                cancel_event=capture_done,
            )
        except Exception as exc:  # noqa: BLE001 — one dead draft never kills the fleet
            self._record_agent_failure(draft_label, exc)
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
        findings = (captured or {}).get("findings") or []
        self._trace_step(
            "dead_scout_synthesis",
            {
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
        state = getattr(session, "state", None)
        payload = commitment_terminus_payload(
            role=label,
            captured=submit_tool.captured,
            wind_down_done=bool(getattr(state, "wind_down_done", False)),
            used_tokens=int(getattr(state, "used_tokens", 0) or 0),
            max_budget_tokens=getattr(session, "max_budget_tokens", None),
            wind_down_token_mark=int(getattr(state, "wind_down_token_mark", 0) or 0),
            artifact=report or "",
        )
        self._trace_step("commitment_terminus", payload)
