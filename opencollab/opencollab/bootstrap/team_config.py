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

from opencollab.bootstrap.tool_registry import COORDINATION_TOOL_NAMES, KNOWN_TOOL_NAMES
from opencollab.domain.hooks import HOOK_ACTION_TYPES, HOOK_EVENT_NAMES, HookSpec
from opencollab.domain.team import Topology

# Default tool bundles, derived from the registry so it stays the single source
# of truth — add a tool there and the lead picks it up, no hand-maintained list
# to drift. The lead (agent 0) gets every registered tool; an ad-hoc specialist
# gets work tools only: no coordination (it must not fan out further) and no
# skill dispatch. ``ask_user`` stays in the base set but is moot for spawned
# specialists — they are built non-interactive, so the registry resolver drops it
# for them regardless. Sorted for a deterministic, reproducible tool order.
LEAD_TOOL_NAMES: tuple[str, ...] = tuple(sorted(KNOWN_TOOL_NAMES))
BASE_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(KNOWN_TOOL_NAMES - COORDINATION_TOOL_NAMES - {"use_skill"})
)

DEFAULT_LEAD_PROMPT = """\
You are OpenCollab, agent 0 — the primary developer. You do the work directly
and can spawn specialist agents to parallelize when it helps. Your available
tools, and any skills you can load on demand, are described in your tool schemas
and context — this prompt covers only how to use them well.

## How to work

1. **Trivial / small tasks** (typos, simple fixes, single-file edits,
   exploration): just do them yourself. Don't spawn agents for these.

2. **Complex features**: decompose the request, `spawn_agent` for each
   independent step (use `spawn_with_review` for risky code changes), and let
   independent work run in parallel. Each spawned agent works in an isolated git
   worktree, so ensure parallel agents don't modify the same files.

3. **Coordinating teammates**: use `team_status` to see the live team and
   `message_agent` to send an existing teammate an async follow-up — don't wait
   for an inline reply; they may reply later by messaging you. Spawned agents
   return summaries, not raw logs, so keep your own context clean for high-level
   reasoning.

4. **Debugging stuck loops**: if a task fails repeatedly, DO NOT retry the same
   approach. Spawn a reviewer to analyze the error with fresh eyes, or ask the
   user for clarification.

5. **Reading files**: work in narrow ranges — `grep` to locate the relevant
   lines and `file_read` with an offset/limit instead of dumping whole large
   files. Oversized tool output is truncated and wastes context.
"""

DEFAULT_ROLE_PROMPT = """\
You are an OpenCollab specialist agent. Complete the assigned task using the
provided tools. Be thorough but efficient. When done, provide a clear summary of
what you did.

When reading files, work in narrow ranges: prefer `grep` to locate the relevant
lines and `file_read` with an offset/limit, rather than dumping whole large
files — oversized tool output is truncated and wastes context.
"""


class RoleConfig(BaseModel):
    """A single role definition: prompt + optional model override + tool names."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(min_length=1)
    model: str | None = None
    # Optional per-role sampling-temperature override. ``None`` falls back to the
    # global ``OpenCollabConfig.temperature`` (resolved in ``ContextBuilder``).
    temperature: float | None = None
    tools: list[str] = Field(default_factory=list)


class _RoleFileModel(BaseModel):
    """On-disk role entry; ``prompt`` or ``prompt_file`` (resolved at load)."""

    model_config = ConfigDict(extra="ignore")

    prompt: str | None = None
    prompt_file: str | None = None
    model: str | None = None
    temperature: float | None = None
    tools: list[str] = Field(default_factory=list)


class _HookActionFileModel(BaseModel):
    """On-disk hook entry: one action bound to a lifecycle event."""

    model_config = ConfigDict(extra="ignore")

    command: str = Field(min_length=1)
    matcher: str | None = None
    type: str = "command"
    timeout: float = 30.0


class _TeamFileModel(BaseModel):
    """Top-level team file schema."""

    model_config = ConfigDict(extra="ignore")

    roles: dict[str, _RoleFileModel] = Field(default_factory=dict)
    topology: dict[str, list[str]] = Field(default_factory=dict)
    hooks: dict[str, list[_HookActionFileModel]] = Field(default_factory=dict)
    # Which role is agent 0 (the root that receives the user task and spawns the
    # rest). Optional; see ``_resolve_entry_role`` for the fallback order.
    entry: str | None = None
    # Per-tool output-cap overrides, e.g. {"bash": {"max_output_chars": 6000}}.
    # Keys are tool names; values are constructor kwargs for that tool. Lets a
    # team tune output budgets to its backend's context size (see
    # ``bootstrap.tool_registry.build_tools_for_role``).
    tool_limits: dict[str, dict[str, int]] = Field(default_factory=dict)


@dataclass(frozen=True)
class TeamConfig:
    """Resolved team: role definitions + topology, with an unknown-role fallback.

    ``entry`` is the role the scheduler builds as agent 0 (aid=0) — the root that
    receives the user task and spawns the rest on demand.
    """

    roles: dict[str, RoleConfig] = field(default_factory=dict)
    topology: Topology = field(default_factory=Topology)
    hooks: tuple[HookSpec, ...] = ()
    entry: str = "lead"
    # Tool name -> constructor kwargs (output caps); applied by the registry.
    tool_limits: dict[str, dict[str, int]] = field(default_factory=dict)

    def role_for(self, name: str) -> RoleConfig:
        """Return the declared role, or a generic fallback for ad-hoc roles."""
        return self.roles.get(name) or default_role(name)


def default_role(name: str) -> RoleConfig:
    """Generic spec for a role not declared in the team file."""
    return RoleConfig(prompt=DEFAULT_ROLE_PROMPT, model=None, tools=list(BASE_TOOL_NAMES))


def default_team_config() -> TeamConfig:
    """Lead-only team with a permissive topology (the no-file default)."""
    lead = RoleConfig(prompt=DEFAULT_LEAD_PROMPT, model=None, tools=list(LEAD_TOOL_NAMES))
    return TeamConfig(roles={"lead": lead}, topology=Topology(allow_all=True), entry="lead")


def _resolve_entry_role(explicit: str | None, roles: dict[str, RoleConfig]) -> str:
    """Pick the entry role (agent 0).

    Order: an explicit ``entry:`` field > a role literally named ``lead``
    (backward compatibility with the old hardcoded default) > the first declared
    role. An explicit ``entry:`` that names no declared role fails fast.
    """
    if explicit is not None:
        if explicit not in roles:
            raise ValueError(
                f"entry role '{explicit}' is not declared in roles "
                f"({sorted(roles)})."
            )
        return explicit
    if "lead" in roles:
        return "lead"
    if roles:
        return next(iter(roles))
    return "lead"


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


def _build_hook_specs(hooks: dict[str, list[_HookActionFileModel]]) -> tuple[HookSpec, ...]:
    specs: list[HookSpec] = []
    for event_name, actions in hooks.items():
        if event_name not in HOOK_EVENT_NAMES:
            raise ValueError(
                f"Unknown hook event '{event_name}'. "
                f"Known events: {sorted(HOOK_EVENT_NAMES)}"
            )
        for action in actions:
            if action.type not in HOOK_ACTION_TYPES:
                raise ValueError(
                    f"Unknown hook action type '{action.type}' for event "
                    f"'{event_name}'. Known types: {sorted(HOOK_ACTION_TYPES)}"
                )
            specs.append(
                HookSpec(
                    event=event_name,
                    action_type=action.type,
                    command=action.command,
                    matcher=action.matcher,
                    timeout=action.timeout,
                )
            )
    return tuple(specs)


def _build_team_config(data: Any, base_dir: Path) -> TeamConfig:
    model = _TeamFileModel.model_validate(data or {})
    roles = {
        name: RoleConfig(
            prompt=_resolve_prompt(entry, base_dir, name),
            model=entry.model,
            temperature=entry.temperature,
            tools=list(entry.tools),
        )
        for name, entry in model.roles.items()
    }
    edges = {src: frozenset(dsts) for src, dsts in model.topology.items()}
    return TeamConfig(
        roles=roles,
        topology=Topology(edges=edges, allow_all=False),
        hooks=_build_hook_specs(model.hooks),
        entry=_resolve_entry_role(model.entry, roles),
        tool_limits={name: dict(kwargs) for name, kwargs in model.tool_limits.items()},
    )


def resolve_team_file(workspace: str | None = None) -> Path | None:
    """Return the team file that ``load_team_config`` would read, or ``None``."""
    for path in _candidate_team_paths(workspace):
        if path.is_file():
            return path.resolve()
    return None


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
