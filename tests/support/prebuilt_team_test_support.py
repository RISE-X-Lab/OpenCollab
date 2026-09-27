"""Real scheduler preparation shared by prebuilt-team tests."""

from opencollab.adapters.trace import Tracer
from opencollab.bootstrap import build_runtime_context, build_scheduler
from opencollab.bootstrap.team_config import ANALYST_TOOL_NAMES, TeamConfig, default_team_config

CONFIG = {
    "model": "gpt-4o",
    "provider": "openai",
    "api_key": "test-key",  # pragma: allowlist secret
    "base_url": None,
    "budget": 1_000_000,
}


def _prebuildable_default() -> TeamConfig:
    """The built-in team with the one change prebuild mode forces on it.

    A prebuilt team refuses ``spawn_agent``, so ``message_agent`` is the only
    tool left that can walk a topology edge — and ``build_scheduler`` refuses a
    prebuilt team whose Analyst is given two outgoing edges and nothing to walk
    them with. The built-in team is exactly that team: it delegates by spawning
    and collects results on the join path, which is legitimate everywhere except
    here.

    Everything else is the shipped configuration verbatim — the same three
    roles, the same edges, the Coder's and Tester's tool bundles untouched — so
    the assertions below still pin what OpenCollab ships.
    """
    team = default_team_config()
    roles = dict(team.roles)
    roles["analyst"] = roles["analyst"].model_copy(
        update={"tools": sorted({*ANALYST_TOOL_NAMES, "message_agent"})}
    )
    return TeamConfig(roles=roles, topology=team.topology, entry=team.entry)


def _scheduler(
    tmp_path,
    *,
    prebuild_team,
    use_worktrees=False,
    workspace=None,
    interactive=False,
    **kwargs,
):
    """A fully wired scheduler over a throwaway workspace, plus its tracer."""
    if prebuild_team and not {"team_config_path", "resolved_team_config"} & set(kwargs):
        kwargs["resolved_team_config"] = _prebuildable_default()
    if workspace is None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "README.md").write_text("hi", encoding="utf-8")
    traces = tmp_path / "traces"
    traces.mkdir(exist_ok=True)
    tracer = Tracer(run_id="prebuild", output_dir=str(traces))
    ctx = build_runtime_context(str(workspace), dict(CONFIG), trace=False)
    ctx.tracer = tracer
    scheduler = build_scheduler(
        ctx,
        use_worktrees=use_worktrees,
        interactive=interactive,
        auto_save=False,
        prebuild_team=prebuild_team,
        **kwargs,
    )
    return scheduler, tracer


def _roles(scheduler) -> dict[int, str]:
    return {aid: scb.agent.name for aid, scb in sorted(scheduler.table.entries.items())}
