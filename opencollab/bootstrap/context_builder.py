"""Role -> Agent assembly: ``SpawnConfig`` + ``ContextBuilder``.

``ContextBuilder`` owns the single role -> ``Agent`` assembly used for both the
lead and spawned agents. ``build_plan`` is the editorial step: it emits an
ordered set of ``ContextSource`` objects, each tagged with its layer /
load-timing / structural position; ``build_agent`` resolves the role's tool
*names* to concrete Tools and folds the plan's SYSTEM sources into the agent's
system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencollab.adapters.skills.null_skill_store import NullSkillStore
from opencollab.adapters.trace import Tracer
from opencollab.application.event_bus import EventBus
from opencollab.application.ports import (
    AskUserPort,
    PermissionPort,
    SafetyPolicyFactory,
    SchedulerPort,
    SkillStorePort,
)
from opencollab.bootstrap.config import (
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING,
    DEFAULT_THINKING_PARAMS,
    DEFAULT_TOP_P,
)
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig
from opencollab.bootstrap.tool_registry import COORDINATION_TOOL_NAMES, build_tools_for_role
from opencollab.domain.agent import DEFAULT_MAX_TOKENS_PER_STEP, Agent
from opencollab.domain.context import (
    ContextLayer,
    ContextPlan,
    ContextPosition,
    ContextSource,
)
from opencollab.domain.scheduler import DelegationTask
from opencollab.domain.skill import SkillManifest


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
    # Global sampling-temperature default; a role may override it (see
    # ``ContextBuilder.build_agent``). Defaulted so the field stays optional for
    # the many call sites that construct a ``SpawnConfig`` by keyword.
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float | None = DEFAULT_TOP_P
    max_output_tokens: int = DEFAULT_MAX_TOKENS_PER_STEP
    context_window: int | None = None
    # Global thinking passthrough; a role may override it (resolved in
    # ``ContextBuilder.build_agent``). Defaulted (OFF) so the field stays optional.
    thinking: bool = DEFAULT_THINKING
    thinking_params: dict = field(default_factory=lambda: dict(DEFAULT_THINKING_PARAMS))
    wire_protocol: str = "chat_completions"
    reasoning_effort: str | None = None
    llm_max_retries: int = 3
    llm_connect_timeout: float = 30.0
    llm_first_event_timeout: float = 180.0
    llm_stream_idle_timeout: float = 180.0


class ContextBuilder:
    """Turns a role name into a ready-to-run ``Agent`` + its context plan.

    Owns the single role -> ``Agent`` assembly used for both the lead and
    spawned agents. ``build_plan`` is the editorial step: it emits an ordered
    set of ``ContextSource`` objects, each tagged with its layer and structural
    position. Identity, team and (when present) the project repo map land in the
    system prompt; the task lands as a user-context message. ``build_agent``
    resolves the role's tool *names* to concrete Tools and folds the plan's
    SYSTEM sources into ``Agent.system_prompt``. Tool schemas are NOT injected
    as prose — they already reach the LLM as function-calling schemas.
    """

    def __init__(
        self,
        team_cfg: TeamConfig,
        cfg: SpawnConfig,
        *,
        skill_store: SkillStorePort | None = None,
        project_context: str | None = None,
    ):
        self._team = team_cfg
        self._cfg = cfg
        # The skill store is a construction dep: a role's catalog is injected at
        # plan time and the dispatcher tool is bound to this store. Defaults to
        # an empty store so call sites that do not wire skills behave as before.
        self._skill_store: SkillStorePort = skill_store or NullSkillStore()
        # Startup content for the PROJECT layer (e.g. a repo map built by the
        # composition root). Empty string means "none".
        self._project_context = project_context or None

    def build_plan(
        self,
        role_name: str,
        *,
        task: str | None = None,
        context: str = "",
    ) -> ContextPlan:
        """Emit the ordered context sources for ``role_name``.

        Identity, the team section, the skill catalog and (when present) the
        project repo map carry content and assemble into the system prompt; the
        task, when given, becomes the sole user-context message. A source is
        emitted only when it has content — reserved long-term layers (memory,
        RAG) are an honest gap, not registered-but-empty placeholders.
        """
        role = self._team.role_for(role_name)
        sources: list[ContextSource] = [
            ContextSource(
                name="identity",
                layer=ContextLayer.IDENTITY,
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
                    position=ContextPosition.SYSTEM,
                    content=team_section,
                )
            )
        # Skill catalog — injected only when the role may invoke ``use_skill``
        # (no invoke permission → no catalog), and only when at least one skill
        # exists. SYSTEM position folds it into the cache-stable system prefix.
        if "use_skill" in role.tools:
            catalog = _render_skill_catalog(self._skill_store.list_manifests())
            if catalog:
                sources.append(
                    ContextSource(
                        name="skills",
                        layer=ContextLayer.SKILL,
                        position=ContextPosition.SYSTEM,
                        content=catalog,
                    )
                )
        # Project layer: a repo map from the composition root, when present,
        # ships at startup in the system prompt — SYSTEM so both the lead and
        # spawn paths pick it up (only spawns seed user-context messages). With
        # no repo map the layer simply does not appear.
        if self._project_context:
            sources.append(
                ContextSource(
                    name="project",
                    layer=ContextLayer.PROJECT,
                    position=ContextPosition.SYSTEM,
                    content=self._project_context,
                )
            )
        if task is not None:
            sources.append(
                ContextSource(
                    name="task",
                    layer=ContextLayer.TASK,
                    position=ContextPosition.USER_CONTEXT,
                    content=DelegationTask(
                        role=role_name, task=task, context=context
                    ).render(),
                )
            )
        return ContextPlan(sources=tuple(sources))

    def build_agent(
        self,
        role_name: str,
        *,
        scheduler: SchedulerPort | None = None,
        interactive: bool = False,
        allow_unisolated_tests: bool = False,
        plan: ContextPlan | None = None,
    ) -> Agent:
        role = self._team.role_for(role_name)
        if plan is None:
            plan = self.build_plan(role_name)
        tools = build_tools_for_role(
            role.tools,
            scheduler=scheduler,
            skill_store=self._skill_store,
            interactive=interactive,
            allow_unisolated_tests=allow_unisolated_tests,
            tool_limits=self._team.tool_limits,
        )
        cfg = self._cfg
        # A role override of 0.0 is meaningful (fully deterministic), so fall
        # back to the global default only when the role left it unset (None).
        temperature = (
            role.temperature if role.temperature is not None else cfg.temperature
        )
        # Thinking resolves the same way: an explicit per-role value (incl. an
        # explicit False) wins; otherwise inherit the global SpawnConfig value.
        thinking = role.thinking if role.thinking is not None else cfg.thinking
        thinking_params = (
            role.thinking_params
            if role.thinking_params is not None
            else cfg.thinking_params
        )
        return Agent(
            name=role_name,
            system_prompt=plan.system_prompt(),
            tools=tools,
            model=role.model or cfg.model,
            provider=cfg.provider,
            wire_protocol=cfg.wire_protocol,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            temperature=temperature,
            top_p=cfg.top_p,
            max_tokens_per_step=cfg.max_output_tokens,
            context_window=cfg.context_window,
            thinking=thinking,
            thinking_params=thinking_params,
            reasoning_effort=cfg.reasoning_effort,
            llm_max_retries=cfg.llm_max_retries,
            llm_connect_timeout=cfg.llm_connect_timeout,
            llm_first_event_timeout=cfg.llm_first_event_timeout,
            llm_stream_idle_timeout=cfg.llm_stream_idle_timeout,
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


def _render_skill_catalog(manifests: tuple[SkillManifest, ...]) -> str:
    """Render the model-facing skill catalog, or ``""`` when there are none.

    A short header instructs the model to call ``use_skill(name)``, followed by
    one ``- {name}: {description}`` line per available skill. Returning an empty
    string when the store is empty lets ``build_plan`` skip the source entirely.
    """
    if not manifests:
        return ""
    lines = [
        "## Skills",
        "",
        "Specialized instruction sets you can load on demand. When one matches "
        "the task, call `use_skill(name)` to pull its full instructions into "
        "your context.",
        "",
    ]
    lines += [f"- {m.name}: {m.description}" for m in manifests]
    return "\n".join(lines)


__all__ = ["ContextBuilder", "SpawnConfig"]
