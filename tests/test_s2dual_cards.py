"""The Single2-seated two-candidate family, and what makes it its own instrument.

Same roster as ``dual-candidate`` and the same five treatment blocks, byte for
byte. One thing differs: every seat declares ``profile: single2``, so the agent
under the card is the evaluated single agent rather than OpenCollab's own. That
is the family's whole claim, and it is only true if four things hold at once --
the blocks did not drift, the profile actually reaches the seat, the card is
appended to the profile's prompt rather than replacing it, and the card says
the things the profile's prompt gets wrong about a team seat.

What is asserted here mirrors ``test_dual_candidate_cards.py`` for this roster,
plus the profile-specific claims that file has no reason to make.
"""

from __future__ import annotations

import difflib
import itertools
import re
from pathlib import Path

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.team_config import load_team_config
from opencollab.teams import declared_role_profiles
from scripts.analyst_cards import (
    BLOCK_SLOT,
    CAPABILITIES_SLOT,
    CLOSING_LINE,
    S2DUAL_CARDS,
    load_registry,
    render,
    shared_body,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "s2dual"
DUAL = CONFIGS / "dual-candidate"
ROLES = ("adopter", "coder_a", "coder_b")
ENTRY = "adopter"
PROFILE = "single2"
#: Four directed edges. The two coders are not connected, which is what makes
#: the candidates independent by construction rather than by the model's
#: restraint -- the treatment's instrument, so no cell may vary it.
EDGES = {
    ("adopter", "coder_a"),
    ("adopter", "coder_b"),
    ("coder_a", "adopter"),
    ("coder_b", "adopter"),
}
#: Single2's six tools in its evaluated order, then the three a team seat needs.
#: The order is the file's and reaches the model as written, so it is pinned
#: here rather than sorted.
TEAM_TOOLS = (
    "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
    "message_agent", "team_status", "submit",
)
#: The sentence in Single2's own prompt that is false for a team seat, and the
#: card's correction of it. A card that lost the correction would leave a seat
#: believing its silence ends the run.
PROFILE_SENTENCE = "A final response without tool calls ends the agent session."
CORRECTION = "A response with no tool call does not end this run."

VARIANTS, LADDERS = load_registry(S2DUAL_CARDS)
NAMES = sorted(VARIANTS)


def _block(name: str) -> str:
    return (PROMPTS / "blocks" / f"{name}.md").read_text(encoding="utf-8")


def _card_on_disk(name: str) -> str:
    return VARIANTS[name].card_path.read_text(encoding="utf-8")


def _unwrapped(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _team(name: str):
    return load_team_config(path=str(CONFIGS / VARIANTS[name].team_file))


# --- The seat is Single2 -------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_seat_declares_the_profile(name: str) -> None:
    """All three, not just the entry one: a team of one Single2 and two others
    would be a different organization from the one this family claims to run."""
    profiles = declared_role_profiles(str(CONFIGS / VARIANTS[name].team_file))
    assert profiles == {role: PROFILE for role in ROLES}


@pytest.mark.parametrize("name", NAMES)
def test_the_seated_prompt_is_single2s_with_the_card_after_it(name: str) -> None:
    """The claim the family is named for, checked on the assembled prompt.

    Not "the card mentions Single2" -- the text a seat is actually seated with
    has to open on that profile's prompt, or the agent under the card is not
    the one the single-agent arm runs.
    """
    team = _team(name)
    builder = ContextBuilder(
        team,
        SpawnConfig(
            model="m", provider="openai", api_key="k", base_url=None,
            llm_timeout=600.0, tracer=None, event_bus=EventBus(None),
            permission_policy=None,
        ),
    )
    for role in ROLES:
        prompt = builder.build_plan(role).system_prompt()
        assert prompt.startswith(SINGLE2_SYSTEM_PROMPT), role
        assert team.roles[role].prompt in prompt, role


@pytest.mark.parametrize("name", NAMES)
def test_the_card_corrects_what_the_profile_says_about_ending_a_run(name: str) -> None:
    """Single2's prompt ends a session on a reply with no tool call. A team seat
    ends a run by calling `submit`, and a Coder's silence is how it waits."""
    assert PROFILE_SENTENCE in SINGLE2_SYSTEM_PROMPT
    for text in (_card_on_disk(name),
                 (PROMPTS / "coder-a.md").read_text(encoding="utf-8"),
                 (PROMPTS / "coder-b.md").read_text(encoding="utf-8")):
        assert CORRECTION in text
    assert "`submit`" in _card_on_disk(name)


# --- The treatment is the older family's, unchanged ----------------------------


@pytest.mark.parametrize("block", sorted({VARIANTS[n].block for n in NAMES}))
def test_each_block_is_the_dual_candidate_block_byte_for_byte(block: str) -> None:
    """The ladder is the same five moves. If a word drifted here, a difference
    between the two families would be the seat AND the wording, with nothing
    saying which."""
    assert _block(block) == (DUAL / "blocks" / f"{block}.md").read_text(encoding="utf-8")


def test_the_peer_paragraph_is_the_older_familys_too() -> None:
    """What each Coder is asked for, and what a Coder's budget is, are facts
    about the organization rather than about the seat, so they are not restated
    in this family's words."""
    ours = (PROMPTS / "capabilities" / "full.md").read_text(encoding="utf-8")
    theirs = (DUAL / "capabilities" / "full.md").read_text(encoding="utf-8")
    assert ours.split("\n\n")[1] == theirs.split("\n\n")[1]


# --- Assembly ------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_checked_in_card_is_exactly_what_its_declaration_assembles_to(name: str) -> None:
    assert _card_on_disk(name) == render(VARIANTS[name])


@pytest.mark.parametrize("name", NAMES)
def test_a_card_is_the_shared_body_everywhere_outside_its_block(name: str) -> None:
    variant = VARIANTS[name]
    card = _card_on_disk(name)
    block = _block(variant.block)
    capabilities = (
        PROMPTS / "capabilities" / f"{variant.capabilities}.md"
    ).read_text(encoding="utf-8")
    assert card.count(block) == 1, name
    assert card.replace(block, BLOCK_SLOT) == shared_body(S2DUAL_CARDS).replace(
        CAPABILITIES_SLOT, capabilities
    )


@pytest.mark.parametrize("name", NAMES)
def test_every_card_ends_on_the_shared_evidence_rule(name: str) -> None:
    assert _card_on_disk(name).endswith("\n" + CLOSING_LINE)
    assert CLOSING_LINE not in _block(VARIANTS[name].block)


def test_this_family_has_a_body_of_its_own() -> None:
    """Pointing it at the older family's body would have told a Single2 seat
    about tools twice, in two different sets of words."""
    from scripts.analyst_cards import DUAL_CARDS

    assert shared_body(S2DUAL_CARDS) != shared_body(DUAL_CARDS)
    assert S2DUAL_CARDS.directory != DUAL_CARDS.directory


# --- Cells must not collapse into each other -----------------------------------


def test_no_two_cells_assemble_to_the_same_card() -> None:
    cards = {name: _card_on_disk(name) for name in NAMES}
    duplicates = [
        (a, b) for a, b in itertools.combinations(NAMES, 2) if cards[a] == cards[b]
    ]
    assert not duplicates, duplicates


def test_every_registered_block_and_bundle_file_exists_and_is_used() -> None:
    used_blocks = {VARIANTS[name].block for name in NAMES}
    assert used_blocks == {p.stem for p in (PROMPTS / "blocks").glob("*.md")}
    used_capabilities = {VARIANTS[name].capabilities for name in NAMES}
    assert used_capabilities == {p.stem for p in (PROMPTS / "capabilities").glob("*.md")}


def test_each_ladder_step_is_exactly_one_contiguous_move() -> None:
    for ladder, rungs in LADDERS.items():
        for upper, lower in zip(rungs, rungs[1:]):
            a = _unwrapped(_block(VARIANTS[upper].block))
            b = _unwrapped(_block(VARIANTS[lower].block))
            moves = [
                op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                if op[0] != "equal"
            ]
            assert len(moves) == 1, f"{ladder}: {upper} -> {lower} is {len(moves)} moves"
            assert moves[0][0] in {"insert", "delete"}, f"{ladder}: {upper} -> {lower}"


# --- Every cell seats the same team --------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_each_cell_seats_the_same_roster_and_topology(name: str) -> None:
    team = _team(name)
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
    heading = "## The answer you are asked for"
    a, b = (
        (PROMPTS / f"coder-{side}.md").read_text(encoding="utf-8") for side in ("a", "b")
    )
    body_a, stance_a = a.split(heading)
    body_b, stance_b = b.split(heading)
    assert stance_a != stance_b
    normalize = lambda text: (  # noqa: E731
        text.replace("Coder A", "<peer>").replace("Coder B", "<peer>")
        .replace("`coder_a`", "<token>").replace("`coder_b`", "<token>")
    )
    assert normalize(body_a) == normalize(body_b)


ENTRY_PROMPT_LINE = "    prompt_file: s2dual/adopter."


def _team_file_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    body = text[text.index("entry:"):]
    return "".join(
        "PROMPT_FILE\n" if line.startswith(ENTRY_PROMPT_LINE) else line
        for line in body.splitlines(keepends=True)
    )


def test_every_team_file_differs_only_in_the_adopters_prompt_line() -> None:
    paths = sorted(CONFIGS.glob("team.s2dual-*.yaml"))
    bodies = {path.name: _team_file_body(path) for path in paths}
    assert len(bodies) == len(VARIANTS)
    distinct = set(bodies.values())
    assert len(distinct) == 1, sorted(
        name for name, body in bodies.items() if body != next(iter(distinct))
    )


def test_this_family_stays_out_of_the_other_two_namespaces() -> None:
    """``team.dual-*.yaml`` and ``team.handoff.*.yaml`` are globbed and counted
    by their own test modules; a cell of this family filed under either name
    would fail those or force them to be loosened."""
    assert not list(CONFIGS.glob("team.dual-s2*.yaml"))
    assert not list(CONFIGS.glob("team.handoff.s2dual-*.yaml"))
    for name in NAMES:
        assert VARIANTS[name].team_file.startswith("team.s2dual-")


@pytest.mark.parametrize("name", NAMES)
def test_every_cell_carries_a_note(name: str) -> None:
    assert len(VARIANTS[name].note.strip()) > 40, name
