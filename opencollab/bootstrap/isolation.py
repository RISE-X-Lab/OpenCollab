from __future__ import annotations

import asyncio
from typing import Any

from opencollab.adapters.session_environments import SessionEnvironmentPool
from opencollab.application.async_timeout import await_owned_operation
from opencollab.application.exception_notes import add_exception_note
from opencollab.application.isolation import isolation_name
from opencollab.bootstrap.environment_profiles import configured_environment_providers
from opencollab.bootstrap.team_config import load_team_config


async def prepare_scheduler(ctx: Any, *, builder: Any, isolation=None,
                            environment_providers=None, stop_requested: asyncio.Event | None = None,
                            **kwargs: Any) -> Any:
    if stop_requested is not None and stop_requested.is_set():
        raise asyncio.CancelledError
    team = kwargs.get("resolved_team_config") or load_team_config(
        ctx.workspace, path=kwargs.get("team_config_path"),
    )
    selected = isolation_name(isolation, default=team.isolation)
    roles = {name: role.isolation for name, role in team.roles.items() if role.isolation is not None}
    environment_providers = configured_environment_providers(ctx.config, environment_providers)
    if selected is None and not roles and not environment_providers:
        return builder(ctx, **kwargs)
    pool = SessionEnvironmentPool(
        ctx.workspace, isolation=selected or ("worktree" if kwargs["use_worktrees"] else "shared"),
        base_environment=kwargs.get("environment"), providers=environment_providers, roles=roles,
        owned_save_dir=kwargs.get("save_dir"),
        enable_adoption=any("adopt_agent" in role.tools for role in team.roles.values()),
        owned_output_paths=getattr(ctx.tracer, "owned_paths", ()),
    )
    try:
        # An unset strategy keeps the historical entry workspace even when a child overrides it.
        entry_isolation = roles.get(team.entry) or selected or "shared"
        entry = await pool.acquire(team.entry, isolation=entry_isolation)
        if stop_requested is not None and stop_requested.is_set():
            raise asyncio.CancelledError
        pool.set_source(entry, isolation=entry_isolation)
        scheduler = builder(ctx, **{**kwargs, "environment": entry, "worktree_pool": pool,
                                    "resolved_team_config": team})
        scheduler.preserve_entry_changes = entry_isolation != "shared"
        return scheduler
    except BaseException as exc:
        try:
            await await_owned_operation(pool.release())
        except (Exception, asyncio.CancelledError) as cleanup_error:
            add_exception_note(
                exc, f"Team preparation cleanup also failed: {type(cleanup_error).__name__}: {cleanup_error}",
            )
        raise
