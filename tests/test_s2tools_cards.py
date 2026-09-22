"""The s2dual roster with the Adopter's tools changed, and what keeps it comparable.

Each cell of this family is a claim that it is ``s2dual-judge`` with one thing
moved -- what the Adopter holds -- and that its card differs from
``adopter.s2dual-judge.md`` only where it has to say so. Both halves are
checked here on the files that are seated: the team files against
``team.s2dual-judge.yaml``, and the body, rendered with ``s2dual``'s own text in
this family's slots, against the ``s2dual-judge`` card byte for byte.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.team_config import load_team_config
from opencollab.bootstrap.tool_registry import build_tools_for_role
from opencollab.teams import declared_role_profiles
from scripts.analyst_cards import (
    BLOCK_SLOT,
    CAPABILITIES_SLOT,
    S2DUAL_CARDS,
    S2TOOLS_CARDS,
    load_registry,
    render,
    shared_body,
    slot_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"
PROMPTS = CONFIGS / "s2tools"
S2DUAL = CONFIGS / "s2dual"
ROLES = ("adopter", "coder_a", "coder_b")
EDGES = {
    ("adopter", "coder_a"),
    ("adopter", "coder_b"),
    ("coder_a", "adopter"),
    ("coder_b", "adopter"),
}
SINGLE2_SIX = ("bash", "file_read", "file_write", "apply_patch", "git_diff", "grep")
TEAM_TOOLS = (*SINGLE2_SIX, "message_agent", "team_status", "submit")
#: ``s2dual``'s own text for this family's two extra slots. The tools line is
#: quoted rather than read from a file because ``s2dual`` has no such file:
#: there it is part of the body.
S2DUAL_TOOLS_LINE = (
    "- Your tools are the six named above and three more: `message_agent`,\n"
    "  `team_status` and `submit`.\n"
)
S2DUAL_ADOPTION = (PROMPTS / "adoption" / "checkout.md").read_text(encoding="utf-8")

VARIANTS, _ = load_registry(S2TOOLS_CARDS)
NAMES = sorted(VARIANTS)
S2DUAL_VARIANTS, _ = load_registry(S2DUAL_CARDS)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _card_on_disk(name: str) -> str:
    return _read(VARIANTS[name].card_path)


def _team(name: str):
    return load_team_config(path=str(CONFIGS / VARIANTS[name].team_file))


def _slot_text(name: str, slot: str) -> str:
    value = dict(VARIANTS[name].slots)[slot]
    return _read(PROMPTS / slot.lower() / f"{value}.md")


def test_the_body_outside_the_slots_is_s2dual_judges_card_byte_for_byte() -> None:
    """Fill this family's slots with ``s2dual``'s own text and the result must be
    the ``s2dual-judge`` card. Then a cell here differs from ``s2dual-judge``
    exactly in what its slots hold, and nowhere a reader would have to find."""
    rebuilt = (
        shared_body(S2TOOLS_CARDS)
        .replace(slot_marker("TOOLS"), S2DUAL_TOOLS_LINE)
        .replace(slot_marker("ADOPTION"), S2DUAL_ADOPTION)
        .replace(CAPABILITIES_SLOT, _read(S2DUAL / "capabilities" / "full.md"))
        .replace(BLOCK_SLOT, _read(S2DUAL / "blocks" / "judge.md"))
    )
    assert rebuilt == _read(S2DUAL_VARIANTS["s2dual-judge"].card_path)
    assert S2DUAL_ADOPTION in _read(S2DUAL / "shared.md")


@pytest.mark.parametrize("name", NAMES)
def test_every_checked_in_card_is_what_its_declaration_assembles_to(name: str) -> None:
    assert _card_on_disk(name) == render(VARIANTS[name])


@pytest.mark.parametrize("name", NAMES)
def test_the_block_is_s2dual_judges_and_commands_nothing(name: str) -> None:
    assert VARIANTS[name].block == "judge"
    assert _read(PROMPTS / "blocks" / "judge.md") == _read(S2DUAL / "blocks" / "judge.md")


@pytest.mark.parametrize("name", NAMES)
def test_the_tools_line_names_what_the_adopter_holds_and_what_it_does_not(name: str) -> None:
    held = VARIANTS[name].tools
    text = _slot_text(name, "TOOLS")
    withheld = re.search(r"((?:`\w+`(?:, | and )?)+) are not yours here", text)
    assert withheld is not None
    assert set(re.findall(r"`(\w+)`", withheld.group(1))) == set(SINGLE2_SIX) - set(held)
    assert set(re.findall(r"`(\w+)`", text)) == set(SINGLE2_SIX) | set(held)


@pytest.mark.parametrize("name", NAMES)
def test_the_card_mentions_adopt_exactly_when_the_adopter_holds_it(name: str) -> None:
    holds = "adopt" in VARIANTS[name].tools
    assert ("`adopt`" in _card_on_disk(name)) is holds
    assert ("`git checkout <sha>`" in _card_on_disk(name)) is (not holds)
    assert ("bash" in VARIANTS[name].tools) is (not holds)


@pytest.mark.parametrize("name", NAMES)
def test_every_seat_is_single2_and_the_seated_prompt_opens_on_its_prompt(name: str) -> None:
    team = _team(name)
    assert declared_role_profiles(str(CONFIGS / VARIANTS[name].team_file)) == {
        role: "single2" for role in ROLES
    }
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
def test_only_the_adopter_changed_the_coders_are_s2duals(name: str) -> None:
    team = _team(name)
    assert team.entry == "adopter"
    assert sorted(team.roles) == sorted(ROLES)
    assert tuple(team.roles["adopter"].tools) == VARIANTS[name].tools
    assert team.roles["adopter"].prompt == _card_on_disk(name)
    for role, card in (("coder_a", "coder-a.md"), ("coder_b", "coder-b.md")):
        assert tuple(team.roles[role].tools) == TEAM_TOOLS, role
        assert team.roles[role].prompt == _read(S2DUAL / card), role
    walked = {(s, d) for s in ROLES for d in ROLES if team.topology.allows(s, d)}
    assert walked == EDGES


@pytest.mark.parametrize("name", NAMES)
def test_every_tool_the_adopter_is_given_resolves(name: str) -> None:
    names = [n for n in VARIANTS[name].tools if n not in {"message_agent", "team_status"}]
    assert [tool.name for tool in build_tools_for_role(names)] == names


def _below_header(path: Path) -> list[str]:
    text = _read(path)
    return text[text.index("entry:"):].splitlines()


@pytest.mark.parametrize("name", NAMES)
def test_the_team_file_is_s2dual_judges_but_for_the_adopters_two_lines(name: str) -> None:
    ours = _below_header(CONFIGS / VARIANTS[name].team_file)
    theirs = _below_header(CONFIGS / "team.s2dual-judge.yaml")
    assert len(ours) == len(theirs)
    differing = [(a, b) for a, b in zip(ours, theirs) if a != b]
    assert [a.split(":")[0].strip() for a, _ in differing] == ["prompt_file", "tools"]


def test_every_slot_file_is_used() -> None:
    for directory, used in (
        ("blocks", {VARIANTS[n].block for n in NAMES}),
        ("capabilities", {VARIANTS[n].capabilities for n in NAMES}),
        ("tools", {dict(VARIANTS[n].slots)["TOOLS"] for n in NAMES}),
        ("adoption", {dict(VARIANTS[n].slots)["ADOPTION"] for n in NAMES}),
    ):
        assert used == {p.stem for p in (PROMPTS / directory).glob("*.md")}, directory


def test_this_family_names_its_files_apart() -> None:
    """``team.s2dual-*.yaml`` is globbed by ``test_s2dual_cards.py`` and read as
    one team; a cell of this family filed there would fail it."""
    for name in NAMES:
        assert VARIANTS[name].team_file.startswith("team.s2tools-")
    assert len(list(CONFIGS.glob("team.s2tools-*.yaml"))) == len(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_every_cell_carries_a_note(name: str) -> None:
    assert len(VARIANTS[name].note.strip()) > 40, name
