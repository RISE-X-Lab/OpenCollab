"""The rewritten shared blocks of the Analyst card, as a second axis.

``configs/team.handoff.facts-v2.yaml`` exists because the primary card's shared
text carried an untrue sentence and a one-sided account of what a handoff costs.
It is not a fourth rung of the delegation-emphasis ladder: it keeps ``primary``'s
``## What is yours to judge`` block byte for byte and changes what surrounds it,
which is the opposite of what the ladder varies.

What is pinned here is that the two axes stay separate, and that the sentence
this card was written to correct does not come back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "configs" / "handoff-experiment"
JUDGE_HEADING = "## What is yours to judge"
ROLES = ("analyst", "coder", "tester")


def _card(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _split(card: str) -> tuple[str, str]:
    head, _, judge = card.partition(JUDGE_HEADING)
    assert judge, "the card has no judge section"
    return head, judge


@pytest.fixture(scope="module")
def variant():
    return load_team_config(path=str(REPO_ROOT / "configs" / "team.handoff.facts-v2.yaml"))


@pytest.fixture(scope="module")
def baseline():
    return load_team_config(path=str(REPO_ROOT / "configs" / "team.handoff.experiment.yaml"))


def test_the_stance_block_is_primarys_byte_for_byte() -> None:
    """The one thing this card must not vary is the thing the ladder varies.

    If the judge block drifted, a difference measured between this card and
    primary would be reading the rewrite and a stance change at once.
    """
    assert _split(_card("analyst.facts-v2.md"))[1] == _split(_card("analyst.md"))[1]


def test_everything_outside_the_stance_block_did_change() -> None:
    """A card that says what primary says is primary, not a second axis."""
    assert _split(_card("analyst.facts-v2.md"))[0] != _split(_card("analyst.md"))[0]


def test_the_card_does_not_claim_the_tester_holds_the_analysts_tools(variant) -> None:
    """The sentence this card was written to correct.

    The Tester carries no ``apply_patch`` and no ``file_write`` -- a declared
    role boundary, stated in its own card. Primary told the Analyst all three
    held the same working tools, so the Analyst was reasoning about its team
    from a false premise.
    """
    tester = variant.roles["tester"].tools
    assert "apply_patch" not in tester
    assert "file_write" not in tester
    card = " ".join(_card("analyst.facts-v2.md").split())
    assert "The Coder and the Tester hold the same working tools you do" not in card
    assert "the Tester holds the same minus the two that edit files" in card


def test_the_card_still_says_the_coder_can_do_the_analysts_work(variant) -> None:
    """The true half of the corrected sentence has to survive the correction.

    The Coder does hold the Analyst's bundle. Dropping that would make the card
    understate the team rather than describe it.
    """
    assert variant.roles["coder"].tools == variant.roles["analyst"].tools
    card = " ".join(_card("analyst.facts-v2.md").split())
    assert "The Coder holds the same tools you do" in card


def test_the_card_tells_the_analyst_the_seat_budget_is_not_transferable() -> None:
    """Primary gave the budget only as a negated cost, with no magnitude.

    A seat's cap is ``pool / 3``, which is the single agent's budget. Both
    halves -- that it is the size of the Analyst's, and that an unspent share
    does not come back to the Analyst -- are what make the resource legible.
    """
    card = " ".join(_card("analyst.facts-v2.md").split())
    assert "a budget the size of yours" in card
    assert "what they do not spend is not returned to you" in card


def test_the_rewrite_is_not_an_instruction_to_hand_work_over() -> None:
    """The axis is how the facts are stated, not what the Analyst is told to do."""
    card = _card("analyst.facts-v2.md").lower()
    for word in ("delegate", "you must", "do not apply the fix yourself"):
        assert word not in card, word


def test_the_variant_changes_nothing_but_the_analysts_card(variant, baseline) -> None:
    """Tools, roster and topology are the instrument; only the prompt varies."""
    assert variant.entry == baseline.entry
    assert sorted(variant.roles) == sorted(baseline.roles)
    for role in ROLES:
        assert variant.roles[role].tools == baseline.roles[role].tools, role
        if role != "analyst":
            assert variant.roles[role].prompt == baseline.roles[role].prompt, role
    walked = {
        (s, d) for s in ROLES for d in ROLES if variant.topology.allows(s, d)
    }
    assert walked == {(s, d) for s in ROLES for d in ROLES if s != d}


def test_the_card_is_shorter_than_the_one_it_replaces() -> None:
    """Same facts in fewer words was the brief, and it is checkable."""
    assert len(_card("analyst.facts-v2.md")) < len(_card("analyst.md"))
