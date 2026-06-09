"""Spawn-session wiring used by the scheduler.

``build_spawn_session`` and ``DefaultSessionFactory`` assemble the Agent +
Session bundle for spawned children and for agent 0 (the lead). Both delegate
role -> Agent assembly to a ``ContextBuilder`` and session construction to
``build_session`` (in ``container``); this module owns only the per-run
transcript-path helpers and the factory glue.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from opencollab.adapters.env import Environment, LocalEnvironment
from opencollab.application.scheduler import LaunchSpec
from opencollab.application.session import Session
from opencollab.bootstrap.container import build_session, build_workspace_safety_policy
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.team_config import TeamConfig, default_team_config


def agent_save_path(save_dir: str, aid: int, role: str) -> str:
    """Per-agent transcript path within a run folder: ``agent_<aid>_<role>.json``."""
    return os.path.join(save_dir, f"agent_{aid}_{role}.json")


def make_run_dir(workspace: str) -> str:
    """A timestamped run folder under ``<workspace>/.opencollab/sessions``.

    A 4-char suffix is appended if a same-second folder already exists, so two
    runs started within the same second do not collide.
    """
    base = os.path.join(workspace, ".opencollab", "sessions")
    run_dir = os.path.join(base, datetime.now().strftime("%Y-%m-%dT%H-%M-%S"))
    if os.path.exists(run_dir):
        run_dir = f"{run_dir}-{uuid.uuid4().hex[:4]}"
    return run_dir


def build_spawn_session(
    *,
    role: str,
    env: Environment,
    cfg: SpawnConfig,
    budget: int,
    max_steps: int = 50,
    aid: int = -1,
    scheduler: Any = None,
    team_cfg: TeamConfig | None = None,
    task: str | None = None,
    context: str = "",
) -> Session:
    """Build the Agent + Session bundle for a spawned child agent.

    ``team_cfg`` defaults to the lead-only default team, so roles resolve to the
    generic spec (base tools). The safety policy is derived from the child's
    environment. When ``task`` is given it is seeded as the agent's first
    user-context message (the TASK-layer source), so no separate
    ``add_user_message`` is needed.
    """
    safety_policy = (
        cfg.safety_policy_factory(env)
        if cfg.safety_policy_factory is not None
        else None
    )
    builder = ContextBuilder(team_cfg or default_team_config(), cfg)
    plan = builder.build_plan(role, task=task, context=context)
    agent = builder.build_agent(role, scheduler=scheduler, interactive=False, plan=plan)
    return build_session(
        agent=agent,
        env=env,
        tracer=cfg.tracer,
        max_budget_tokens=budget,
        max_steps=max_steps,
        event_sink=cfg.event_bus,
        permission_policy=cfg.permission_policy,
        safety_policy=safety_policy,
        llm_timeout=cfg.llm_timeout,
        aid=aid,
        seed_user_messages=plan.startup_user_messages(),
    )


class DefaultSessionFactory:
    """Default ``SessionFactoryPort`` implementation used by the scheduler.

    Holds the shared ``SpawnConfig`` (LLM config inherited by every session),
    the resolved ``TeamConfig`` (role prompts/tools + topology), and the
    lead-only composition bits (``lead_workspace`` for the local environment and
    ``interactive`` for the ask-user tool). All role -> Agent assembly is
    delegated to a single ``ContextBuilder``.
    """

    def __init__(
        self,
        cfg: SpawnConfig,
        *,
        team_cfg: TeamConfig | None = None,
        lead_workspace: str | None = None,
        interactive: bool = False,
        save_dir: str | None = None,
    ):
        self._cfg = cfg
        self._team = team_cfg or default_team_config()
        self._context_builder = ContextBuilder(self._team, cfg)
        self._lead_workspace = lead_workspace
        self._interactive = interactive
        # Run folder where every agent's transcript is persisted. When set,
        # spawned children get their own ``agent_<aid>_<role>.json`` autosave.
        self._save_dir = save_dir

    def build_spawn_session(
        self,
        *,
        role: str,
        env: Any,
        budget: int,
        max_steps: int = 50,
        aid: int = -1,
        scheduler: Any = None,
        task: str | None = None,
        context: str = "",
    ) -> Session:
        cfg = self._cfg
        safety_policy = (
            cfg.safety_policy_factory(env)
            if cfg.safety_policy_factory is not None
            else None
        )
        plan = self._context_builder.build_plan(role, task=task, context=context)
        agent = self._context_builder.build_agent(
            role, scheduler=scheduler, interactive=False, plan=plan
        )
        auto_save_path = (
            agent_save_path(self._save_dir, aid, role)
            if self._save_dir
            else None
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            max_steps=max_steps,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=safety_policy,
            auto_save_path=auto_save_path,
            llm_timeout=cfg.llm_timeout,
            aid=aid,
            seed_user_messages=plan.startup_user_messages(),
        )

    def create_lead_session(
        self,
        *,
        scheduler: Any,
        launch: LaunchSpec,
        budget: int,
        aid: int = 0,
    ) -> Session:
        """Build agent 0 with its local env, entry-role tools, and prompt.

        The entry role comes from the team config (``team_cfg.entry``), so agent
        0 is whichever root the team declares — not a hardcoded ``lead``. The
        scheduler-bound tools are resolved against ``scheduler`` here, so the
        lead<->scheduler cycle is closed inside this single handshake.
        ``launch.auto_save_path`` wires the auto-save subscriber; resume/seed is
        left to ``Session.apply_launch``.
        """
        cfg = self._cfg
        env = LocalEnvironment(self._lead_workspace)
        agent = self._context_builder.build_agent(
            self._team.entry, scheduler=scheduler, interactive=self._interactive
        )
        return build_session(
            agent=agent,
            env=env,
            tracer=cfg.tracer,
            max_budget_tokens=budget,
            event_sink=cfg.event_bus,
            permission_policy=cfg.permission_policy,
            safety_policy=build_workspace_safety_policy(env),
            auto_save_path=launch.auto_save_path,
            llm_timeout=cfg.llm_timeout,
            aid=aid,
        )


__all__ = [
    "DefaultSessionFactory",
    "agent_save_path",
    "build_spawn_session",
    "make_run_dir",
]
