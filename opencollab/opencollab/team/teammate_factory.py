"""Construct a teammate Session given a role, environment, and shared config.

Centralizes the per-teammate Agent + Session wiring and the budget split
(reserve some headroom for the Lead's follow-up turns).
"""

from __future__ import annotations

from dataclasses import dataclass

from opencollab.core.agent import Agent
from opencollab.core.env import Environment
from opencollab.core.session import EventBus, PermissionPolicy, Session
from opencollab.core.tracer import Tracer
from opencollab.team.prompts import get_role_prompt
from opencollab.tools.bash import BashTool
from opencollab.tools.fs import FileReadTool, FileWriteTool, GrepTool


@dataclass
class TeammateConfig:
    """Shared LLM/runtime config every teammate inherits from the Team."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPolicy | None
    repo_map: str | None


def split_budget(total: int, used: int) -> int:
    """How many tokens this teammate gets, reserving headroom for the Lead.

    Same arithmetic as the previous inline version — extracted so it can be
    unit-tested directly without spinning up a Team.

    - Always leaves the teammate at least 10_000 tokens (a floor for any real
      reasoning).
    - Reserves min(25% of original total, remaining - 10_000) for the Lead.
    """
    remaining = max(10_000, total - used)
    reserve_for_lead = min(
        max(10_000, total // 4),
        max(0, remaining - 10_000),
    )
    return max(10_000, remaining - reserve_for_lead)


def build_teammate_session(
    *,
    role: str,
    env: Environment,
    cfg: TeammateConfig,
    budget: int,
    max_steps: int = 50,
) -> Session:
    """Build the teammate Agent + Session bundle.

    Tools are stateless; ToolCallProcessor derives a worktree-rooted
    SandboxInterceptor from env.workspace (step 5).
    """
    agent = Agent(
        name=role,
        system_prompt=get_role_prompt(role),
        tools=[BashTool(), FileReadTool(), FileWriteTool(), GrepTool()],
        model=cfg.model,
        provider=cfg.provider,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
    )
    return Session(
        agent=agent,
        env=env,
        tracer=cfg.tracer,
        max_budget_tokens=budget,
        max_steps=max_steps,
        event_sink=cfg.event_bus,
        permission_policy=cfg.permission_policy,
        repo_map=cfg.repo_map,
    )
