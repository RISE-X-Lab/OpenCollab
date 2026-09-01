"""Four rungs that locate which sentence in a command carries the effect.

``instructed`` reads 3/3 delegation and ``default`` reads 0/3, and the two cards
differ by more than one thing: a prohibition on doing the work oneself, a line
suspending the Analyst's discretion, and an explicit permission to depart. Which
of the three carries the effect cannot be read off those two batches, and the
answer changes the claim: that a declared topology needs the alternative
*blocked* is a different finding from that it needs an imperative.

So the four cards here are one text with three switches, and each adjacent pair
differs by exactly one of them. What is pinned is that property -- if two rungs
ever drift apart in a second place, a difference measured across them stops
naming the switch it is named for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "configs" / "handoff-experiment"
BLOCKS = PROMPTS / "blocks"
LEGACY = PROMPTS / "legacy"
#: rung order, from most to least of the alternative removed.
LADDER = ("cmd-prohibit", "cmd-plain", "cmd-bare", "cmd-optout")
PROHIBITION = "Do not apply the fix yourself."
SUSPENSION = "not asking you to decide how to divide the work"
PERMISSION = "You may depart from this and do the implementation yourself"


def _card(name: str) -> str:
    return (PROMPTS / f"analyst.{name}.md").read_text(encoding="utf-8")


def _closing(name: str) -> str:
    """The rung's block, read from the file the card is assembled from.

    This used to be recovered by splitting the card on a hardcoded heading.
    That mechanism is retired: the heading is not shared across cells -- three
    of the current blocks carry a different one -- so it silently stopped being
    a way to find a block at all.
    """
    return (BLOCKS / f"{name}.md").read_text(encoding="utf-8")


def _paragraphs(name: str) -> list[str]:
    return [p.strip() for p in _closing(name).split("\n\n") if p.strip()]


@pytest.mark.parametrize("rung", LADDER)
def test_every_rung_keeps_the_facts_v2_shared_block(rung: str) -> None:
    """The ladder varies a stance. Anything else moving makes it two variables."""
    shared = _card("facts-v2").replace(_closing("judge"), "")
    assert _card(rung).replace(_closing(rung), "") == shared, rung


@pytest.mark.parametrize("rung", LADDER)
def test_every_rung_still_commands_the_handoff(rung: str) -> None:
    """All four are commands. The ladder is about what surrounds the command."""
    card = " ".join(_card(rung).split())
    assert "send the Coder a message with `message_agent` describing the change" in card, rung
    assert "Verification is the Tester's" in card, rung


def test_only_the_top_rung_forbids_the_alternative() -> None:
    assert PROHIBITION in _card("cmd-prohibit")
    for rung in ("cmd-plain", "cmd-bare", "cmd-optout"):
        assert PROHIBITION not in _card(rung), rung


def test_the_suspension_line_is_dropped_at_the_third_rung() -> None:
    for rung in ("cmd-prohibit", "cmd-plain"):
        assert SUSPENSION in " ".join(_card(rung).split()), rung
    for rung in ("cmd-bare", "cmd-optout"):
        assert SUSPENSION not in " ".join(_card(rung).split()), rung


def test_only_the_bottom_rung_grants_permission_to_depart() -> None:
    assert PERMISSION in " ".join(_card("cmd-optout").split())
    for rung in ("cmd-prohibit", "cmd-plain", "cmd-bare"):
        assert PERMISSION not in " ".join(_card(rung).split()), rung


@pytest.mark.parametrize("lower,upper", list(zip(LADDER, LADDER[1:])))
def test_adjacent_rungs_differ_in_exactly_one_move(lower: str, upper: str) -> None:
    """One switch per step, or the ladder cannot attribute a difference.

    Paragraphs are compared as sets: deleting a sentence rewraps the paragraph it
    lived in, so a single edit shows up as one paragraph leaving and at most one
    arriving. Two paragraphs arriving means two things changed.
    """
    a, b = set(_paragraphs(lower)), set(_paragraphs(upper))
    assert len(b - a) <= 1, (lower, upper, sorted(b - a))
    assert len(a - b) <= 1, (lower, upper, sorted(a - b))
    assert a != b, (lower, upper)


@pytest.mark.parametrize("rung", LADDER)
def test_every_rung_keeps_the_evidence_rule(rung: str) -> None:
    assert _card(rung).rstrip().endswith(
        "Do not report a change as verified unless you have the evidence for it."
    ), rung


def test_the_top_rung_carries_the_instructed_cards_three_load_bearing_sentences() -> None:
    """Rung 1 is the anchor: it has to be the stance that already read 3/3.

    Its shared block is the corrected one, so it is not byte-identical to
    ``analyst.instructed.md`` -- but the three sentences that make that card a
    command have to survive verbatim, or the anchor is not the anchor.
    """
    top = " ".join(_card("cmd-prohibit").split())
    inst = " ".join((LEGACY / "analyst.instructed.md").read_text(encoding="utf-8").split())
    for fragment in (SUSPENSION, PROHIBITION, "Verification is the Tester's"):
        assert fragment in top, fragment
        assert fragment in inst, fragment


@pytest.mark.parametrize("rung", LADDER)
def test_each_rung_seats_the_baseline_team(rung: str) -> None:
    """Tools, roster and topology are the instrument; only the card varies."""
    variant = load_team_config(path=str(REPO_ROOT / "configs" / f"team.handoff.{rung}.yaml"))
    baseline = load_team_config(path=str(REPO_ROOT / "configs" / "team.handoff.experiment.yaml"))
    assert variant.entry == baseline.entry
    assert sorted(variant.roles) == sorted(baseline.roles)
    for role in ("analyst", "coder", "tester"):
        assert variant.roles[role].tools == baseline.roles[role].tools, role
    for role in ("coder", "tester"):
        assert variant.roles[role].prompt == baseline.roles[role].prompt, role
    assert variant.roles["analyst"].prompt == _card(rung)
    roles = ("analyst", "coder", "tester")
    walked = {(s, d) for s in roles for d in roles if variant.topology.allows(s, d)}
    assert walked == {(s, d) for s in roles for d in roles if s != d}
