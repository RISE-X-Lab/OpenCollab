"""Public facts about a team file that a caller needs before starting a run.

A team's budget pool is sized per seat: one seat is worth what one agent
working alone is given, so the pool a caller passes to a team run is that
figure times the number of roles the file declares (see
``domain/scheduler.PER_AGENT_BUDGET_SHARE``). The caller therefore has to know
how many roles a team file declares *before* it can say what pool to run it
with, and reading that out of the file is the only way to get it right when the
file changes.
"""

from opencollab.bootstrap.team_config import load_team_config

__all__ = ["declared_role_names", "declared_role_tools"]


def declared_role_names(path: str) -> tuple[str, ...]:
    """The role names a team file declares, the entry role first.

    This is ``N`` in the per-agent cap rule ``c * total / N``: every declared
    role is seated before the first model call under a prebuilt roster, so the
    count is a property of the file rather than of how the run turns out.
    """
    return tuple(load_team_config(path=path).roles)


def declared_role_tools(path: str) -> dict[str, tuple[str, ...]]:
    """The tool names each declared role is seated with, role name to bundle.

    Read through the same loader ``declared_role_names`` reads, and keyed by the
    same normalized role identities, so the count a budget is divided by and the
    bundles compared against another arm cannot come from two different readings
    of one file. A caller that parsed the YAML itself would key the result on the
    raw names instead, and a team whose file spells a role differently from its
    topology would then report bundles nobody is seated with.

    The names are the file's, in the file's order. Whether a name survives into
    a live seat is a separate, capability-level question -- ``ask_user`` is
    dropped when no human is at the run -- and a role that declares no tools is
    given its fallback bundle at spawn time, not here.
    """
    config = load_team_config(path=path)
    return {name: tuple(role.tools) for name, role in config.roles.items()}
