"""Scheduler — passive process tracker for multi-agent execution.

Tracks every Session as a SessionControlBlock in a SessionTable:
- spawn() is non-blocking (returns aid immediately)
- Agents run in parallel via asyncio.create_task
- Results are delivered to parents via message injection into parent state
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from opencollab.application.ports import (
    EnvironmentPort,
    EventPublisherPort,
    PermissionPort,
    SessionFactoryPort,
    TracePort,
    WorktreePoolPort,
)
from opencollab.domain.events import SchedulerEvent
from opencollab.domain.scheduler import DelegationTask, ReviewVerdict, SessionControlBlock, SessionTable, split_budget
from opencollab.domain.session import SessionPhase


@dataclass(frozen=True)
class LaunchSpec:
    """Launch-time persistence spec for a session process.

    Carries *where* to restore from and *where* to checkpoint. The scheduler
    sequences resume/seed as a launch lifecycle step (``create_init_process``
    -> ``Session.apply_launch``); the Session/Store own *how*. Pure data — the
    scheduler forwards it without interpreting the contents.
    """

    session_file: str | None = None
    auto_save_path: str | None = None


class Scheduler:
    """Passive scheduler that tracks SCBs and runs agents in parallel.

    Design:
    - SessionTable holds all SCBs (pure data, no I/O)
    - spawn() creates a new SCB and schedules it for execution
    - run() drives the main loop, waiting for all agents to complete
    - Results are injected into parent sessions as system messages
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactoryPort,
        worktree_pool: WorktreePoolPort,
        event_sink: EventPublisherPort | None = None,
        tracer: TracePort | None = None,
        max_budget_tokens: int = 500_000,
        permission_policy: PermissionPort | None = None,
    ):
        self._session_factory = session_factory
        self._worktree_pool = worktree_pool
        self._event_sink = event_sink
        self._tracer = tracer
        self._max_budget_tokens = max_budget_tokens
        self._permission_policy = permission_policy

        self.table = SessionTable()
        self._tasks: dict[int, asyncio.Task] = {}
        self._lead_session: Any | None = None

    @property
    def used_tokens(self) -> int:
        """Total tokens across all agents."""
        return self.table.total_used_tokens

    @property
    def lead_session(self) -> Any:
        """Agent 0's session (the interactive entry)."""
        return self._lead_session

    def register_lead(self, session: Any) -> int:
        """Register an already-built session as agent 0 (aid=0).

        Low-level primitive: assigns aid, marks SCHEDULED, adds the SCB, and
        stores the lead handle. ``create_init_process`` builds the session and
        delegates here; tests can register a pre-built (or fake) lead directly.
        """
        aid = self.table.allocate_aid()  # = 0
        session.state.aid = aid
        session.state.set_phase(SessionPhase.SCHEDULED)
        scb = SessionControlBlock(
            aid=aid,
            parent_aid=None,
            agent=session.agent,
            state=session.state,
        )
        self.table.add(scb)
        self._lead_session = session
        return aid

    def create_init_process(self, launch: LaunchSpec) -> int:
        """Create and register agent 0 — the init process (aid=0).

        The factory owns construction (env, tools, prompt, store); the
        scheduler owns the launch lifecycle: build via the factory, apply the
        launch spec (resume or seed), then register. The root-process mirror of
        ``spawn``.
        """
        session = self._session_factory.create_lead_session(
            scheduler=self,
            launch=launch,
            budget=self._max_budget_tokens,
        )
        session.apply_launch(launch)
        return self.register_lead(session)

    async def spawn(
        self,
        parent_aid: int,
        role: str,
        task: str,
        context: str = "",
    ) -> int:
        """Non-blocking spawn. Creates SCB, builds session, starts task. Returns aid."""
        aid = self.table.allocate_aid()

        # Build environment
        env = await self._worktree_pool.acquire(role)
        budget = split_budget(self._max_budget_tokens, self.used_tokens)

        # Build session via factory
        session = self._session_factory.build_spawn_session(
            role=role,
            env=env,
            budget=budget,
            aid=aid,
        )

        # Add task to session messages
        delegation = DelegationTask(role=role, task=task, context=context)
        await session.add_user_message(delegation.render())
        session.state.set_phase(SessionPhase.SCHEDULED)

        # Create SCB
        scb = SessionControlBlock(
            aid=aid,
            parent_aid=parent_aid,
            agent=session.agent,
            state=session.state,
        )
        self.table.add(scb)

        # Emit spawn event
        await self._emit_scheduler_event(
            "agent_spawned",
            {"aid": aid, "parent_aid": parent_aid, "role": role, "task": task[:100]},
        )

        # Start async task
        self._tasks[aid] = asyncio.create_task(self._run_agent(aid, session))

        return aid

    async def _run_agent(self, aid: int, session: Any) -> None:
        """Execute a spawned agent to completion."""
        scb = self.table.get(aid)
        if scb is None:
            return

        start = time.monotonic()

        try:
            result = await session.run_loop()
            scb.result = result

            # Append worktree diff if available
            env = getattr(session, "env", None)
            if env is not None:
                result = await self._append_worktree_diff(env, result)

            latency = time.monotonic() - start

            if self._tracer:
                self._tracer.log_step(
                    step_type="agent_completed",
                    payload={"aid": aid, "role": scb.agent.name, "result_len": len(result)},
                    tokens=session.used_tokens,
                    latency=latency,
                )

            await self._emit_scheduler_event(
                "agent_completed",
                {
                    "aid": aid,
                    "parent_aid": scb.parent_aid,
                    "role": scb.agent.name,
                    "latency": latency,
                    "result_len": len(result),
                },
            )

            # Inject result into parent
            if scb.parent_aid is not None:
                await self._inject_result(scb.parent_aid, aid, result)

        except asyncio.CancelledError:
            scb.state.cancel()
            await self._emit_scheduler_event(
                "agent_cancelled",
                {"aid": aid, "role": scb.agent.name},
            )
            raise

        except Exception as exc:
            scb.state.fail()
            scb.result = f"Error: {exc}"
            await self._emit_scheduler_event(
                "agent_failed",
                {"aid": aid, "role": scb.agent.name, "error": str(exc)},
            )
            # Inject error as result so parent can handle it
            if scb.parent_aid is not None:
                await self._inject_result(scb.parent_aid, aid, f"Error: {exc}")

    async def _inject_result(
        self,
        parent_aid: int,
        child_aid: int,
        result: str,
    ) -> None:
        """Inject child result into parent's messages as a system message."""
        parent_scb = self.table.get(parent_aid)
        if parent_scb is None:
            return

        # Only inject if parent is still running (not terminal)
        if parent_scb.state.phase.is_terminal():
            return

        parent_scb.state.append_message({
            "role": "system",
            "content": f"[Agent {child_aid} completed]\n{result}",
        })

    async def _append_worktree_diff(self, env: EnvironmentPort, result: str) -> str:
        """If env is a worktree, append its diff to the result."""
        get_diff = getattr(env, "get_diff", None)
        if not callable(get_diff):
            return result
        diff = await get_diff()
        if not diff:
            return result
        if len(diff) > 12_000:
            diff = (
                diff[:6_000]
                + f"\n\n... [{len(diff) - 12_000} chars truncated] ...\n\n"
                + diff[-6_000:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def _emit_scheduler_event(self, event_type: str, data: dict) -> None:
        """Emit a scheduler event via the event sink."""
        if self._event_sink is None:
            return
        event = SchedulerEvent(type=event_type, data=data)
        if hasattr(self._event_sink, "emit"):
            await self._event_sink.emit(event)
        else:
            result = self._event_sink(event)
            if asyncio.iscoroutine(result):
                await result

    async def run(self, user_message: str) -> str:
        """Send message to lead and run until all spawned agents finish."""
        if self._lead_session is None:
            raise RuntimeError("Scheduler has no lead session. Call create_init_process() first.")

        await self._lead_session.add_user_message(user_message)
        self._tasks[0] = asyncio.create_task(self._lead_session.run_loop())

        # Wait loop: handle dynamically spawned tasks
        while self._tasks:
            pending = {t for t in self._tasks.values() if not t.done()}
            if not pending:
                break
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                for aid, t in list(self._tasks.items()):
                    if t is task:
                        del self._tasks[aid]
                        break

        # Return lead's last assistant message content
        for msg in reversed(self._lead_session.state.messages):
            if msg.get("role") == "assistant" and msg.get("content"):
                return msg["content"]
        return ""

    async def cleanup(self) -> None:
        """Cancel pending tasks and clean up worktree environments."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        await self._worktree_pool.release()

    async def spawn_with_review(
        self,
        parent_aid: int,
        task: str,
        context: str = "",
        max_iterations: int = 3,
    ) -> str:
        """Self-Collaboration: Coder -> Reviewer loop.

        Runs sequentially within the scheduler but tracks each agent in the
        SessionTable for observability.
        """
        current_task = task
        last_result = ""

        for iteration in range(1, max_iterations + 1):
            await self._emit_scheduler_event(
                "review_started",
                {
                    "tool": "review_loop",
                    "iteration": iteration,
                    "max": max_iterations,
                },
            )

            # Spawn coder and wait
            coder_aid = await self.spawn(parent_aid, "coder", current_task, context)
            await self._tasks[coder_aid]
            coder_scb = self.table.get(coder_aid)
            code_result = coder_scb.result if coder_scb else ""

            # Spawn reviewer and wait
            review_prompt = (
                f"Review the following implementation for task: '{task}'\n\n"
                f"Implementation result:\n{code_result}\n\n"
                f"Your response MUST end with a verdict line in exactly this format:\n"
                f"VERDICT: PASS\n"
                f"or\n"
                f"VERDICT: FAIL\n\n"
                f"If FAIL, provide detailed fix instructions before the verdict line."
            )
            reviewer_aid = await self.spawn(parent_aid, "reviewer", review_prompt)
            await self._tasks[reviewer_aid]
            reviewer_scb = self.table.get(reviewer_aid)
            review_result = reviewer_scb.result if reviewer_scb else ""

            verdict = ReviewVerdict.parse(review_result)
            await self._emit_scheduler_event(
                "review_completed",
                {
                    "tool": "review_loop",
                    "iteration": iteration,
                    "verdict": "PASS" if verdict.passed else "FAIL",
                },
            )

            if verdict.passed:
                return (
                    f"[Self-Collaboration: PASSED after {iteration} iteration(s)]\n\n"
                    f"{code_result}"
                )

            current_task = (
                f"Your previous implementation failed review (iteration {iteration}/{max_iterations}).\n"
                f"Original task: {task}\n\n"
                f"Reviewer feedback:\n{review_result}\n\n"
                f"Fix the issues identified by the reviewer."
            )
            last_result = code_result

        return (
            f"[Self-Collaboration: FAILED after {max_iterations} iterations]\n\n"
            f"Last implementation:\n{last_result}"
        )


__all__ = ["LaunchSpec", "Scheduler"]
