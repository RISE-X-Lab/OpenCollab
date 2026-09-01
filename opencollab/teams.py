"""Public facts about a team file that a caller needs before starting a run.

A team's budget pool is sized per seat: one seat is worth what one agent
working alone is given, so the pool a caller passes to a team run is that
figure times the number of roles the file declares (see
``domain/scheduler.PER_AGENT_BUDGET_SHARE``). The caller therefore has to know
how many roles a team file declares *before* it can say what pool to run it
with, and reading that out of the file is the only way to get it right when the
file changes.
"""

import hashlib

from opencollab.bootstrap.team_config import load_team_config

__all__ = [
    "declared_role_names",
    "declared_role_prompt_digests",
    "declared_role_tools",
]


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


def declared_role_prompt_digests(path: str) -> dict[str, str]:
    """Each declared role's system prompt as a sha256, role name to digest.

    The digest is of the prompt text the run actually seats, after
    ``prompt_file`` has been read -- so a caller recording it is recording the
    card, not a path that may be repointed later. That distinction is the whole
    reason this exists: an experiment whose treatment IS the wording of a role
    card needs the wording itself as the grouping key. A run recorded by path
    cannot be told apart from a run of a differently worded card that was moved
    to the same path, and two generations of a card that share a name pool
    silently into one condition.

    Prompts can be long and are not secrets, but a digest is what a metrics row
    should carry: it is fixed width, it compares exactly, and it says nothing
    about the card beyond identity, which is all a grouping key may claim.
    """
    config = load_team_config(path=path)
    return {
        name: hashlib.sha256(role.prompt.encode("utf-8")).hexdigest()
        for name, role in config.roles.items()
    }
