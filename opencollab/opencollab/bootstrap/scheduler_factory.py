"""Scheduler factory — the high-level CLI/eval entry point.

``build_scheduler`` loads the team config, wires every concrete dependency,
hands the ``Scheduler`` a ``LaunchSpec`` + ``Topology``, and lets it create
agent 0 (the init process). This is the one place that knows the full
collaborator graph for a multi-agent run.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from opencollab.adapters.hooks import ShellHookRunner
from opencollab.adapters.storage import SessionStore
from opencollab.adapters.worktree_pool import WorktreePool
from opencollab.application.event_bus import EventBus
from opencollab.application.hooks import HookEventSubscriber
from opencollab.application.scheduler import LaunchSpec, Scheduler
from opencollab.bootstrap.container import RuntimeContext, build_workspace_safety_policy
from opencollab.bootstrap.context_builder import SpawnConfig
from opencollab.bootstrap.session_factory import (
    DefaultSessionFactory,
    agent_save_path,
    make_run_dir,
)
from opencollab.bootstrap.team_config import load_team_config, resolve_team_file


def build_scheduler(
    ctx: RuntimeContext,
    *,
    use_worktrees: bool,
    interactive: bool,
    session_file: str | None = None,
    auto_save: bool = True,
    enable_hooks: bool = True,
) -> Scheduler:
    """Build the Scheduler and let it create agent 0 (the init process).

    Loads the team config (roles/tools/topology) from the workspace, wires
    dependencies, and hands the scheduler a ``LaunchSpec``;
    ``scheduler.create_init_process`` builds agent 0 (aid=0) through the factory
    and applies the launch spec. ``session_file`` resumes agent 0's history;
    ``auto_save`` writes a structured-JSON transcript per agent
    (``agent_<aid>_<role>.json``) plus a ``team.json`` manifest under a
    timestamped run folder in ``<workspace>/.opencollab/sessions``.

    When ``enable_hooks`` and the team config declares ``hooks``, a
    ``HookEventSubscriber`` is attached to the team event bus so configured
    shell commands fire on lifecycle events. Disable (e.g. under eval) to keep
    runs free of hook side effects.
    """
    cfg = ctx.config
    team_cfg = load_team_config(ctx.workspace)
    event_bus = EventBus(ctx.event_sink)

    # Per-run folder: every agent's transcript plus a team.json manifest land
    # here. Known before the factory so spawned children inherit the same dir.
    run_dir: str | None = make_run_dir(ctx.workspace) if auto_save else None
    lead_save_path = agent_save_path(run_dir, 0, team_cfg.entry) if run_dir else None

    session_factory = DefaultSessionFactory(
        SpawnConfig(
            model=cfg["model"],
            provider=cfg["provider"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            llm_timeout=cfg.get("llm_timeout", 600.0),
            tracer=ctx.tracer,
            event_bus=event_bus,
            permission_policy=ctx.permission_policy,
            safety_policy_factory=build_workspace_safety_policy,
        ),
        team_cfg=team_cfg,
        lead_workspace=ctx.workspace,
        interactive=interactive,
        save_dir=run_dir,
    )
    worktree_pool = WorktreePool(ctx.workspace, use_worktrees=use_worktrees)

    scheduler = Scheduler(
        session_factory=session_factory,
        worktree_pool=worktree_pool,
        event_sink=event_bus,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        permission_policy=ctx.permission_policy,
        topology=team_cfg.topology,
        roles=tuple(team_cfg.roles),
    )

    # Attach hooks after the scheduler exists so the runner can hold its handle
    # (the coordination-ready seam for a future ``agent`` executor). Subscribing
    # appends to the same bus, leaving the TUI sink at target[0] untouched.
    if enable_hooks and team_cfg.hooks:
        runner = ShellHookRunner(team_cfg.hooks, scheduler=scheduler)
        event_bus.subscribe(HookEventSubscriber(runner))

    # Persist a team.json manifest from the live roster on every roster change.
    # Wired before create_init_process so agent 0 is captured on registration.
    if run_dir is not None:
        store = SessionStore()
        manifest_path = os.path.join(run_dir, "team.json")
        team_file = resolve_team_file(ctx.workspace)
        run_id = os.path.basename(run_dir)
        started_at = datetime.now(timezone.utc).isoformat()

        def _write_manifest() -> None:
            store.save_manifest(manifest_path, {
                "run_id": run_id,
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "team_file": str(team_file) if team_file else None,
                "agents": scheduler.team_snapshot(),
            })

        scheduler.set_manifest_writer(_write_manifest)

    scheduler.create_init_process(
        LaunchSpec(session_file=session_file, auto_save_path=lead_save_path)
    )
    return scheduler


__all__ = ["build_scheduler"]
