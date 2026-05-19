"""Team Orchestrator — Lead + N Teammates with context isolation.

First Principle: Multi-agent cooperation = Tool-based delegation with isolated contexts.
The Lead calls delegate_task as a Tool. The framework spawns an isolated Session,
runs it to completion, and returns the summary.

Addresses blind spot #2: Parallel teammates use WorktreeEnvironment to avoid
corrupting each other's workspace state.

Ref:
- Design doc: Team class with delegate_task tool
- claude_code_agen_team.md: flat hierarchy, Lead fixed, permissions at spawn
- opencode: build/plan agents with permission system
- openclaw: lane-based concurrency with session locks
"""

from __future__ import annotations

import time
from typing import Any

from opencollab.domain.events import TeamEvent
from opencollab.application.event_bus import EventBus, EventSink
from opencollab.application.ports import (
    PermissionPort,
    SafetyPolicyFactory,
    SessionFactoryPort,
)
from opencollab.application.tool_runtime import ToolRuntime
from opencollab.domain.agent import Agent
from opencollab.adapters.env import Environment, LocalEnvironment, WorktreeEnvironment
from opencollab.adapters.trace import Tracer
from opencollab.tools.base import Tool
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool
from opencollab.team.prompts import LEAD_SYSTEM_PROMPT
from opencollab.team.teammate_factory import (
    DefaultSessionFactory,
    TeammateConfig,
    split_budget,
)
from opencollab.team.worktree_pool import WorktreePool


# ---------------------------------------------------------------------------
# Delegation tools (exposed to Lead as JSON Schema functions)
# ---------------------------------------------------------------------------

class DelegateTaskTool(Tool):
    """Tool that the Lead uses to delegate work to a teammate."""

    name = "delegate_task"
    description = (
        "Delegate a task to a specialist teammate. The teammate works in an isolated "
        "context and returns a summary. Available roles: analyst, coder, reviewer, "
        "or any custom name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "role": {
                "type": "string",
                "description": "The specialist role (e.g., 'coder', 'analyst', 'reviewer').",
            },
            "task": {
                "type": "string",
                "description": "Detailed task description for the teammate.",
            },
            "context": {
                "type": "string",
                "description": "Optional context to inject (e.g., relevant file contents, prior analysis).",
            },
        },
        "required": ["role", "task"],
    }

    def __init__(self, team: Team):
        self._team = team

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        role = params["role"]
        task = params["task"]
        context = params.get("context", "")
        return await self._team.delegate(role, task, context)


class DelegateWithReviewTool(Tool):
    """Tool for tasks requiring code review — Self-Collaboration loop.

    Ref: design doc delegate_with_review — Coder → Reviewer → iterate.
    """

    name = "delegate_with_review"
    description = (
        "Delegate a coding task with mandatory code review. A Coder implements the task, "
        "then a Reviewer checks the work. If the review fails, the Coder retries with "
        "feedback. Max 3 iterations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task to implement and review.",
            },
            "context": {
                "type": "string",
                "description": "Optional context (file contents, requirements, etc.).",
            },
            "max_iterations": {
                "type": "integer",
                "description": "Max review iterations (default 3).",
            },
        },
        "required": ["task"],
    }

    def __init__(self, team: Team):
        self._team = team

    async def execute_with_runtime(
        self,
        params: dict[str, Any],
        runtime: ToolRuntime,
    ) -> str:
        task = params["task"]
        context = params.get("context", "")
        max_iter = params.get("max_iterations", 3)
        return await self._team.delegate_with_review(task, context, max_iter)


# ---------------------------------------------------------------------------
# Team
# ---------------------------------------------------------------------------

class Team:
    """Orchestrates a Lead agent and N teammate agents.

    The Lead sees delegate_task and delegate_with_review as Tools. When called,
    the Team spawns (or reuses) an isolated Session for the teammate, runs it,
    and returns the summary to the Lead.

    Key design:
    - Flat hierarchy: Lead + N Teammates, no nesting
    - Context isolation: each teammate has its own message history
    - Workspace isolation: parallel teammates get separate WorktreeEnvironments
    - Budget sharing: total budget distributed across Lead and all teammates
    """

    def __init__(
        self,
        workspace: str = ".",
        model: str = "gpt-4o",
        provider: str = "openai",
        api_key: str | None = None,
        base_url: str | None = None,
        lead_prompt: str | None = None,
        max_budget_tokens: int = 500_000,
        tracer: Tracer | None = None,
        event_sink: EventSink | None = None,
        permission_policy: PermissionPort | None = None,
        use_worktrees: bool = True,
        repo_map: str | None = None,
        lead_env: Environment | None = None,
        lead_max_steps: int | None = None,
        safety_policy_factory: SafetyPolicyFactory | None = None,
        session_factory: SessionFactoryPort | None = None,
    ):
        self.workspace = workspace
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.tracer = tracer
        self.event_bus = EventBus(event_sink)
        self.permission_policy = permission_policy
        self.safety_policy_factory = safety_policy_factory
        self.repo_map = repo_map
        self._total_budget = max_budget_tokens
        self._used_tokens = 0
        self._worktree_pool = WorktreePool(workspace, use_worktrees=use_worktrees)

        self._teammate_cfg = TeammateConfig(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            tracer=tracer,
            event_bus=self.event_bus,
            permission_policy=permission_policy,
            safety_policy_factory=safety_policy_factory,
            repo_map=repo_map,
        )
        self._session_factory: SessionFactoryPort = (
            session_factory
            if session_factory is not None
            else DefaultSessionFactory(self._teammate_cfg)
        )

        # Build Lead agent with delegation tools
        delegate_tool = DelegateTaskTool(self)
        review_tool = DelegateWithReviewTool(self)

        # Lead also gets basic tools for quick tasks
        basic_tools = self._make_basic_tools()
        lead_runtime_env = lead_env if lead_env is not None else LocalEnvironment(workspace)
        lead_safety_policy = (
            safety_policy_factory(lead_runtime_env)
            if safety_policy_factory is not None
            else None
        )

        self.lead_agent = Agent(
            name="lead",
            system_prompt=lead_prompt or LEAD_SYSTEM_PROMPT,
            tools=[delegate_tool, review_tool] + basic_tools,
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
        )

        self.lead_session = self._session_factory.build_lead_session(
            agent=self.lead_agent,
            env=lead_runtime_env,
            tracer=tracer,
            max_budget_tokens=max_budget_tokens,
            event_sink=self.event_bus,
            permission_policy=permission_policy,
            safety_policy=lead_safety_policy,
            repo_map=repo_map,
            max_steps=lead_max_steps,
        )

    def _make_basic_tools(self) -> list[Tool]:
        """Standard tool set for the Lead.

        Tools are stateless; the Lead session receives safety policy wiring
        during Team construction. Teammate tools are built independently in
        teammate_factory.build_teammate_session.
        """
        return [BashTool(), FileReadTool(), FileWriteTool(), GrepTool()]

    @property
    def used_tokens(self) -> int:
        """Total tokens spent across the Lead session and all delegated teammates."""
        return self.lead_session.used_tokens + self._used_tokens

    async def run(self, user_message: str) -> str:
        """Send a user message to the Lead and run the team loop."""
        await self.lead_session.add_user_message(user_message)
        result = await self.lead_session.run_loop()
        return result

    async def delegate(self, role: str, task: str, context: str = "") -> str:
        """Spawn an isolated teammate to execute a task. Return its summary."""
        start = time.monotonic()
        await self.event_bus.emit(TeamEvent(
            type="delegation_started",
            data={"tool": "delegate", "role": role, "task": task[:100]},
        ))

        env = await self._worktree_pool.acquire(role)
        budget = split_budget(self._total_budget, self._used_tokens)
        teammate_session = self._session_factory.build_teammate_session(
            role=role, env=env, budget=budget,
        )

        task_message = f"Context:\n{context}\n\nTask:\n{task}" if context else task
        await teammate_session.add_user_message(task_message)
        result = await teammate_session.run_loop()

        self._used_tokens += teammate_session.used_tokens
        latency = time.monotonic() - start
        if self.tracer:
            self.tracer.log_step(
                step_type="delegate",
                payload={"role": role, "task": task[:200], "result_len": len(result)},
                tokens=teammate_session.used_tokens,
                latency=latency,
            )
        await self.event_bus.emit(TeamEvent(
            type="delegation_completed",
            data={
                "tool": "delegate",
                "role": role,
                "latency": latency,
                "result_len": len(result),
            },
        ))

        return await self._append_worktree_diff(env, result)

    async def _append_worktree_diff(self, env: Environment, result: str) -> str:
        """If env is a worktree, append its diff (truncated) to the result."""
        if not isinstance(env, WorktreeEnvironment):
            return result
        diff = await env.get_diff()
        if not diff:
            return result
        if len(diff) > 12_000:
            diff = (
                diff[:6_000]
                + f"\n\n... [{len(diff) - 12_000} chars truncated] ...\n\n"
                + diff[-6_000:]
            )
        return result + f"\n\n[Changes made in worktree]\n```diff\n{diff}\n```"

    async def delegate_with_review(
        self, task: str, context: str = "", max_iterations: int = 3
    ) -> str:
        """Self-Collaboration loop: Coder → Reviewer → iterate.

        Ref: design doc delegate_with_review — the only framework-level
        manifestation of Self-Collaboration thinking.
        """
        current_task = task
        last_result = ""

        for iteration in range(1, max_iterations + 1):
            await self.event_bus.emit(TeamEvent(
                type="review_started",
                data={
                    "tool": "review_loop",
                    "iteration": iteration,
                    "max": max_iterations,
                },
            ))

            # Step 1: Coder implements
            code_result = await self.delegate("coder", current_task, context)

            # Step 2: Reviewer checks
            # Ref: claude-code review plugin — use structured verdict line
            # to avoid false positives (e.g., "password" containing "PASS")
            review_prompt = (
                f"Review the following implementation for task: '{task}'\n\n"
                f"Implementation result:\n{code_result}\n\n"
                f"Your response MUST end with a verdict line in exactly this format:\n"
                f"VERDICT: PASS\n"
                f"or\n"
                f"VERDICT: FAIL\n\n"
                f"If FAIL, provide detailed fix instructions before the verdict line."
            )
            review_result = await self.delegate("reviewer", review_prompt)

            # Check for structured verdict (line must start with "VERDICT: PASS")
            passed = any(
                line.strip().upper() == "VERDICT: PASS"
                for line in review_result.splitlines()
            )
            await self.event_bus.emit(TeamEvent(
                type="review_completed",
                data={
                    "tool": "review_loop",
                    "iteration": iteration,
                    "verdict": "PASS" if passed else "FAIL",
                },
            ))
            if passed:
                return (
                    f"[Self-Collaboration: PASSED after {iteration} iteration(s)]\n\n"
                    f"{code_result}"
                )

            # Step 3: Feedback loop — give reviewer's notes to coder
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

    async def cleanup(self) -> None:
        """Clean up all worktree environments."""
        await self._worktree_pool.cleanup()
