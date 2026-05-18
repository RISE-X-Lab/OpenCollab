"""Session/Team builders for the CLI entry points."""

from __future__ import annotations

import os
import uuid

from opencollab.bootstrap.runtime import RuntimeContext
from opencollab.bootstrap.safety import build_workspace_safety_policy
from opencollab.bootstrap.tool_factory import build_default_tools
from opencollab.core.agent import Agent
from opencollab.core.env import LocalEnvironment
from opencollab.core.session import Session
from opencollab.team.orchestrator import Team
from opencollab.tools.human import AskUserTool


_CHAT_SYSTEM_PROMPT = (
    "You are a skilled software developer. Use the provided tools to help the user "
    "with coding tasks. Read files before modifying them. Verify your changes."
)


def build_chat_session(
    ctx: RuntimeContext,
    *,
    session_file: str | None = None,
    auto_save: bool = True,
) -> Session:
    """Build the single-agent chat Session.

    Constructs LocalEnvironment(ctx.workspace) internally. If session_file is
    provided and exists, uses Session.load(...); otherwise creates a fresh
    Session and immediately writes the auto-save JSONL.
    """
    cfg = ctx.config
    agent = Agent(
        name="assistant",
        system_prompt=_CHAT_SYSTEM_PROMPT,
        tools=build_default_tools(include_ask_user=True),
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
    )

    env = LocalEnvironment(ctx.workspace)
    safety_policy = build_workspace_safety_policy(env)

    auto_save_path: str | None = None
    if auto_save:
        session_id = uuid.uuid4().hex[:8]
        auto_save_dir = os.path.join(ctx.workspace, ".opencollab", "sessions")
        auto_save_path = os.path.join(auto_save_dir, f"{session_id}.jsonl")

    common_kwargs = dict(
        env=env,
        tracer=ctx.tracer,
        max_budget_tokens=cfg["budget"],
        event_sink=ctx.event_sink,
        permission_policy=ctx.permission_policy,
        safety_policy=safety_policy,
        repo_map=ctx.repo_map,
        auto_save_path=auto_save_path,
    )

    if session_file and os.path.exists(session_file):
        return Session.load(session_file, agent, **common_kwargs)

    session = Session(agent=agent, **common_kwargs)
    if auto_save_path:
        session.save(auto_save_path)
    return session


def build_team(
    ctx: RuntimeContext,
    *,
    use_worktrees: bool,
    interactive: bool,
) -> Team:
    """Build the multi-agent Team.

    interactive=True appends AskUserTool to team.lead_agent.tools post-construction
    (keeps headless eval clean — ref: SWE-bench regression root cause).
    """
    cfg = ctx.config
    team = Team(
        workspace=ctx.workspace,
        model=cfg["model"],
        provider=cfg["provider"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        max_budget_tokens=cfg["budget"],
        tracer=ctx.tracer,
        event_sink=ctx.event_sink,
        permission_policy=ctx.permission_policy,
        use_worktrees=use_worktrees,
        repo_map=ctx.repo_map,
        safety_policy_factory=build_workspace_safety_policy,
    )
    if interactive:
        # Interactive mode: give Lead the ask_user tool (not added in Team.__init__
        # to keep headless eval clean — ref: SWE-bench regression root cause)
        team.lead_agent.tools.append(AskUserTool())
    return team
