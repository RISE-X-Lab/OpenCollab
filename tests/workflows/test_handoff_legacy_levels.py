"""The shipped experimental team: three peers that can hand work over git.

``configs/team.handoff.primary-legacy.yaml`` exists to measure whether models hand
work to each other when they are able to and nothing tells them to. That makes
its *capabilities* the instrument: if a role loses ``message_agent``, or the
Tester loses ``bash``, or a return edge disappears from the topology, the run
still completes and still produces a transcript — one in which the handoff
simply never happened. The result would read as a fact about the model and be a
fact about this file.

So the properties the measurement rests on are pinned here, by name, with the
reason each one is load-bearing.
"""


from __future__ import annotations

import pytest

from opencollab.bootstrap.team_config import load_team_config
from tests.support.paths import REPO_ROOT

TEAM_FILE = REPO_ROOT / "configs" / "team.handoff.primary-legacy.yaml"


ROLES = ("analyst", "coder", "tester")


@pytest.fixture(scope="module")
def team():
    return load_team_config(path=str(TEAM_FILE))


SINGLE_AGENT_WORKING_TOOLS = frozenset(
    {"apply_patch", "bash", "file_read", "file_write", "grep", "submit"}
)


COLLABORATION_TOOLS = frozenset({"git_diff", "message_agent", "team_status"})


PROMPTS = REPO_ROOT / "configs" / "handoff-experiment"


LEGACY = PROMPTS / "legacy"


LEVELS = {
    "primary": ("analyst.md", "team.handoff.primary-legacy.yaml"),
    "weak": ("analyst.weak.md", "team.handoff.weak.yaml"),
    "strong": ("analyst.strong.md", "team.handoff.strong.yaml"),
}


JUDGE_HEADING = "## What is yours to judge"


def _role_card(role: str) -> str:
    """The card ``team.handoff.primary-legacy.yaml`` actually seats this role with."""
    path = LEGACY / "analyst.md" if role == "analyst" else PROMPTS / f"{role}.md"
    return path.read_text(encoding="utf-8")


def _analyst_card(level: str) -> str:
    return (LEGACY / LEVELS[level][0]).read_text(encoding="utf-8")


def _split_legacy_judge_block(card: str) -> tuple[str, str]:
    """Everything outside the judge section, and the judge section itself.

    Only ever applied to ``legacy/``. The current generation does not need this:
    its blocks are files under ``blocks/``, and
    ``tests/test_analyst_card_assembly.py`` compares them directly rather than
    trying to recover them from a heading. Three of the current blocks do not
    even carry this heading, so recovering them this way is not possible.
    """
    head, _, rest = card.partition(JUDGE_HEADING)
    assert rest, "the card has no judge section to vary"
    block, sep, tail = rest.partition("\n\nDo not report")
    assert sep, "the card does not end with the shared closing line"
    return head + tail, block


def test_the_three_analyst_cards_differ_only_in_the_judge_block() -> None:
    """One dimension means one block.

    If any other sentence drifted between the levels -- what the tools are, how
    a sha travels, which tree is read -- the difference between two levels would
    no longer be the thing the levels are named for, and a paired difference
    across them would be reading off two variables at once.
    """
    shared = {level: _split_legacy_judge_block(_analyst_card(level))[0] for level in LEVELS}
    assert len(set(shared.values())) == 1, {k: len(v) for k, v in shared.items()}


def test_the_three_levels_are_actually_three_different_levels() -> None:
    """Three files that happen to say the same thing are one level, not three."""
    blocks = {level: _split_legacy_judge_block(_analyst_card(level))[1] for level in LEVELS}
    assert len(set(blocks.values())) == 3, sorted(blocks)


def test_the_weak_card_keeps_the_sentence_the_first_batches_ran_under() -> None:
    """``weak`` is not a new card: it is the one those runs were produced under.

    The measured batches that showed a zero delegation rate ran on this exact
    stance. If it drifts, those runs stop belonging to any level that still
    exists and their numbers become unattributable.
    """
    weak = " ".join(_analyst_card("weak").split())
    assert "carry out this request end to end without involving anyone" in weak


def test_the_primary_card_pushes_neither_way() -> None:
    """The level every reported quantity is measured on has to be the neutral one.

    ``primary`` is ours to write, which is exactly why it is attackable: a card
    that tells the Analyst it needs no one predicts its own result, and so does
    one that tells it to delegate. What is pinned is that primary does neither
    -- it names both courses and declines to name a default.
    """
    block = " ".join(_split_legacy_judge_block(_analyst_card("primary"))[1].split()).lower()
    for leaning_alone in ("without involving anyone", "you can settle yourself"):
        assert leaning_alone not in block, leaning_alone
    for leaning_over in ("decide what to hand over before", "hand the parts", "has not used the team"):
        assert leaning_over not in block, leaning_over
    # Both courses named, neither made the expectation.
    assert "keeping all of it" in block
    assert "handing parts of it over" in block
    assert "neither one is what you are expected to do" in block


def test_the_strong_card_asks_for_the_handoff_before_the_work() -> None:
    """The level that exists to test whether a non-zero first stage is reachable."""
    block = " ".join(_split_legacy_judge_block(_analyst_card("strong"))[1].split()).lower()
    assert "decide what to hand over before you start" in block
    assert "has not used the team it was given" in block


@pytest.mark.parametrize("level", sorted(LEVELS))
def test_each_level_loads_and_seats_the_same_team(level: str) -> None:
    """Only the Analyst's card may differ; roster, tools and topology may not."""
    variant = load_team_config(path=str(REPO_ROOT / "configs" / LEVELS[level][1]))
    baseline = load_team_config(path=str(TEAM_FILE))
    assert variant.entry == baseline.entry
    assert sorted(variant.roles) == sorted(baseline.roles)
    for role in ROLES:
        assert variant.roles[role].tools == baseline.roles[role].tools, role
        if role != "analyst":
            assert variant.roles[role].prompt == baseline.roles[role].prompt, role
    assert variant.roles["analyst"].prompt == _analyst_card(level)
