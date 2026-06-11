"""Role -> Agent assembly: ``SpawnConfig`` + ``ContextBuilder``.

``ContextBuilder`` owns the single role -> ``Agent`` assembly used for both the
lead and spawned agents. ``build_plan`` is the editorial step: it emits an
ordered set of ``ContextSource`` objects, each tagged with its layer /
load-timing / structural position; ``build_agent`` resolves the role's tool
*names* to concrete Tools and folds the plan's SYSTEM sources into the agent's
system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from opencollab.adapters.trace import Tracer
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import (
    AskUserPort,
    PermissionPort,
    SafetyPolicyFactory,
    SchedulerPort,
)
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.bootstrap.tool_registry import COORDINATION_TOOL_NAMES, build_tools_for_role
from opencollab.domain.agent import Agent
from opencollab.domain.context import (
    ContextLayer,
    ContextPlan,
    ContextPosition,
    ContextSource,
    LoadTiming,
)
from opencollab.domain.scheduler import DelegationTask


@dataclass
class SpawnConfig:
    """Shared LLM/runtime config inherited by every spawned agent session."""

    model: str
    provider: str
    api_key: str | None
    base_url: str | None
    llm_timeout: float
    tracer: Tracer | None
    event_bus: EventBus
    permission_policy: PermissionPort | None
    ask_policy: AskUserPort | None = None
    safety_policy_factory: SafetyPolicyFactory | None = None


class ContextBuilder:
    """Turns a role name into a ready-to-run ``Agent`` + its context plan.

    Owns the single role -> ``Agent`` assembly used for both the lead and
    spawned agents. ``build_plan`` is the editorial step: it emits an ordered
    set of ``ContextSource`` objects, each tagged with its layer / load-timing /
    structural position. Identity and team land in the system prompt; the task
    and (reserved) project/memory layers are user-context. ``build_agent``
    resolves the role's tool *names* to concrete Tools and folds the plan's
    SYSTEM sources into ``Agent.system_prompt``. Tool descriptions are NOT
    injected into the prompt — those already reach the LLM as function-calling
    schemas, so the tool-meta layer is a registered-but-deferred source.
    """

    def __init__(self, team_cfg: TeamConfig, cfg: SpawnConfig):
        self._team = team_cfg
        self._cfg = cfg

    def build_plan(
        self,
        role_name: str,
        *,
        task: str | None = None,
        context: str = "",
    ) -> ContextPlan:
        """Emit the ordered context sources for ``role_name``.

        Startup sources (identity, team, and the task when given) carry content
        and are assembled into messages; project/memory/tool-meta are registered
        with a ``loader_key`` for a future lazy-loading pass but contribute no
        content this period.
        """
        role = self._team.role_for(role_name)
        sources: list[ContextSource] = [
            ContextSource(
                name="identity",
                layer=ContextLayer.IDENTITY,
                timing=LoadTiming.STARTUP,
                position=ContextPosition.SYSTEM,
                content=role.prompt,
            )
        ]
        team_section = self._team_section(role_name, role)
        if team_section:
            sources.append(
                ContextSource(
                    name="team",
                    layer=ContextLayer.TEAM,
                    timing=LoadTiming.STARTUP,
                    position=ContextPosition.SYSTEM,
                    content=team_section,
                )
            )
        # Project conventions — reserved; registered now, loaded later.
        sources.append(
            ContextSource(
                name="project",
                layer=ContextLayer.PROJECT,
                timing=LoadTiming.ON_DEMAND,
                position=ContextPosition.USER_CONTEXT,
                loader_key="project",
            )
        )
        if task is not None:
            sources.append(
                ContextSource(
                    name="task",
                    layer=ContextLayer.TASK,
                    timing=LoadTiming.STARTUP,
                    position=ContextPosition.USER_CONTEXT,
                    content=DelegationTask(
                        role=role_name, task=task, context=context
                    ).render(),
                )
            )
        # Recalled memory — reserved; registered now, loaded later.
        sources.append(
            ContextSource(
                name="memory",
                layer=ContextLayer.MEMORY,
                timing=LoadTiming.ON_DEMAND,
                position=ContextPosition.USER_CONTEXT,
                loader_key="memory",
            )
        )
        # Tool schemas already reach the model via function-calling; the layer
        # is registered for completeness, not injected as prose.
        sources.append(
            ContextSource(
                name="tool_meta",
                layer=ContextLayer.TOOL_META,
                timing=LoadTiming.ON_DEMAND,
                position=ContextPosition.SYSTEM,
                loader_key="tools",
            )
        )
        return ContextPlan(sources=tuple(sources))

    def build_agent(
        self,
        role_name: str,
        *,
        scheduler: SchedulerPort | None = None,
        interactive: bool = False,
        plan: ContextPlan | None = None,
    ) -> Agent:
        role = self._team.role_for(role_name)
        if plan is None:
            plan = self.build_plan(role_name)
        tools = build_tools_for_role(
            role.tools, scheduler=scheduler, interactive=interactive
        )
        cfg = self._cfg
        return Agent(
            name=role_name,
            system_prompt=plan.system_prompt(),
            tools=tools,
            model=role.model or cfg.model,
            provider=cfg.provider,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
        )

    def _team_section(self, role_name: str, role: RoleConfig) -> str:
        topo = self._team.topology
        # The permissive default carries generic guidance in the base prompt;
        # the dynamic section only adds value for an explicit topology graph.
        if topo.allow_all:
            return ""
        targets = sorted(topo.edges.get(role_name, frozenset()))
        if not targets or not (set(role.tools) & COORDINATION_TOOL_NAMES):
            return ""
        lines = ["## Your team", "", "Roles you may spawn or message:"]
        lines += [f"- {t}" for t in targets]
        if {"team_status", "message_agent"} & set(role.tools):
            lines += [
                "",
                "Use `team_status` to list live agents (with their ids) and "
                "`message_agent` to send an async message to one.",
            ]
        return "\n".join(lines)


__all__ = ["ContextBuilder", "SpawnConfig"]
