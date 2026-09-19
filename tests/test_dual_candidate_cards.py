"""The dual-candidate family's own invariants, kept apart from the ladder's.

This family is a second instrument, not more cells of the handoff ladder: an
``adopter`` and two coders that cannot address each other, over four directed
edges instead of six. Its files therefore live in ``configs/dual-candidate/``
and are named ``team.dual-*.yaml``, outside the ``team.handoff.*.yaml``
namespace that ``test_analyst_card_assembly.py`` reads as "one instrument,
seventeen files". Keeping the namespaces apart is what lets both instruments
carry the same guarantees without either test weakening the other's.

What is asserted here is what is asserted there, restated for this roster: a
card is exactly what its declaration assembles to, one place varies and it is
the declared one, no two cells collapse into each other, and every cell seats
the same roster and the same partial topology.
"""

from __future__ import annotations

import difflib
import itertools
import re
from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config
from scripts.analyst_cards import (
    BLOCK_SLOT,
    CAPABILITIES_SLOT,
    CLOSING_LINE,
    DUAL_CARDS,
    load_registry,
    render,
    shared_body,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "dual-candidate"
HANDOFF = CONFIGS / "handoff-experiment"
#: Each cell of this family against the handoff cell it restates. The block on
#: the right is the block on the left with ONE paragraph replaced -- the one
#: that divides the work, which this roster forces to change. Everything else,
#: including `cmd-prohibit`'s prohibition sentence and `cmd-optout`'s permission
#: paragraph, is the handoff block's bytes. `judge` has no division paragraph at
#: all (it names no role), so that pair is byte identity.
COUNTERPARTS = {
    "judge": "judge",
    "prohibit": "cmd-prohibit",
    "plain": "cmd-plain",
    "bare": "cmd-bare",
    "optout": "cmd-optout",
}
DIVISION_MARKER = "Implementation is the Coder"
#: Sentences that live INSIDE a handoff division paragraph but name no role,
#: and therefore have to cross into ours unedited. They are invisible to the
#: paragraph comparison below, which drops that whole paragraph -- and they are
#: exactly what a ladder move is, so a reworded one is a rung that is no longer
#: the rung it is named after, with nothing saying so.
CARRIED_SENTENCES = ("Do not apply the fix yourself.",)
ROLES = ("adopter", "coder_a", "coder_b")
ENTRY = "adopter"
#: Four directed edges. The two coders are not connected, which is what makes
#: the candidates independent by construction rather than by the model's
#: restraint -- and it is the treatment's instrument, so no cell may vary it.
EDGES = {
    ("adopter", "coder_a"),
    ("adopter", "coder_b"),
    ("coder_a", "adopter"),
    ("coder_b", "adopter"),
}
TEAM_TOOLS = (
    "apply_patch", "bash", "file_read", "file_write", "grep",
    "message_agent", "run_tests", "submit", "team_status",
)

VARIANTS, LADDERS = load_registry(DUAL_CARDS)
NAMES = sorted(VARIANTS)


def _block(name: str) -> str:
    return (PROMPTS / "blocks" / f"{name}.md").read_text(encoding="utf-8")


def _capabilities(name: str) -> str:
    return (PROMPTS / "capabilities" / f"{name}.md").read_text(encoding="utf-8")


def _card_on_disk(name: str) -> str:
    return VARIANTS[name].card_path.read_text(encoding="utf-8")


def _unwrapped(text: str) -> str:
    """The text with every run of whitespace collapsed, for move counting."""
    return re.sub(r"\s+", " ", text).strip()


def _paragraphs(text: str) -> list[str]:
    return text.split("\n\n")


# --- This family restates the handoff ladder, cell for cell --------------------


@pytest.mark.parametrize("ours,theirs", sorted(COUNTERPARTS.items()))
def test_each_block_is_its_handoff_counterpart_outside_the_division_paragraph(
    ours: str, theirs: str
) -> None:
    """One paragraph carries the roster; every other paragraph is copied.

    This is what makes a rung here comparable with the rung it is named after.
    If the two blocks drifted anywhere else -- a softer verb in the closing
    paragraph, a comma in the suspension line -- a difference between the two
    families' numbers could be that drift, and nothing would say so.
    """
    mine = _paragraphs(_block(ours))
    theirs_paragraphs = _paragraphs((HANDOFF / "blocks" / f"{theirs}.md").read_text(encoding="utf-8"))
    kept = [p for p in mine if DIVISION_MARKER not in p]
    their_kept = [p for p in theirs_paragraphs if DIVISION_MARKER not in p]
    assert kept == their_kept, ours
    replaced, their_replaced = len(mine) - len(kept), len(theirs_paragraphs) - len(their_kept)
    assert replaced == their_replaced <= 1, (ours, replaced, their_replaced)


@pytest.mark.parametrize("ours,theirs", sorted(COUNTERPARTS.items()))
def test_the_role_free_sentences_of_a_division_paragraph_cross_unedited(
    ours: str, theirs: str
) -> None:
    """A move the handoff ladder makes, this ladder makes in the same words."""
    mine = _unwrapped(_block(ours))
    theirs_text = _unwrapped((HANDOFF / "blocks" / f"{theirs}.md").read_text(encoding="utf-8"))
    for sentence in CARRIED_SENTENCES:
        assert (sentence in mine) == (sentence in theirs_text), (ours, sentence)


def test_the_two_families_state_the_same_tools_in_the_same_words() -> None:
    """The Adopter's bundle is the Analyst's, so the sentence that states it is too.

    The second paragraph of each file names the teammates and therefore differs;
    the first names the tools and may not.
    """
    ours = _paragraphs(_capabilities("full"))[0]
    theirs = _paragraphs((HANDOFF / "capabilities" / "full.md").read_text(encoding="utf-8"))[0]
    assert ours == theirs


# --- Assembly ------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_checked_in_card_is_exactly_what_its_declaration_assembles_to(name: str) -> None:
    """The team files load the checked-in card, not the assembly."""
    assert _card_on_disk(name) == render(VARIANTS[name])


@pytest.mark.parametrize("name", NAMES)
def test_a_card_is_the_shared_body_everywhere_outside_its_block(name: str) -> None:
    """Whatever surrounds the block is not this cell's to vary."""
    variant = VARIANTS[name]
    card = _card_on_disk(name)
    block = _block(variant.block)
    assert card.count(block) == 1, name
    assert card.replace(block, BLOCK_SLOT) == shared_body(DUAL_CARDS).replace(
        CAPABILITIES_SLOT, _capabilities(variant.capabilities)
    )


@pytest.mark.parametrize("name", NAMES)
def test_every_card_ends_on_the_shared_evidence_rule(name: str) -> None:
    """The one rule both instruments share: what counts as a verified change."""
    assert _card_on_disk(name).endswith("\n" + CLOSING_LINE)
    assert CLOSING_LINE not in _block(VARIANTS[name].block)


def test_the_two_card_sets_do_not_share_a_body() -> None:
    """A card of this family is not a card of the ladder with a word changed.

    Stated as a test because the cheap way to add this family would have been
    to point it at ``handoff-experiment/shared.md``, and the resulting cards
    would then have told an Adopter it was on a team with a Tester.
    """
    from scripts.analyst_cards import HANDOFF_CARDS

    assert shared_body(DUAL_CARDS) != shared_body(HANDOFF_CARDS)
    assert DUAL_CARDS.directory != HANDOFF_CARDS.directory


# --- Cells must not collapse into each other -----------------------------------


def test_no_two_cells_assemble_to_the_same_card() -> None:
    cards = {name: _card_on_disk(name) for name in NAMES}
    duplicates = [
        (a, b) for a, b in itertools.combinations(NAMES, 2) if cards[a] == cards[b]
    ]
    assert not duplicates, duplicates


def test_no_two_block_files_hold_the_same_text() -> None:
    blocks = sorted({VARIANTS[name].block for name in NAMES})
    texts = {name: _block(name) for name in blocks}
    duplicates = [
        (a, b) for a, b in itertools.combinations(blocks, 2) if texts[a] == texts[b]
    ]
    assert not duplicates, duplicates


def test_every_registered_block_and_bundle_file_exists_and_is_used() -> None:
    used_blocks = {VARIANTS[name].block for name in NAMES}
    assert used_blocks == {p.stem for p in (PROMPTS / "blocks").glob("*.md")}
    used_capabilities = {VARIANTS[name].capabilities for name in NAMES}
    assert used_capabilities == {p.stem for p in (PROMPTS / "capabilities").glob("*.md")}


# --- Adjacent rungs differ by exactly one move ---------------------------------


def test_each_ladder_step_is_exactly_one_contiguous_move() -> None:
    """A rung that differs in two places cannot attribute the difference.

    Stated as the strongest form of "one move": one rung's block is the other's
    with a single contiguous passage inserted, and nothing else. ``difflib``
    reports that as exactly one non-equal opcode, and it must be an insertion
    on one side -- never an insertion and a deletion, which is a rewrite
    wearing a ladder's clothes.

    Compared with line breaks collapsed. Inserting a sentence into a paragraph
    re-wraps the lines after it, and a re-wrap is a consequence of the move, not
    a second one; compared byte for byte, the handoff ladder would fail this
    too.
    """
    for ladder, rungs in LADDERS.items():
        for upper, lower in zip(rungs, rungs[1:]):
            a = _unwrapped(_block(VARIANTS[upper].block))
            b = _unwrapped(_block(VARIANTS[lower].block))
            moves = [
                op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                if op[0] != "equal"
            ]
            assert len(moves) == 1, f"{ladder}: {upper} -> {lower} is {len(moves)} moves, not one"
            assert moves[0][0] in {"insert", "delete"}, (
                f"{ladder}: {upper} -> {lower} replaces text rather than adding or removing it"
            )


# --- Every cell seats the same team --------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_each_cell_seats_the_dual_candidate_team(name: str) -> None:
    """Roster, bundles and topology are the instrument, not the treatment.

    A cell that quietly connected the two coders, or dropped a return edge,
    would still run and still produce a transcript -- one in which the two
    candidates were not independent, or could not be delivered. Neither shows
    up as an error; both show up as a number.
    """
    variant = VARIANTS[name]
    team = load_team_config(path=str(CONFIGS / variant.team_file))
    assert team.entry == ENTRY
    assert sorted(team.roles) == sorted(ROLES)
    for role in ROLES:
        assert tuple(team.roles[role].tools) == TEAM_TOOLS, role
    assert team.roles[ENTRY].prompt == _card_on_disk(name)
    for role, card in (("coder_a", "coder-a.md"), ("coder_b", "coder-b.md")):
        assert team.roles[role].prompt == (PROMPTS / card).read_text(encoding="utf-8")
    walked = {(s, d) for s in ROLES for d in ROLES if team.topology.allows(s, d)}
    assert walked == EDGES
    assert not team.topology.allows("coder_a", "coder_b")
    assert not team.topology.allows("coder_b", "coder_a")


def test_the_two_coder_cards_differ_only_in_identity_and_the_answer_asked_for() -> None:
    """Two seats, one instrument: the stance is the only declared difference.

    If anything else drifted between them -- a tool sentence, a worktree
    sentence -- the two candidates would differ for a reason the cell does not
    name, and "A was narrower than B" would stop being about the stance.
    """
    heading = "## The answer you are asked for"
    a, b = (
        (PROMPTS / f"coder-{side}.md").read_text(encoding="utf-8") for side in ("a", "b")
    )
    body_a, stance_a = a.split(heading)
    body_b, stance_b = b.split(heading)
    assert stance_a != stance_b
    identity = str.maketrans({})
    normalize = lambda text: (  # noqa: E731
        text.translate(identity)
        .replace("Coder A", "<peer>").replace("Coder B", "<peer>")
        .replace("`coder_a`", "<token>").replace("`coder_b`", "<token>")
    )
    assert normalize(body_a) == normalize(body_b)


# --- The team files themselves --------------------------------------------------

ENTRY_PROMPT_LINE = "    prompt_file: dual-candidate/adopter."


def _team_file_body(path: Path) -> str:
    """The configuration below the header comment, with the Adopter's line masked."""
    text = path.read_text(encoding="utf-8")
    body = text[text.index("entry:"):]
    return "".join(
        "PROMPT_FILE\n" if line.startswith(ENTRY_PROMPT_LINE) else line
        for line in body.splitlines(keepends=True)
    )


def test_every_team_file_differs_only_in_the_adopters_prompt_line() -> None:
    """Four files, one instrument. A cell is selected by naming its team file."""
    paths = sorted(CONFIGS.glob("team.dual-*.yaml"))
    bodies = {path.name: _team_file_body(path) for path in paths}
    assert len(bodies) == len(VARIANTS)
    distinct = set(bodies.values())
    assert len(distinct) == 1, sorted(
        name for name, body in bodies.items() if body != next(iter(distinct))
    )


def test_this_family_stays_out_of_the_ladders_namespace() -> None:
    """The ladder's own test globs ``team.handoff.*.yaml`` and counts the hits.

    A cell of this family filed under that name would either fail that test or
    force it to be loosened, and what it asserts -- seventeen files, one
    instrument -- is the ladder's whole claim to comparability.
    """
    assert not list(CONFIGS.glob("team.handoff.dual-*.yaml"))
    for name in NAMES:
        assert VARIANTS[name].team_file.startswith("team.dual-")


# --- Every cell has to declare what it may not be pooled with -------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_cell_carries_a_note(name: str) -> None:
    assert len(VARIANTS[name].note.strip()) > 40, name
