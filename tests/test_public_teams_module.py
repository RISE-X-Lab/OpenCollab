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
from opencollab.teams import (
    declared_role_names,
    declared_role_prompt_digests,
    declared_role_tools,
)

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


def test_the_declared_tools_are_read_through_the_loader_a_run_uses() -> None:
    bundles = declared_role_tools(str(EXPERIMENT_TEAM))

    assert set(bundles) == set(declared_role_names(str(EXPERIMENT_TEAM)))
    # The one thing this team is meant to differ by is the collaboration
    # channel, so a caller comparing it against a solo arm has to be able to
    # see that channel in the bundle rather than take it on trust.
    assert "message_agent" in bundles["analyst"]
    # And the Tester's missing writers are a declared role boundary, not an
    # arm difference -- also only visible if the bundles are readable.
    assert "apply_patch" not in bundles["tester"]
    assert "file_write" not in bundles["tester"]


def test_the_bundles_are_keyed_by_the_same_identities_as_the_role_names(tmp_path) -> None:
    # The reason this reads through ``load_team_config`` instead of the YAML:
    # role names are normalized on the way in, so a caller that parsed the file
    # itself would key its bundles on names the run never seats -- and the two
    # readings would then disagree about which role holds what.
    team_file = tmp_path / "team.yaml"
    team_file.write_text(
        "entry: Solo\nroles:\n  Solo:\n    prompt: do the work\n"
        "    tools: [file_read]\n",
        encoding="utf-8",
    )

    bundles = declared_role_tools(str(team_file))

    assert tuple(bundles) == declared_role_names(str(team_file))
    assert bundles[declared_role_names(str(team_file))[0]] == ("file_read",)


def test_the_prompt_digest_is_of_the_card_text_not_of_its_path(tmp_path) -> None:
    # Why this is a digest of the resolved text: the handoff experiment's
    # treatment IS the wording of the analyst card, so the grouping key a
    # metrics row carries has to be the wording. Two cards that differ move
    # the digest even when the team file names the same path.
    card = tmp_path / "analyst.md"
    team_file = tmp_path / "team.yaml"
    team_file.write_text(
        "entry: analyst\nroles:\n  analyst:\n    prompt_file: analyst.md\n",
        encoding="utf-8",
    )

    card.write_text("decide for yourself", encoding="utf-8")
    first = declared_role_prompt_digests(str(team_file))["analyst"]
    card.write_text("hand the work over", encoding="utf-8")
    second = declared_role_prompt_digests(str(team_file))["analyst"]

    assert first != second


def test_the_two_generations_of_the_analyst_card_do_not_pool() -> None:
    """The failure this field exists to prevent.

    ``legacy/analyst.md`` and the assembled ``analyst.facts-v2.md`` carry the
    same stance block on two different shared bodies. Runs of the two are not
    poolable, and a metrics row that recorded only the arm name -- or only the
    path, which was repointed when the first generation moved to ``legacy/`` --
    could not tell them apart afterwards.
    """
    first_generation = declared_role_prompt_digests(str(EXPERIMENT_TEAM))["analyst"]
    second_generation = declared_role_prompt_digests(
        str(REPO_ROOT / "configs" / "team.handoff.facts-v2.yaml")
    )["analyst"]

    assert first_generation != second_generation


def test_roles_that_share_a_card_share_a_digest() -> None:
    """The Coder and Tester are held fixed across cells, so they must not move."""
    primary = declared_role_prompt_digests(str(EXPERIMENT_TEAM))
    facts_v2 = declared_role_prompt_digests(
        str(REPO_ROOT / "configs" / "team.handoff.facts-v2.yaml")
    )

    assert primary["coder"] == facts_v2["coder"]
    assert primary["tester"] == facts_v2["tester"]
