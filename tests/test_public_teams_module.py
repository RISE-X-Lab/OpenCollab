"""The roster size a budget has to be divided by is readable from the file.

A team run's pool is sized per seat, so whoever starts one has to multiply a
per-seat figure by the number of roles the team file declares. That count was
only knowable by reading YAML or by remembering it, and remembering it is how a
three-role team gets run on a one-role pool: every seat then holds a third of
what the same agent gets working alone, and the run reads as something about
working in a team.
"""

from __future__ import annotations

from pathlib import Path

from opencollab.domain.scheduler import per_agent_cap
from opencollab.teams import declared_role_names

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_TEAM = REPO_ROOT / "configs" / "team.handoff.experiment.yaml"


def test_the_declared_roles_are_the_file_s_roles_in_order() -> None:
    assert declared_role_names(str(EXPERIMENT_TEAM)) == ("analyst", "coder", "tester")


def test_a_pool_of_n_seats_gives_each_seat_one_solo_agent_s_budget() -> None:
    solo_budget = 1_000_000
    roles = declared_role_names(str(EXPERIMENT_TEAM))

    cap = per_agent_cap(solo_budget * len(roles), len(roles))

    # This is the whole point of the count: multiply by it and a seat is worth
    # exactly what the same agent is given alone. Pass the solo figure as the
    # pool instead and the seat is worth a third of it.
    assert cap == solo_budget
    assert per_agent_cap(solo_budget, len(roles)) == solo_budget // 3
