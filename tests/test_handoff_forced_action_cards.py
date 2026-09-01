"""The forced-action axis: three cells between "state a fact" and "give an order".

Five Analyst cards that only restate facts -- capability (``primary``), benefit
(``salience``), norm (``norm``), default-with-an-opt-out (``default``), and the
three-level stance ladder (``weak``/``primary``/``strong``) -- all measured
``message_agent`` at zero. The one card that did not, ``analyst.instructed.md``,
does not restate anything: it blocks the action the model would otherwise take
("Do not apply the fix yourself"). The difference between the two groups is not
how clearly the option was described but whether an action was compelled, so
these three cells vary exactly that and nothing else:

``decide-first``
    A stated choice is compelled; neither branch of it is.
``opt-out-message``
    One ``message_agent`` call is compelled; which of two messages it carries
    is not. This is the cell that removes the asymmetry whereby doing nothing
    and doing the work alone are the same action.
``role-identity``
    Nothing is compelled. The role is framed as an identity, with the tools
    stated to be in hand and their use stated to break no rule.

What is pinned here is that the cells differ from ``facts-v2`` in their closing
section and in nothing else, that no cell smuggles in the order that
``instructed`` gives, and that all three seat the same team.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "handoff-experiment"
BLOCKS = PROMPTS / "blocks"
BASE_CARD = "analyst.facts-v2.md"
#: cell -> the block file its card is assembled from. ``facts-v2`` carries the
#: ``judge`` block, which is what these three replace.
BLOCK_OF = {
    "decide-first": "decide-first",
    "opt-out-message": "opt-out-message",
    "role-identity": "role-identity",
    BASE_CARD: "judge",
}
CLOSING_LINE = "Do not report a change as verified unless you have the evidence for it."
ROLES = ("analyst", "coder", "tester")

#: cell -> (Analyst card, team file). Each is its own cell: runs produced under
#: two of them are not poolable, which is why they are named separately here
#: rather than treated as levels of one ladder.
CELLS = {
    "decide-first": ("analyst.decide-first.md", "team.handoff.decide-first.yaml"),
    "opt-out-message": ("analyst.opt-out-message.md", "team.handoff.opt-out-message.yaml"),
    "role-identity": ("analyst.role-identity.md", "team.handoff.role-identity.yaml"),
}

#: The instruction the one card that reached a non-zero delegation rate gives.
#: No cell on this axis may contain it: the axis is how much of an action is
#: compelled, and naming delegation as the outcome would collapse all three
#: onto ``instructed``.
DELEGATION_ORDERS = ("delegate", "you must", "do not apply the fix yourself")


def _card(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Line wrapping is not a variable; compare on the words."""
    return " ".join(text.split())


def _block(cell_or_card: str) -> str:
    """The cell's closing section, read from the file its card is assembled from.

    This used to be recovered by taking everything after ``facts-v2``'s judge
    heading. That mechanism is retired: these three cells each carry a
    *different* heading, so a hardcoded one cannot find their blocks.
    """
    return (BLOCKS / f"{BLOCK_OF[cell_or_card]}.md").read_text(encoding="utf-8")


def _shared_body_of(card_name: str, cell: str) -> str:
    """The card with its block removed -- everything the cell may not vary."""
    block = _block(cell)
    assert card_name == BASE_CARD or _card(card_name).count(block) == 1, card_name
    return _card(card_name).replace(block, "")


def _closing(card_name: str) -> str:
    cell = next(c for c, (name, _) in CELLS.items() if name == card_name) \
        if card_name != BASE_CARD else BASE_CARD
    return _block(cell)


@pytest.fixture(scope="module")
def baseline():
    return load_team_config(path=str(CONFIGS / "team.handoff.experiment.yaml"))


def _team(cell: str):
    return load_team_config(path=str(CONFIGS / CELLS[cell][1]))


# --- The cards ----------------------------------------------------------------


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_card_is_facts_v2_byte_for_byte_up_to_its_closing_section(cell: str) -> None:
    """One block moves, or the cell is not on one axis.

    If any sentence outside the closing section drifted -- what the tools are,
    how a sha travels, which tree is read as the answer -- a difference measured
    between this cell and ``facts-v2`` would be reading two changes at once, and
    the axis would no longer be the thing the cell is named for.
    """
    assert _shared_body_of(CELLS[cell][0], cell) == _shared_body_of(BASE_CARD, BASE_CARD)
    assert _block(cell) in _card(CELLS[cell][0]), "the card does not carry its declared block"


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_closing_section_differs_from_the_one_it_replaces(cell: str) -> None:
    """A cell whose closing section is ``facts-v2``'s is ``facts-v2``.

    It would then be a second copy of an already-measured cell, and any
    difference in its numbers would be run-to-run noise reported as an effect.
    """
    assert _closing(CELLS[cell][0]) != _closing(BASE_CARD)


def test_the_three_closing_sections_are_three_different_sections() -> None:
    """Three files that say the same thing are one cell, not three."""
    blocks = {cell: _closing(CELLS[cell][0]) for cell in CELLS}
    assert len(set(blocks.values())) == len(CELLS), sorted(blocks)


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_no_card_on_this_axis_orders_the_handoff(cell: str) -> None:
    """The compelled action is never "hand the work over".

    ``instructed`` already measures what happens when delegation is ordered.
    These cells exist to separate "an action is compelled" from "delegation is
    the ordered action"; a card that ordered it would answer a question that is
    already answered and leave this one open.
    """
    card = _card(CELLS[cell][0]).lower()
    for order in DELEGATION_ORDERS:
        assert order not in card, order


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_card_keeps_the_shared_closing_line(cell: str) -> None:
    """Every Analyst card on this experiment ends on the same evidence rule.

    Dropping it would let a cell differ in what counts as a verified change,
    which is an outcome-side variable and not the one being varied.
    """
    assert _card(CELLS[cell][0]).rstrip("\n").endswith(CLOSING_LINE)


def test_the_decide_first_card_names_both_answers() -> None:
    """A forced choice with one option named is a forced option.

    The compelled thing here is the sentence, not its content. If only the
    handoff branch were named, the cell would sit next to ``instructed`` on the
    axis it is supposed to be measured against, and a non-zero result could not
    be attributed to the compulsion rather than to the naming.
    """
    block = _flat(_closing(CELLS["decide-first"][0]))
    assert "keeping the implementation" in block
    assert "handing it to the Coder" in block
    # Neither branch is set up as the expected one.
    assert "Both are legitimate answers and neither one is expected of you" in block
    assert "What is not an answer is starting without having chosen" in block


def test_the_opt_out_card_asks_for_a_message_on_the_keeping_branch() -> None:
    """The whole point of this cell is the branch that is easy to leave silent.

    Everywhere else, an Analyst that keeps the work sends nothing, so "zero
    ``message_agent`` calls" and "no decision was taken" are indistinguishable.
    The keeping branch has to cost a message too, or the asymmetry survives and
    the cell measures what the other cells already measured.
    """
    block = _flat(_closing(CELLS["opt-out-message"][0]))
    assert "`message_agent`" in block
    assert "If you are keeping the implementation, that message says so" in block
    assert "so the Coder can stop waiting" in block
    # The handoff branch is the other message, not the recommended one.
    assert "The choice between them is yours. Sending one of them is not." in block


def test_the_role_identity_card_says_the_tools_are_in_hand_and_permitted() -> None:
    """Without this the cell would be starvation by implication.

    The Analyst really does hold ``apply_patch``, ``file_write``, ``bash`` and
    ``run_tests`` here. A card that framed the role as "the Coder writes" and
    left the permission unstated would invite the model to infer a rule that
    does not exist, and an Analyst avoiding its own tools would then be obeying
    an inferred prohibition rather than choosing -- which is the reading this
    whole experiment exists to rule out.
    """
    block = _flat(_closing(CELLS["role-identity"][0]))
    assert "You also hold the Coder's tools" in block
    assert "nothing here forbids using them" in block
    assert "no rule is broken and nothing refuses it" in block


# --- The team files -----------------------------------------------------------


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_cell_seats_the_baseline_team(cell: str, baseline) -> None:
    """Tools, roster and topology are the instrument; only the prompt varies.

    A cell that quietly dropped ``message_agent`` from the Analyst, or a return
    edge from the topology, would still run and still produce a transcript --
    one in which the handoff simply never happened. The zero would read as a
    fact about the model and be a fact about this file.
    """
    variant = _team(cell)
    assert variant.entry == baseline.entry
    assert sorted(variant.roles) == sorted(baseline.roles)
    for role in ROLES:
        assert variant.roles[role].tools == baseline.roles[role].tools, role
        if role != "analyst":
            assert variant.roles[role].prompt == baseline.roles[role].prompt, role
    walked = {(s, d) for s in ROLES for d in ROLES if variant.topology.allows(s, d)}
    assert walked == {(s, d) for s in ROLES for d in ROLES if s != d}
    assert not any(variant.topology.allows(role, role) for role in ROLES)


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_cell_loads_its_own_analyst_card(cell: str, baseline) -> None:
    """The one thing that is allowed to differ has to actually differ.

    A ``prompt_file`` left pointing at the base card would make the cell a
    silent duplicate of ``facts-v2`` -- a wrong answer with no error.
    """
    variant = _team(cell)
    assert variant.roles["analyst"].prompt == _card(CELLS[cell][0])
    assert variant.roles["analyst"].prompt != baseline.roles["analyst"].prompt
    assert variant.roles["analyst"].prompt != _card(BASE_CARD)


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_each_team_file_declares_that_its_runs_are_not_poolable(cell: str) -> None:
    """Every cell here differs from every other one, so nothing may be merged.

    The failure this guards against is silent: pooling two cells produces a
    number, not an error. The declaration lives in the file that names the
    cell so that whoever selects it reads it.
    """
    text = (CONFIGS / CELLS[cell][1]).read_text(encoding="utf-8").lower()
    assert "poolable" in text
    assert "forced-action axis" in text
