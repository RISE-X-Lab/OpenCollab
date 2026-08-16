"""Team configuration — YAML-backed role + topology definitions.

A team file declares the agents that make up a collaboration: each role's
system prompt, optional model override, and the names of the tools it may use,
plus a directed ``topology`` of which role may spawn/message which other role.

A team file is loaded only when the caller passes ``path=`` or sets the explicit
``OPENCOLLAB_TEAM_FILE`` environment variable. Otherwise,
``default_team_config`` returns the built-in Self-Collaboration team: an
``analyst`` entry that plans and delegates, a ``coder`` that implements, and a
``tester`` that verifies, over a *closed* topology. A declared team file may
still open its topology with ``allow_all``, in which case undeclared roles fall
back to ``default_role``. Conventional filenames such as ``configs/team.yaml``
are never discovered implicitly.

Tool *names* are resolved to concrete Tool instances by ``ContextBuilder`` in
``bootstrap.container``; this module only carries the names.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from opencollab.adapters.safe_files import read_regular_text
from opencollab.bootstrap.tool_registry import (
    COORDINATION_TOOL_NAMES,
    KNOWN_TOOL_NAMES,
    validate_tool_limits,
)
from opencollab.domain.hooks import (
    EXECUTABLE_HOOK_ACTION_TYPES,
    HOOK_ACTION_TYPES,
    HOOK_EVENT_NAMES,
    HookSpec,
)
from opencollab.domain.identity import role_collision_key, validate_role_identity
from opencollab.domain.team import Topology

# Built-in Self-Collaboration tool bundles. The division of labour is enforced
# by capability, not by asking a role nicely in its prompt: the Analyst has no
# tool that writes, the Tester has no tool that writes, and neither the Coder
# nor the Tester carries a coordination tool, so they cannot fan the work out
# further. ``ask_user`` is the Analyst's alone and is moot for the other two —
# they are built non-interactive, so the registry resolver drops it regardless.
# Sorted for a deterministic, reproducible tool order.
ANALYST_TOOL_NAMES: tuple[str, ...] = ("ask_user", "file_read", "grep", "spawn_agent", "use_skill")
CODER_TOOL_NAMES: tuple[str, ...] = ("apply_patch", "bash", "file_read", "grep", "run_tests")
TESTER_TOOL_NAMES: tuple[str, ...] = ("file_read", "git_diff", "grep", "run_tests")

# Fallback bundle for a role an ``allow_all`` team file spawns without declaring
# it. Derived from the registry so it stays the single source of truth — add a
# tool there and the fallback picks it up, no hand-maintained list to drift. It
# is work tools only: no coordination (it must not fan out further) and no skill
# dispatch.
BASE_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(KNOWN_TOOL_NAMES - COORDINATION_TOOL_NAMES - {"use_skill"})
)

# The three bundles above are hand-written subsets, so a rename in the registry
# would silently leave a role short a tool. Fail at import instead.
for _role_bundle in (ANALYST_TOOL_NAMES, CODER_TOOL_NAMES, TESTER_TOOL_NAMES):
    _unknown = sorted(set(_role_bundle) - KNOWN_TOOL_NAMES)
    if _unknown:
        raise RuntimeError(
            f"built-in team declares tools missing from the registry: {_unknown}"
        )

# Built-in default prompts live as data files next to this module (``prompts/``)
# so they read as prose, not Python string literals, and ship with the package —
# loading regardless of cwd or install layout. A team file can still override any
# role's prompt via ``prompt`` / ``prompt_file`` (see ``_resolve_prompt``).
_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
MAX_TEAM_CONFIG_BYTES = 4 * 1024 * 1024
MAX_ROLE_PROMPT_BYTES = 4 * 1024 * 1024


def _validate_thinking_params(
    value: dict[Any, Any] | None,
) -> dict[Any, Any] | None:
    if value is None:
        return None
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("thinking_params must be JSON-serializable") from exc
    return value


def _load_default_prompt(filename: str) -> str:
    return (_PROMPT_DIR / filename).read_text(encoding="utf-8")


DEFAULT_ANALYST_PROMPT = _load_default_prompt("analyst.md")
DEFAULT_CODER_PROMPT = _load_default_prompt("coder.md")
DEFAULT_TESTER_PROMPT = _load_default_prompt("tester.md")
DEFAULT_ROLE_PROMPT = _load_default_prompt("role.md")


class RoleConfig(BaseModel):
    """A single role definition: prompt + optional model override + tool names."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    model: str | None = None
    # Optional per-role sampling-temperature override. ``None`` falls back to the
    # global ``OpenCollabConfig.temperature`` (resolved in ``ContextBuilder``).
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        allow_inf_nan=False,
    )
    # Optional per-role thinking overrides. ``None`` falls back to the global
    # ``OpenCollabConfig`` values (resolved in ``ContextBuilder``), mirroring how
    # ``temperature`` is resolved.
    thinking: bool | None = None
    thinking_params: dict | None = None
    tools: list[str] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def _reject_blank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("role prompt must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def _normalize_optional_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("role model must not be blank")
        return normalized

    @field_validator("thinking_params")
    @classmethod
    def _validate_provider_parameters(
        cls,
        value: dict[Any, Any] | None,
    ) -> dict[Any, Any] | None:
        return _validate_thinking_params(value)

    @field_validator("temperature", mode="before")
    @classmethod
    def _reject_boolean_temperature(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("role temperature must not be a boolean")
        return value

class _RoleFileModel(BaseModel):
    """On-disk role entry; ``prompt`` or ``prompt_file`` (resolved at load)."""

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    model: str | None = None
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        allow_inf_nan=False,
    )
    thinking: bool | None = None
    thinking_params: dict | None = None
    tools: list[str] = Field(default_factory=list)

    @field_validator("prompt")
    @classmethod
    def _reject_blank_prompt(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("role prompt must not be blank")
        return value

    @field_validator("model")
    @classmethod
    def _normalize_optional_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("role model must not be blank")
        return normalized

    @field_validator("thinking_params")
    @classmethod
    def _validate_provider_parameters(
        cls,
        value: dict[Any, Any] | None,
    ) -> dict[Any, Any] | None:
        return _validate_thinking_params(value)

    @field_validator("temperature", mode="before")
    @classmethod
    def _reject_boolean_temperature(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("role temperature must not be a boolean")
        return value

class _HookActionFileModel(BaseModel):
    """On-disk hook entry: one action bound to a lifecycle event."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    matcher: str | None = None
    type: str = "command"
    timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)

    @field_validator("timeout", mode="before")
    @classmethod
    def _reject_boolean_timeout(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("hook timeout must not be a boolean")
        return value


class _TeamFileModel(BaseModel):
    """Top-level team file schema."""

    model_config = ConfigDict(extra="forbid")

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

    @field_validator("tool_limits", mode="before")
    @classmethod
    def _validate_tool_limits(cls, value: object) -> dict[str, dict[str, int]]:
        return validate_tool_limits(value)


@dataclass(frozen=True)
class TeamConfig:
    """Resolved team: role definitions + topology, with an unknown-role fallback.

    ``entry`` is the role the scheduler builds as agent 0 (aid=0) — the root that
    receives the user task and spawns the rest on demand.
    """

    roles: dict[str, RoleConfig] = field(default_factory=dict)
    topology: Topology = field(default_factory=Topology)
    hooks: tuple[HookSpec, ...] = ()
    entry: str | None = None
    # Tool name -> constructor kwargs (output caps); applied by the registry.
    tool_limits: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_tool_limits = validate_tool_limits(self.tool_limits)
        normalized_roles: dict[str, RoleConfig] = {}
        role_names: dict[str, str] = {}
        for raw_name, config in self.roles.items():
            name = validate_role_identity(raw_name)
            collision = role_collision_key(name)
            if collision in role_names:
                raise ValueError(
                    "role identities collide after Unicode/case normalization: "
                    f"{role_names[collision]!r} and {raw_name!r}"
                )
            role_names[collision] = name
            normalized_roles[name] = config

        if self.entry is None:
            entry = role_names.get("lead")
            if entry is None:
                entry = next(iter(normalized_roles), "lead")
        else:
            entry = validate_role_identity(self.entry)
            if normalized_roles:
                canonical_entry = role_names.get(role_collision_key(entry))
                if canonical_entry is None:
                    raise ValueError(
                        f"entry role '{entry}' is not declared in roles "
                        f"({sorted(normalized_roles)})."
                    )
                entry = canonical_entry

        canonical_edges: dict[str, frozenset[str]] = {}
        canonical_edge_sources: dict[str, str] = {}
        for source, destinations in self.topology.edges.items():
            source_identity = validate_role_identity(source)
            source_key = role_collision_key(source_identity)
            if source_key in canonical_edge_sources:
                raise ValueError(
                    "topology source identities collide after normalization: "
                    f"{canonical_edge_sources[source_key]!r} and {source!r}"
                )
            canonical_edge_sources[source_key] = source_identity
            canonical_source = role_names.get(
                source_key,
                source_identity,
            )
            canonical_destinations = frozenset(
                role_names.get(
                    role_collision_key(destination),
                    validate_role_identity(destination),
                )
                for destination in destinations
            )
            canonical_edges[canonical_source] = canonical_destinations

        object.__setattr__(self, "roles", normalized_roles)
        object.__setattr__(self, "entry", entry)
        object.__setattr__(self, "tool_limits", normalized_tool_limits)
        object.__setattr__(
            self,
            "topology",
            Topology(edges=canonical_edges, allow_all=self.topology.allow_all),
        )

    def role_for(self, name: str) -> RoleConfig:
        """Return the declared role, or a generic fallback for ad-hoc roles."""
        name = validate_role_identity(name)
        key = role_collision_key(name)
        for role, config in self.roles.items():
            if role_collision_key(role) == key:
                return config
        return default_role(name)


def default_role(name: str) -> RoleConfig:
    """Generic spec for a role not declared in the team file."""
    validate_role_identity(name)
    return RoleConfig(prompt=DEFAULT_ROLE_PROMPT, model=None, tools=list(BASE_TOOL_NAMES))


def default_team_config() -> TeamConfig:
    """The built-in Self-Collaboration team (the no-file default).

    Analyst plans and delegates, Coder implements, Tester verifies — the role
    split of Dong et al., *Self-Collaboration Code Generation via ChatGPT*. The
    topology is closed on purpose: only the Analyst may delegate, only to these
    two roles, and neither of them may delegate onward.
    """
    return TeamConfig(
        roles={
            "analyst": RoleConfig(
                prompt=DEFAULT_ANALYST_PROMPT,
                model=None,
                tools=list(ANALYST_TOOL_NAMES),
            ),
            "coder": RoleConfig(
                prompt=DEFAULT_CODER_PROMPT,
                model=None,
                tools=list(CODER_TOOL_NAMES),
            ),
            "tester": RoleConfig(
                prompt=DEFAULT_TESTER_PROMPT,
                model=None,
                tools=list(TESTER_TOOL_NAMES),
            ),
        },
        topology=Topology(
            edges={
                "analyst": frozenset({"coder", "tester"}),
                "coder": frozenset(),
                "tester": frozenset(),
            },
            allow_all=False,
        ),
        entry="analyst",
    )


def _resolve_entry_role(explicit: str | None, roles: dict[str, RoleConfig]) -> str:
    """Pick the entry role (agent 0).

    Order: an explicit ``entry:`` field > a role literally named ``lead``
    (backward compatibility with the old hardcoded default) > the first declared
    role. An explicit ``entry:`` that names no declared role fails fast.
    """
    if explicit is not None:
        explicit = validate_role_identity(explicit)
        canonical = {
            role_collision_key(role): role
            for role in roles
        }.get(role_collision_key(explicit))
        if canonical is None:
            raise ValueError(
                f"entry role '{explicit}' is not declared in roles "
                f"({sorted(roles)})."
            )
        return canonical
    canonical_roles = {
        role_collision_key(role): role
        for role in roles
    }
    lead = canonical_roles.get(role_collision_key("lead"))
    if lead is not None:
        return lead
    if roles:
        return next(iter(roles))
    return "lead"


def _configured_team_path(
    path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Return the explicitly selected team path, if any."""
    if path is not None:
        return Path(path)
    explicit = os.environ.get("OPENCOLLAB_TEAM_FILE")
    return Path(explicit) if explicit else None


def _resolve_prompt(entry: _RoleFileModel, base_dir: Path, role_name: str) -> str:
    if entry.prompt is not None:
        return entry.prompt
    if entry.prompt_file is not None:
        prompt_path = Path(os.path.abspath(base_dir / entry.prompt_file))
        base_absolute = Path(os.path.abspath(base_dir))
        try:
            contained = os.path.commonpath((base_absolute, prompt_path)) == str(
                base_absolute
            )
        except ValueError:
            contained = False
        if not contained:
            raise ValueError(
                f"Role '{role_name}': prompt_file escapes team directory: "
                f"{prompt_path}"
            )
        try:
            text = read_regular_text(
                prompt_path,
                max_bytes=MAX_ROLE_PROMPT_BYTES,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(
                f"Role '{role_name}': prompt_file cannot be read safely: "
                f"{prompt_path}"
            ) from exc
        return text
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
            if action.type not in EXECUTABLE_HOOK_ACTION_TYPES:
                raise ValueError(
                    f"Hook action type '{action.type}' for event '{event_name}' "
                    "is recognized but not implemented"
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
    roles: dict[str, RoleConfig] = {}
    canonical_roles: dict[str, str] = {}
    for raw_name, entry in model.roles.items():
        name = validate_role_identity(raw_name)
        collision = role_collision_key(name)
        if collision in canonical_roles:
            raise ValueError(
                "role identities collide after Unicode/case normalization: "
                f"{canonical_roles[collision]!r} and {raw_name!r}"
            )
        canonical_roles[collision] = name
        roles[name] = RoleConfig(
            prompt=_resolve_prompt(entry, base_dir, name),
            model=entry.model,
            temperature=entry.temperature,
            thinking=entry.thinking,
            thinking_params=entry.thinking_params,
            tools=list(entry.tools),
        )

    edges: dict[str, frozenset[str]] = {}
    edge_sources: dict[str, str] = {}
    for raw_source, raw_destinations in model.topology.items():
        source_identity = validate_role_identity(raw_source)
        source_key = role_collision_key(source_identity)
        if source_key in edge_sources:
            raise ValueError(
                "topology source identities collide after normalization: "
                f"{edge_sources[source_key]!r} and {raw_source!r}"
            )
        edge_sources[source_key] = source_identity
        source = canonical_roles.get(source_key, source_identity)
        destinations: list[str] = []
        destination_keys: set[str] = set()
        for raw_destination in raw_destinations:
            destination_identity = validate_role_identity(raw_destination)
            destination_key = role_collision_key(destination_identity)
            if destination_key in destination_keys:
                raise ValueError(
                    "topology destination identities collide after normalization "
                    f"for source {raw_source!r}"
                )
            destination_keys.add(destination_key)
            destinations.append(
                canonical_roles.get(destination_key, destination_identity)
            )
        edges[source] = frozenset(destinations)
    return TeamConfig(
        roles=roles,
        topology=Topology(edges=edges, allow_all=False),
        hooks=_build_hook_specs(model.hooks),
        entry=_resolve_entry_role(model.entry, roles),
        tool_limits={name: dict(kwargs) for name, kwargs in model.tool_limits.items()},
    )


def resolve_team_file(workspace: str | None = None) -> Path | None:
    """Return the explicitly configured environment team file, or ``None``.

    ``workspace`` remains accepted for compatibility but is intentionally not
    searched for conventional filenames.
    """
    del workspace
    candidate = _configured_team_path()
    if candidate is None:
        return None
    try:
        inspected = candidate.lstat()
    except OSError:
        return None
    if stat.S_ISREG(inspected.st_mode):
        return Path(os.path.abspath(candidate))
    return None


def load_team_config(
    workspace: str | None = None,
    *,
    path: str | os.PathLike[str] | None = None,
) -> TeamConfig:
    """Load an explicitly selected team file or the built-in Self-Collaboration team.

    ``workspace`` remains accepted for compatibility but is intentionally not
    searched for conventional filenames. An explicit ``path`` takes precedence
    over ``OPENCOLLAB_TEAM_FILE``.
    """
    del workspace
    candidate = _configured_team_path(path)
    if candidate is None:
        return default_team_config()
    try:
        inspected = candidate.lstat()
    except FileNotFoundError:
        raise ValueError(f"team config does not exist: {candidate}") from None
    if not stat.S_ISREG(inspected.st_mode):
        raise ValueError(f"team config is not a regular file: {candidate}")
    try:
        text = read_regular_text(candidate, max_bytes=MAX_TEAM_CONFIG_BYTES)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"team config cannot be read safely: {candidate}") from exc
    data = yaml.safe_load(text)
    return _build_team_config(data, Path(os.path.abspath(candidate)).parent)


__all__ = [
    "ANALYST_TOOL_NAMES",
    "BASE_TOOL_NAMES",
    "CODER_TOOL_NAMES",
    "TESTER_TOOL_NAMES",
    "DEFAULT_ANALYST_PROMPT",
    "DEFAULT_CODER_PROMPT",
    "DEFAULT_ROLE_PROMPT",
    "DEFAULT_TESTER_PROMPT",
    "RoleConfig",
    "TeamConfig",
    "default_role",
    "default_team_config",
    "load_team_config",
]
