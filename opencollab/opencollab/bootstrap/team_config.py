"""Team configuration — YAML-backed role + topology definitions.

A team file declares the agents that make up a collaboration: each role's
system prompt, optional model override, and the names of the tools it may use,
plus a directed ``topology`` of which role may spawn/message which other role.

Resolution order (highest priority first):
    OPENCOLLAB_TEAM_FILE  → explicit path
    <workspace>/configs/team.yaml
    <cwd>/configs/team.yaml

When no team file exists, ``default_team_config`` returns a lead-only team with
a permissive (``allow_all``) topology — the lead can still spawn ad-hoc roles
that fall back to ``default_role``.

Tool *names* are resolved to concrete Tool instances by ``ContextBuilder`` in
``bootstrap.container``; this module only carries the names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from opencollab.domain.team import Topology

# Canonical tool bundles, referenced by name (see container.TOOL_REGISTRY).
BASE_TOOL_NAMES: tuple[str, ...] = ("bash", "file_read", "file_write", "grep")
LEAD_TOOL_NAMES: tuple[str, ...] = (
    *BASE_TOOL_NAMES,
    "spawn_agent",
    "spawn_with_review",
    "message_agent",
    "team_status",
    "ask_user",
)

DEFAULT_LEAD_PROMPT = """\
You are agent 0, the primary developer. You do the work directly and can spawn
specialist agents to parallelize when it helps.

You have direct tools (`bash`, `file_read`, `file_write`, `grep`) plus agent
coordination tools:
- `spawn_agent`: spawn a specialist agent to work on an independent sub-task in
  parallel; its result is injected back to you when it completes.
- `spawn_with_review`: spawn a coding task with a mandatory Coder -> Reviewer
  loop. Use for complex or risky code changes.
- `team_status`: list the live team (agent ids, roles, phases).
- `message_agent`: send a message to an existing agent (by aid) and get its
  reply inline.

## How to work

1. **Trivial / small tasks** (typos, simple fixes, single-file edits, exploration):
   Just do them yourself with your direct tools. Don't spawn agents for these.

2. **Complex features**: decompose the request, `spawn_agent` for the independent
   steps (use `spawn_with_review` for risky code changes), and let independent
   work run in parallel. Each spawned agent works in an isolated git worktree, so
   ensure parallel agents don't modify the same files.

3. **Coordinating teammates**: use `team_status` to see the live team and
   `message_agent` to ask an existing teammate a follow-up question and get its
   reply inline. Spawned agents return summaries, not raw logs — keep your own
   context clean for high-level reasoning.

4. **Debugging stuck loops**: if a task fails repeatedly, DO NOT retry the same
   approach. Spawn a reviewer to analyze the error with fresh eyes, or ask the
   user for clarification.
"""

DEFAULT_ROLE_PROMPT = """\
You are a specialist agent. Complete the assigned task using the provided tools.
Be thorough but efficient. When done, provide a clear summary of what you did.
"""


class RoleConfig(BaseModel):
    """A single role definition: prompt + optional model override + tool names."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1)
    model: str | None = None
    tools: list[str] = Field(default_factory=list)


class _RoleFileModel(BaseModel):
    """On-disk role entry; ``prompt`` or ``prompt_file`` (resolved at load)."""

    model_config = ConfigDict(extra="ignore")

    prompt: str | None = None
    prompt_file: str | None = None
    model: str | None = None
    tools: list[str] = Field(default_factory=list)


class _TeamFileModel(BaseModel):
    """Top-level team file schema."""

    model_config = ConfigDict(extra="ignore")

    roles: dict[str, _RoleFileModel] = Field(default_factory=dict)
    topology: dict[str, list[str]] = Field(default_factory=dict)


@dataclass(frozen=True)
class TeamConfig:
    """Resolved team: role definitions + topology, with an unknown-role fallback."""

    roles: dict[str, RoleConfig] = field(default_factory=dict)
    topology: Topology = field(default_factory=Topology)

    def role_for(self, name: str) -> RoleConfig:
        """Return the declared role, or a generic fallback for ad-hoc roles."""
        return self.roles.get(name) or default_role(name)


def default_role(name: str) -> RoleConfig:
    """Generic spec for a role not declared in the team file."""
    return RoleConfig(prompt=DEFAULT_ROLE_PROMPT, model=None, tools=list(BASE_TOOL_NAMES))


def default_team_config() -> TeamConfig:
    """Lead-only team with a permissive topology (the no-file default)."""
    lead = RoleConfig(prompt=DEFAULT_LEAD_PROMPT, model=None, tools=list(LEAD_TOOL_NAMES))
    return TeamConfig(roles={"lead": lead}, topology=Topology(allow_all=True))


def _candidate_team_paths(workspace: str | None) -> list[Path]:
    explicit = os.environ.get("OPENCOLLAB_TEAM_FILE")
    if explicit:
        return [Path(explicit)]
    bases: list[Path] = []
    if workspace:
        bases.append(Path(workspace))
    bases.append(Path.cwd())
    paths: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        candidate = base / "configs" / "team.yaml"
        key = str(candidate.absolute())
        if key not in seen:
            paths.append(candidate)
            seen.add(key)
    return paths


def _resolve_prompt(entry: _RoleFileModel, base_dir: Path, role_name: str) -> str:
    if entry.prompt is not None:
        return entry.prompt
    if entry.prompt_file is not None:
        prompt_path = (base_dir / entry.prompt_file).resolve()
        if not prompt_path.is_file():
            raise ValueError(
                f"Role '{role_name}': prompt_file not found: {prompt_path}"
            )
        return prompt_path.read_text(encoding="utf-8")
    raise ValueError(f"Role '{role_name}': must set 'prompt' or 'prompt_file'.")


def _build_team_config(data: Any, base_dir: Path) -> TeamConfig:
    model = _TeamFileModel.model_validate(data or {})
    roles = {
        name: RoleConfig(
            prompt=_resolve_prompt(entry, base_dir, name),
            model=entry.model,
            tools=list(entry.tools),
        )
        for name, entry in model.roles.items()
    }
    edges = {src: frozenset(dsts) for src, dsts in model.topology.items()}
    return TeamConfig(roles=roles, topology=Topology(edges=edges, allow_all=False))


def load_team_config(workspace: str | None = None) -> TeamConfig:
    """Load the team file from the resolved path, or the lead-only default."""
    for path in _candidate_team_paths(workspace):
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return _build_team_config(data, path.resolve().parent)
    return default_team_config()


__all__ = [
    "BASE_TOOL_NAMES",
    "LEAD_TOOL_NAMES",
    "DEFAULT_LEAD_PROMPT",
    "DEFAULT_ROLE_PROMPT",
    "RoleConfig",
    "TeamConfig",
    "default_role",
    "default_team_config",
    "load_team_config",
]
