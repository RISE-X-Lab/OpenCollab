"""The Single2-seated self-collaboration family, and what makes it one instrument.

The handoff experiment's Analyst, Coder and Tester, seated the way ``s2dual``
seats its roster, over two topologies with edges removed. The cells are a claim
that they differ in one directed edge -- ``coder -> analyst`` -- and in the one
line of every card that states it, and nowhere else. That claim holds only if
the edge sets really differ by that edge, every card states the topology the
scheduler actually enforces, everything outside the topology is the same bytes
in both cells, and the seat and the bundle are ``s2dual-judge``'s.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.scheduler_factory import _reject_unwalkable_edges
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.team_config import load_team_config
from opencollab.teams import declared_role_profiles
from scripts.analyst_cards import (
    BLOCK_SLOT,
    CAPABILITIES_SLOT,
    CLOSING_LINE,
    S2SC_CARDS,
    load_registry,
    peer_body,
    render,
    render_peer,
    shared_body,
    slot_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "s2sc"
ROLES = ("analyst", "coder", "tester")
ENTRY = "analyst"
PROFILE = "single2"
TOPOLOGY_SLOT = slot_marker("TOPOLOGY")
#: ``s2dual-judge``'s bundle, on every role here -- the Tester included, so the
#: division of labour is on the cards and not in what a seat can call.
TEAM_TOOLS = (
    "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
    "message_agent", "team_status", "submit",
)
#: The edges each cell is registered to seat. The two differ in one edge.
EDGES = {
    "s2sc-judge-bypass": {
        ("analyst", "coder"),
        ("coder", "analyst"),
        ("coder", "tester"),
        ("tester", "analyst"),
    },
    "s2sc-judge-pipeline": {
        ("analyst", "coder"),
        ("coder", "tester"),
        ("tester", "analyst"),
    },
}
THE_EDGE = ("coder", "analyst")
PROFILE_SENTENCE = "A final response without tool calls ends the agent session."
CORRECTION = "A response with no tool call ends your turn, not the run."

VARIANTS, LADDERS = load_registry(S2SC_CARDS)
NAMES = sorted(VARIANTS)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _team(name: str):
    return load_team_config(path=str(CONFIGS / VARIANTS[name].team_file))


def _cards_on_disk(name: str) -> dict[str, str]:
    variant = VARIANTS[name]
    cards = {ENTRY: _read(variant.card_path)}
    for peer in S2SC_CARDS.peers:
        cards[peer] = _read(variant.peer_card_path(peer))
    return cards


def _topology_text(name: str) -> str:
    return _read(PROMPTS / "topology" / f"{dict(VARIANTS[name].slots)['TOPOLOGY']}.md")


_EDGE_LINE = re.compile(r"^- the (Analyst|Coder|Tester) can address (.+?)[;.]$")


def _edges_stated(text: str) -> set[tuple[str, str]]:
    """The edges a card's topology paragraph says exist, read off its list."""
    edges = set()
    for line in text.splitlines():
        match = _EDGE_LINE.match(line)
        if not match:
            continue
        source, targets = match.groups()
        for target in targets.split(" and "):
            assert target.startswith("the "), line
            edges.add((source.lower(), target.removeprefix("the ").lower()))
    return edges


# --- The seat is Single2 -------------------------------------------------------


def test_the_registry_is_the_two_topologies() -> None:
    assert NAMES == sorted(EDGES)


@pytest.mark.parametrize("name", NAMES)
def test_every_seat_declares_the_profile(name: str) -> None:
    profiles = declared_role_profiles(str(CONFIGS / VARIANTS[name].team_file))
    assert profiles == {role: PROFILE for role in ROLES}


@pytest.mark.parametrize("name", NAMES)
def test_the_seated_prompt_is_single2s_with_the_card_after_it(name: str) -> None:
    team = _team(name)
    builder = ContextBuilder(
        team,
        SpawnConfig(
            model="m", provider="openai", api_key="k", base_url=None,
            llm_timeout=600.0, tracer=None, event_bus=EventBus(None),
            permission_policy=None,
        ),
    )
    cards = _cards_on_disk(name)
    for role in ROLES:
        prompt = builder.build_plan(role).system_prompt()
        assert prompt.startswith(SINGLE2_SYSTEM_PROMPT), role
        assert team.roles[role].prompt == cards[role], role
        assert cards[role] in prompt, role


@pytest.mark.parametrize("name", NAMES)
def test_every_card_corrects_what_the_profile_says_about_ending_a_run(name: str) -> None:
    assert PROFILE_SENTENCE in SINGLE2_SYSTEM_PROMPT
    cards = _cards_on_disk(name)
    for role in ROLES:
        assert CORRECTION in cards[role], role
    assert "`submit`" in cards[ENTRY]


# --- The treatment: the topology, stated truly --------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_scheduler_enforces_the_registered_edges(name: str) -> None:
    team = _team(name)
    walked = {(s, d) for s in ROLES for d in ROLES if s != d and team.topology.allows(s, d)}
    assert walked == EDGES[name]


def test_the_two_cells_differ_in_exactly_the_one_edge() -> None:
    bypass, pipeline = EDGES["s2sc-judge-bypass"], EDGES["s2sc-judge-pipeline"]
    assert bypass - pipeline == {THE_EDGE}
    assert pipeline - bypass == set()


@pytest.mark.parametrize("name", NAMES)
def test_every_card_states_the_topology_the_team_file_declares(name: str) -> None:
    """A card that described an edge the scheduler refuses, or left out one it
    allows, would put the seat in front of a false premise about the very thing
    the cells vary."""
    team = _team(name)
    declared = {(s, d) for s in ROLES for d in ROLES if s != d and team.topology.allows(s, d)}
    for role, card in _cards_on_disk(name).items():
        assert _edges_stated(card) == declared, role


@pytest.mark.parametrize("name", NAMES)
def test_all_three_cards_carry_the_same_topology_text(name: str) -> None:
    text = _topology_text(name)
    for role, card in _cards_on_disk(name).items():
        assert card.count(text) == 1, role


def test_no_card_promises_a_reply_the_topology_may_not_carry() -> None:
    """``s2dual``'s body said any addressee "can send one back to you the same
    way". Here that is false of the Coder in the pipeline, so the sentence is
    gone from every body and the topology paragraph says who can reply."""
    for name in NAMES:
        for role, card in _cards_on_disk(name).items():
            assert "can send one back to you" not in card, (name, role)


def test_the_topology_ladder_is_one_contiguous_deletion() -> None:
    for ladder, rungs in LADDERS.items():
        for upper, lower in zip(rungs, rungs[1:]):
            a = " ".join(_topology_text(upper).split())
            b = " ".join(_topology_text(lower).split())
            moves = [
                op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
                if op[0] != "equal"
            ]
            assert len(moves) == 1, f"{ladder}: {upper} -> {lower} is {len(moves)} moves"
            assert moves[0][0] == "delete", f"{ladder}: {upper} -> {lower}"


# --- Everything else is held fixed ---------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_block_is_s2dual_judges_byte_for_byte(name: str) -> None:
    block = VARIANTS[name].block
    assert block == "judge"
    ours = _read(PROMPTS / "blocks" / f"{block}.md")
    assert ours == _read(CONFIGS / "s2dual" / "blocks" / "judge.md")
    assert ours == _read(CONFIGS / "handoff-experiment" / "blocks" / "judge.md")


@pytest.mark.parametrize("name", NAMES)
def test_every_role_holds_s2dual_judges_bundle(name: str) -> None:
    team = _team(name)
    reference = load_team_config(path=str(CONFIGS / "team.s2dual-judge.yaml"))
    assert tuple(reference.roles["adopter"].tools) == TEAM_TOOLS
    assert team.entry == ENTRY
    assert sorted(team.roles) == sorted(ROLES)
    for role in ROLES:
        assert tuple(team.roles[role].tools) == TEAM_TOOLS, role
        assert tuple(VARIANTS[name].tools) == TEAM_TOOLS


@pytest.mark.parametrize("name", NAMES)
def test_every_declared_edge_is_walkable(name: str) -> None:
    """``build_scheduler`` refuses a prebuilt team whose edges a seat cannot walk."""
    _reject_unwalkable_edges(_team(name))


def test_the_capabilities_pair_states_the_bundle_every_role_holds() -> None:
    text = " ".join(_read(PROMPTS / "capabilities" / "full.md").split())
    assert "each holds the same tools you do" in text
    assert "minus" not in text


def test_the_cells_are_the_same_bytes_outside_the_topology() -> None:
    a, b = (_cards_on_disk(name) for name in NAMES)
    ta, tb = (_topology_text(name) for name in NAMES)
    assert ta != tb
    for role in ROLES:
        assert a[role].replace(ta, TOPOLOGY_SLOT) == b[role].replace(tb, TOPOLOGY_SLOT), role


# --- Assembly ------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_checked_in_card_is_exactly_what_its_declaration_assembles_to(name: str) -> None:
    variant = VARIANTS[name]
    cards = _cards_on_disk(name)
    assert cards[ENTRY] == render(variant)
    for peer in S2SC_CARDS.peers:
        assert cards[peer] == render_peer(variant, peer), peer


@pytest.mark.parametrize("name", NAMES)
def test_the_analyst_card_is_the_shared_body_outside_its_slots(name: str) -> None:
    variant = VARIANTS[name]
    card = _cards_on_disk(name)[ENTRY]
    block = _read(PROMPTS / "blocks" / f"{variant.block}.md")
    capabilities = _read(PROMPTS / "capabilities" / f"{variant.capabilities}.md")
    unfilled = (
        card.replace(block, BLOCK_SLOT)
        .replace(capabilities, CAPABILITIES_SLOT)
        .replace(_topology_text(name), TOPOLOGY_SLOT)
    )
    assert unfilled == shared_body(S2SC_CARDS)


@pytest.mark.parametrize("peer", S2SC_CARDS.peers)
def test_each_peer_card_is_its_template_outside_the_topology(peer: str) -> None:
    for name in NAMES:
        card = _cards_on_disk(name)[peer]
        assert card.replace(_topology_text(name), TOPOLOGY_SLOT) == peer_body(S2SC_CARDS, peer)


def test_the_analyst_card_ends_on_the_shared_evidence_rule() -> None:
    for name in NAMES:
        assert _cards_on_disk(name)[ENTRY].endswith("\n" + CLOSING_LINE)


def test_no_team_file_loads_a_template_or_a_card_with_a_slot_left_in_it() -> None:
    for name in NAMES:
        team = _team(name)
        for role in ROLES:
            assert "{{" not in team.roles[role].prompt, (name, role)
        text = _read(CONFIGS / VARIANTS[name].team_file)
        for template in ("s2sc/shared.md", "s2sc/coder.md", "s2sc/tester.md"):
            assert template not in text, (name, template)


def test_every_registered_slot_file_exists_and_is_used() -> None:
    used = {dict(VARIANTS[name].slots)["TOPOLOGY"] for name in NAMES}
    assert used == {p.stem for p in (PROMPTS / "topology").glob("*.md")}
    assert {VARIANTS[n].block for n in NAMES} == {p.stem for p in (PROMPTS / "blocks").glob("*.md")}
    assert {VARIANTS[n].capabilities for n in NAMES} == {
        p.stem for p in (PROMPTS / "capabilities").glob("*.md")
    }


# --- Team files ------------------------------------------------------------------


def _team_file_body(path: Path) -> str:
    """The configuration below the header, with the lines the cells may vary
    -- the three cards and the topology block -- replaced by markers."""
    text = _read(path)
    body = text[text.index("entry:"):]
    body = re.sub(r"prompt_file: s2sc/(\w+)\.s2sc-[\w-]+\.md", r"prompt_file: \1-CARD", body)
    head, _, _ = body.partition("\n# ")
    return head


def test_every_team_file_differs_only_in_its_cards_and_its_topology() -> None:
    bodies = {name: _team_file_body(CONFIGS / VARIANTS[name].team_file) for name in NAMES}
    assert len(set(bodies.values())) == 1, bodies


def test_this_family_names_its_files_apart() -> None:
    """``team.s2dual-*``, ``team.dual-*`` and ``team.handoff.*`` are globbed and
    counted by their own test modules."""
    for name in NAMES:
        assert VARIANTS[name].team_file == f"team.{name}.yaml"
        assert name.startswith("s2sc-")
    assert not list(CONFIGS.glob("team.s2dual-s2sc*.yaml"))
    assert not list(CONFIGS.glob("team.handoff.s2sc*.yaml"))


@pytest.mark.parametrize("name", NAMES)
def test_every_cell_carries_a_note(name: str) -> None:
    assert len(VARIANTS[name].note.strip()) > 40, name
