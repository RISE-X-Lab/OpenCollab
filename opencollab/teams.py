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

__all__ = ["declared_role_names"]


def declared_role_names(path: str) -> tuple[str, ...]:
    """The role names a team file declares, the entry role first.

    This is ``N`` in the per-agent cap rule ``c * total / N``: every declared
    role is seated before the first model call under a prebuilt roster, so the
    count is a property of the file rather than of how the run turns out.
    """
    return tuple(load_team_config(path=path).roles)
