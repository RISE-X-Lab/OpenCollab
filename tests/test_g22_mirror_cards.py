"""The G22-mirror cell's invariants: G22's words, this runtime's collaboration.

`team.dual-*.yaml` restates G22's ORGANIZATION in this repository's voice;
`team.g22-mirror.yaml` restates G22's WORDS. What makes the second readable is
that its departures from the source are a closed, declared list -- so this
module pins the sentences that must have crossed unedited, and pins the bundle
each seat actually gets against the bundle G22 gives the same seat.

The expected strings below are copied from OpenCollab-Eval at
``src/opencollab_eval/workflows/validation_council_dual_coder_contract.py``
(``MINIMAL_CODER_PROMPT`` / ``CROSS_COMPONENT_CODER_PROMPT`` /
``CONTRACT_PROMPT``) and ``_validation_council_solve_defs.SHARED_RULES``. They
live here rather than being imported because that package is a separate
repository: a copy that drifts is exactly what this file exists to catch, and a
drifting copy fails loudly here instead of silently changing the instrument.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config
from opencollab.bootstrap.tool_registry import KNOWN_TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parents[1]
TEAM_FILE = REPO_ROOT / "configs" / "team.g22-mirror.yaml"
CARDS = REPO_ROOT / "configs" / "g22-mirror"

ENTRY = "adopter"
ROLES = ("adopter", "coder_a", "coder_b")
#: Four directed edges. The two candidates are not connected, so their
#: independence is the roster's rather than the model's restraint.
EDGES = {
    ("adopter", "coder_a"),
    ("adopter", "coder_b"),
    ("coder_a", "adopter"),
    ("coder_b", "adopter"),
}
#: G22's ``_coder_tools()``, exactly. A difference here is a difference in the
#: instrument, which is the whole reason this cell exists beside `dual-*`.
G22_CANDIDATE_TOOLS = frozenset(
    {"bash", "file_read", "file_write", "apply_patch", "grep", "git_diff"}
)
#: What a team seat needs to take part at all, and which G22 has no equivalent
#: of because Python does the sequencing there.
COLLABORATION_TOOLS = frozenset({"message_agent", "submit", "team_status"})
EDIT_TOOLS = frozenset({"apply_patch", "file_write"})
#: Named in every card only to say the team does not have it ("there is no
#: ``spawn_agent`` on this team"), so a mention is not a claim to hold it.
NAMED_AS_ABSENT = frozenset({"spawn_agent"})

CARD_FILES = {
    "adopter": "adopter.md",
    "coder_a": "coder-a.md",
    "coder_b": "coder-b.md",
}

MINIMAL_CODER_TASK = """\
Find the narrowest root cause that fully explains the issue. Preserve backward
compatibility and existing public behavior outside the requested change. Trace
the immediate callers and consumers needed to verify the fix, then implement a
minimal complete source patch. Avoid broad refactors, speculative cleanup, test
edits, generated files, caches, and logs. Run the nearest relevant public tests
with bash, inspect the final diff, and finish with a non-empty source diff.
Do not use official results, hidden tests, FAIL_TO_PASS ids, grader patches, or
historical outcomes."""

CROSS_COMPONENT_CODER_TASK = """\
Solve the issue end to end with emphasis on cross-component completeness.
Trace every producer and producing state or data path, every direct consumer, public API and
serialization contract, error propagation, lifecycle boundary, and relevant
edge cases. Implement the smallest patch that covers the whole contract while
preserving unrelated behavior. Avoid test edits, generated files, caches, and
logs. Run relevant public tests through Bash using the project's native test command. When the shared command is
available, execute the same native command without replacing
it with an easier test. Inspect the final diff and finish with a non-empty source
patch. Do not use official results, hidden tests, FAIL_TO_PASS ids, grader
patches, or historical outcomes."""

CONTRACT_OPENING = """\
Candidate A was instructed to make the narrowest compatible root-cause repair.
Candidate B was instructed to cover producer, consumer, API, lifecycle, and
edge-case contracts. You cannot edit, merge, or rerun either candidate."""

CONTRACT_JUDGING = """\
Enumerate every explicit behavior requirement in the public issue. For each
requirement, compare the actual A and B diffs against the relevant producer,
consumer, and public API behavior. Cite concrete changed paths and diff details.
Public test records are comparable only when target, runner, and command are
identical. Do not reward larger diffs, stylistic changes, or unsupported claims.
Choose B only when public evidence shows B covers at least one requirement A
does not cover and no requirement is better covered by A."""

#: ``SHARED_RULES`` minus its last line, which is the one documented inversion.
SHARED_RULES_HEAD = """\
Rules:
- Use public issue, repository, test, and documentation evidence only.
- Never use hidden grader data, official hidden tests, grader patches, or FAIL_TO_PASS IDs.
- Obey this role and its tools.
- Keep probes under /tmp/opencollab-validation-* and out of the patch.
- Report unavailable probes as not_run. Make the smallest source fix.
- Read-only roles do not search for write tools."""
NO_COMMIT_RULE = "- Do not run git commit."


@pytest.fixture(scope="module")
def team():
    return load_team_config(path=str(TEAM_FILE))


def _card(role: str) -> str:
    return (CARDS / CARD_FILES[role]).read_text(encoding="utf-8")


def test_the_cell_seats_g22_s_roster_over_g22_s_four_edges(team) -> None:
    assert team.entry == ENTRY
    assert sorted(team.roles) == sorted(ROLES)
    edges = {
        (sender, receiver)
        for sender, receivers in team.topology.edges.items()
        for receiver in receivers
    }
    assert edges == EDGES


@pytest.mark.parametrize("role", ["coder_a", "coder_b"])
def test_each_candidate_carries_g22_s_candidate_bundle_and_nothing_else(team, role) -> None:
    """The one number a cross-arm comparison rests on.

    A tool the workflow's candidate does not hold, or one it holds and this seat
    does not, makes the two arms differ in capability while reporting the same
    organization -- the defect this cell was written to remove.
    """
    granted = frozenset(team.roles[role].tools)
    assert granted - COLLABORATION_TOOLS == G22_CANDIDATE_TOOLS
    assert "run_tests" not in granted


def test_the_adjudicator_holds_no_tool_that_edits_a_file(team) -> None:
    """G22's adjudicator is ``tools=[]``. A team seat cannot be, because the
    tree it owns is the answer and someone has to check the winner out into it.
    ``bash`` is that concession and it is the only one: the seat holds neither
    ``apply_patch`` nor ``file_write``."""
    granted = frozenset(team.roles[ENTRY].tools)
    assert granted & EDIT_TOOLS == frozenset()
    assert "bash" in granted


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("coder_a", MINIMAL_CODER_TASK),
        ("coder_b", CROSS_COMPONENT_CODER_TASK),
        ("adopter", CONTRACT_OPENING),
        ("adopter", CONTRACT_JUDGING),
    ],
)
def test_g22_s_own_paragraphs_crossed_unedited(role: str, expected: str) -> None:
    assert expected in _card(role)


@pytest.mark.parametrize("role", ROLES)
def test_the_shared_rules_cross_with_exactly_one_documented_inversion(role: str) -> None:
    """``Do not run git commit.`` is the single rule this runtime forces to
    move: a commit is the only thing that crosses between worktrees, so the
    candidates must commit and the read-only seat still must not."""
    card = _card(role)
    assert SHARED_RULES_HEAD in card
    if role == ENTRY:
        assert NO_COMMIT_RULE in card
    else:
        assert NO_COMMIT_RULE not in card
        assert "Commit your finished work; that is how it reaches the Adopter." in card


@pytest.mark.parametrize("role", ROLES)
def test_a_card_names_only_tools_this_team_actually_has(team, role: str) -> None:
    """The card and the bundle are one declaration or the cell is broken: a seat
    reasoning from tools nobody holds invents a workaround, and the workaround
    becomes an artefact of the contradiction rather than a finding.

    A card may name a TEAMMATE's tool -- the adjudicator's card names the edit
    tools it does not have, which is the fact that tells it to delegate -- so
    the bound here is the team's tools, with the seat's own bundle pinned by
    ``test_the_adjudicator_holds_no_tool_that_edits_a_file`` above.
    """
    held_by_anyone = {tool for seat in team.roles.values() for tool in seat.tools}
    named = {
        name
        for name in re.findall(r"`([a-z_]+)`", _card(role))
        if name in KNOWN_TOOL_NAMES
    } - NAMED_AS_ABSENT
    assert named <= held_by_anyone, (
        f"{role} names {sorted(named - held_by_anyone)}, which no seat holds"
    )


def test_the_adjudicators_claim_about_the_candidates_bundles_is_true(team) -> None:
    """The one cross-role sentence in the adjudicator's card, against the real
    bundles. It is load-bearing: it is why the seat delegates rather than
    repairing the code itself."""
    adopter = set(team.roles[ENTRY].tools)
    claimed = EDIT_TOOLS
    assert "They hold the edit tools you do not:" in _card(ENTRY)
    assert claimed & adopter == set()
    for role in ("coder_a", "coder_b"):
        assert claimed <= set(team.roles[role].tools)


def test_the_two_candidate_cards_differ_only_where_g22_differs() -> None:
    """A and B are one seat twice unless their asks are different. G22 makes
    them different in exactly one place -- the task paragraph -- so a card that
    dropped that difference would turn this cell into best-of-2 with extra
    steps."""
    a, b = _card("coder_a"), _card("coder_b")
    assert MINIMAL_CODER_TASK in a and MINIMAL_CODER_TASK not in b
    assert CROSS_COMPONENT_CODER_TASK in b and CROSS_COMPONENT_CODER_TASK not in a
    assert "The shared public command" in b and "The shared public command" not in a
