"""Construct teammate (and lead) Sessions for the Team layer.

Default implementation of ``application.ports.SessionFactoryPort``. The
team orchestrator uses a factory injected at construction time so it does
not import ``opencollab.core.session.Session`` directly; this module is
the documented transitional default that still touches the concrete
``Session`` class — bootstrap binds it to the port.

Centralizes the per-teammate Agent + Session wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opencollab.application.event_bus import EventBus
from opencollab.application.ports import PermissionPort, SafetyPolicyFactory
from opencollab.domain.agent import Agent
from opencollab.adapters.env import Environment
from opencollab.bootstrap.session import Session
from opencollab.adapters.trace import Tracer
from opencollab.application.team_prompts import get_role_prompt
from opencollab.adapters.tools.bash import BashTool
from opencollab.adapters.tools.fs import FileReadTool, FileWriteTool, GrepTool


@dataclass
class TeammateConfig:
    """Shared LLM/runtime config every teammate inherits from the Team."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPort | None
    repo_map: str | None
    safety_policy_factory: SafetyPolicyFactory | None = None


def build_teammate_session(
    *,
    role: str,
    env: Environment,
    cfg: TeammateConfig,
    budget: int,
    max_steps: int = 50,
) -> Session:
    """Build the teammate Agent + Session bundle.

    Tools are stateless; safety policy wiring is derived from the teammate
    environment and passed into the Session.
    """
    safety_policy = (
        cfg.safety_policy_factory(env)
        if cfg.safety_policy_factory is not None
        else None
    )

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
        safety_policy=safety_policy,
        repo_map=cfg.repo_map,
    )


class DefaultSessionFactory:
    """Default ``SessionFactoryPort`` implementation used by ``Team``.

    Holds the shared ``TeammateConfig`` so the team orchestrator can build
    teammate sessions by role/env/budget without re-supplying LLM config.
    Also exposes ``build_lead_session`` so the orchestrator can construct
    the Lead session without importing ``Session`` directly.
    """

    def __init__(self, cfg: TeammateConfig):
        self._cfg = cfg

    def build_teammate_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
    ) -> Session:
        return build_teammate_session(
            role=role,
            env=env,
            cfg=self._cfg,
            budget=budget,
            max_steps=max_steps,
        )

    def build_lead_session(
        self,
        *,
        agent: Agent,
        env: Environment,
        tracer: Tracer | None,
        max_budget_tokens: int,
        event_sink: Any,
        permission_policy: PermissionPort | None,
        safety_policy: Any,
        repo_map: str | None,
        max_steps: int | None = None,
    ) -> Session:
        kwargs: dict[str, Any] = dict(
            agent=agent,
            env=env,
            tracer=tracer,
            max_budget_tokens=max_budget_tokens,
            event_sink=event_sink,
            permission_policy=permission_policy,
            safety_policy=safety_policy,
            repo_map=repo_map,
        )
        if max_steps is not None:
            kwargs["max_steps"] = max_steps
        return Session(**kwargs)
