"""Managed workflow calls with complete per-session execution results."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from opencollab.application.async_timeout import CallerTimeoutError
from opencollab.application.workflow_budget import _positive_budget
from opencollab.domain.tools import validate_unique_tool_names


@dataclass(frozen=True, slots=True)
class WorkflowAgentResult:
    """A call snapshot; final usage requires ``cleanup_complete`` to be true."""

    output: str | None
    status: str
    reason: str | None
    tokens: int
    steps: int
    session_id: str | None
    hard_budget_tokens: int | None
    soft_budget_tokens: int | None
    cleanup_complete: bool
    workspace_ready: bool
    observation_errors: tuple[str, ...] = ()


class WorkflowAgentRunMixin:
    """Run a normal profile session through the context's owned leases."""

    async def _settle_agent_run(self, lease: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        saw_empty = False
        while True:
            pending = self._lease_cleanup_tasks(lease)
            if not pending:
                if saw_empty:
                    return True
                saw_empty = True
                await asyncio.sleep(0)
                continue
            saw_empty = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _done, active = await asyncio.wait(pending, timeout=remaining)
            if active:
                return False

    async def agent_run(
        self,
        prompt: str,
        *,
        label: str | None = None,
        tools: str | Sequence[Any] | None = None,
        isolation: bool = False,
        budget: int | None = None,
        timeout: float | None = None,
        max_steps: int | None = None,
        system_prompt: str | None = None,
        run_control: Any | None = None,
        cleanup_timeout: float = 2.0,
    ) -> WorkflowAgentResult:
        """Create a fresh session, returning after bounded call cleanup.

        ``budget`` requests a hard lease. A run control's soft allowance is
        bounded by the actual grant. Explicit system prompts replace the
        profile prompt for this call; profile tools and safety still apply.
        External cancellation propagates and retains cleanup ownership.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if system_prompt is not None and (
            not isinstance(system_prompt, str) or not system_prompt.strip()
        ):
            raise ValueError("system_prompt must be a non-empty string")
        max_steps = _positive_budget(max_steps, "max_steps")
        if isinstance(cleanup_timeout, bool):
            raise ValueError("cleanup_timeout must be positive and finite")
        cleanup_timeout = float(cleanup_timeout)
        if not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0:
            raise ValueError("cleanup_timeout must be positive and finite")
        timeout = self._normalize_timeout(timeout)
        if not isinstance(tools, str):
            validate_unique_tool_names(
                [tool.name for tool in tools or ()],
                reserved={"structured_output", "submit_findings"},
            )
        self._raise_if_environment_revoked()
        call = asyncio.current_task()
        already_owned = call in self._active_call_tasks
        if call is not None:
            self._active_call_tasks.add(call)
        lease = None
        budget_token = None
        released = False
        try:
            lease = await self._acquire_budget_lease(budget, over_budget_ok=False, label=label)
            budget_token = self._active_budget_lease.set(lease)

            async def execute() -> WorkflowAgentResult:
                nonlocal released
                session = None
                output = None
                quiet = False
                status, reason = "completed", None
                try:
                    self._raise_if_environment_revoked()
                    try:
                        options: dict[str, Any] = {}
                        if max_steps is not None:
                            options["max_steps"] = max_steps
                        if system_prompt is not None:
                            options["system_prompt"] = system_prompt
                        if run_control is not None:
                            options["run_control"] = run_control
                        session = await self._build_workflow_session(
                            prompt=prompt, budget=lease.total, tools=tools,
                            isolation=isolation, label=label, **options,
                        )
                    except Exception as exc:
                        self._record_agent_failure(label, exc)
                        return WorkflowAgentResult(
                            None, "failed", f"build failed: {type(exc).__name__}",
                            0, 0, None, lease.total, None, True,
                            not bool(getattr(self._factory, "environment_revoked", False)),
                        )
                    self._track_session(session)
                    try:
                        output = await self._run_session_turn(
                            session, prompt, deadline=self._timeout_deadline(timeout),
                        )
                    except CallerTimeoutError:
                        status, reason = "stopped", "timeout"
                    except Exception as exc:
                        self._record_agent_failure(label, exc)
                        status, reason = "failed", f"run failed: {type(exc).__name__}"
                    quiet = await self._settle_agent_run(lease, cleanup_timeout)
                    close = getattr(session, "aclose", None)
                    if quiet and callable(close):
                        try:
                            await self._run_with_timeout(close(), cleanup_timeout)
                            quiet = await self._settle_agent_run(lease, cleanup_timeout)
                        except (CallerTimeoutError, Exception) as exc:
                            self._record_agent_failure(label, exc)
                            quiet = False
                    if not quiet:
                        self._agent_run_environment_unsafe = True
                        status, reason = "failed", "cleanup incomplete"
                    if getattr(session, "persistence_errors", ()):
                        status, reason = "failed", "session persistence failed"
                    state = getattr(session, "state", None)
                    phase = getattr(getattr(state, "phase", None), "value", None)
                    terminal_reason = getattr(state, "terminal_reason", None)
                    if status == "completed" and phase in {"stopped", "error"}:
                        status = "stopped" if phase == "stopped" else "failed"
                        reason = terminal_reason
                    elif status == "completed" and phase not in {None, "done"}:
                        status, reason = "stopped", terminal_reason or f"session phase: {phase}"
                    runner = getattr(session, "runner", None)
                    return WorkflowAgentResult(
                        output, status, reason, int(getattr(session, "used_tokens", 0)),
                        int(getattr(session, "step_count", 0)),
                        str(getattr(runner, "_response_session_id", getattr(state, "aid", ""))),
                        lease.total, getattr(session, "max_budget_tokens", lease.total),
                        quiet,
                        quiet and not bool(getattr(self._factory, "environment_revoked", False)),
                        tuple(getattr(runner, "observation_errors", ())),
                    )
                except asyncio.CancelledError:
                    try:
                        quiet = await self._settle_agent_run(lease, cleanup_timeout)
                        close = getattr(session, "aclose", None)
                        if quiet and callable(close):
                            await self._run_with_timeout(close(), cleanup_timeout)
                            quiet = await self._settle_agent_run(lease, cleanup_timeout)
                    except BaseException:
                        quiet = False
                    if not quiet:
                        self._agent_run_environment_unsafe = True
                    raise
                finally:
                    released = True
                    if quiet:
                        self.budget.release(lease)
                    else:
                        self._release_lease_when_quiescent(lease, release_slot=False)

            return await self._run_with_concurrency_permit(execute)
        finally:
            if budget_token is not None:
                self._active_budget_lease.reset(budget_token)
            if lease is not None and not released:
                self._release_lease_when_quiescent(lease, release_slot=False)
            if call is not None and not already_owned:
                self._active_call_tasks.discard(call)
