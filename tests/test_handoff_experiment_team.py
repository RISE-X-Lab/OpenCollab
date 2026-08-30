"""The shipped experimental team: three peers that can hand work over git.

``configs/team.handoff.experiment.yaml`` exists to measure whether models hand
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

from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
TEAM_FILE = REPO_ROOT / "configs" / "team.handoff.experiment.yaml"
ROLES = ("analyst", "coder", "tester")


@pytest.fixture(scope="module")
def team():
    return load_team_config(path=str(TEAM_FILE))


def test_the_entry_is_the_analyst_and_the_roster_is_the_three_roles(team) -> None:
    assert team.entry == "analyst"
    assert sorted(team.roles) == sorted(ROLES)


def test_every_ordered_pair_is_a_declared_edge(team) -> None:
    """Six edges, including the three that point back.

    The shipped Self-Collaboration team is a one-way star — the Analyst reaches
    the Coder and the Tester, and neither reaches anything. Under a prebuilt
    team that is fatal rather than merely restrictive: a prebuilt peer has no
    join path, so an edgeless role has no way to return anything at all.
    """
    walked = {
        (source, destination)
        for source in ROLES
        for destination in ROLES
        if team.topology.allows(source, destination)
    }
    assert walked == {
        (source, destination)
        for source in ROLES
        for destination in ROLES
        if source != destination
    }
    # No self-edges: the diagonal is not something the config declares.
    assert not any(team.topology.allows(role, role) for role in ROLES)


def test_every_role_can_walk_its_edges(team) -> None:
    """``message_agent`` is the only channel a prebuilt team leaves open."""
    for role in ROLES:
        assert "message_agent" in team.roles[role].tools, role
        # Resolving a teammate's aid is what makes message_agent usable.
        assert "team_status" in team.roles[role].tools, role


def test_the_two_halves_of_the_git_handoff_are_both_runnable(team) -> None:
    """One side has to be able to ``git commit``, the other to ``git checkout``.

    Both go through ``bash``. The shipped Tester has no ``bash``, so it could
    not check out a sha it was handed no matter what it was told.
    """
    assert "bash" in team.roles["coder"].tools
    assert "bash" in team.roles["tester"].tools


def test_no_role_carries_a_tool_that_can_only_be_refused(team) -> None:
    """A prebuilt team refuses ``spawn_agent`` for the whole run.

    The shipped team offers it because that is how the Analyst delegates when
    the roster is dynamic. Here it would only produce a refusal, so it is not
    offered — the delegation this run is watching for has to happen over the
    message channel or not at all.
    """
    for role in ROLES:
        assert "spawn_agent" not in team.roles[role].tools, role
        assert "spawn_with_review" not in team.roles[role].tools, role


def test_each_role_prompt_is_loaded_from_its_own_file(team) -> None:
    prompts = REPO_ROOT / "configs" / "handoff-experiment"
    for role in ROLES:
        body = (prompts / f"{role}.md").read_text(encoding="utf-8")
        assert team.roles[role].prompt == body
        # The two facts a handoff is impossible without: that a commit's sha
        # travels on its own, and that the other side checks it out.
        assert "sha" in body
        assert "git checkout <sha>" in body


def test_the_shipped_default_team_is_not_this_team() -> None:
    """This file must never become the product default.

    ``load_team_config`` with no path returns the built-in Self-Collaboration
    team; an experimental config is reached only by naming it.
    """
    from opencollab.bootstrap.team_config import default_team_config

    shipped = load_team_config()
    assert shipped == default_team_config()
    assert "message_agent" not in shipped.roles["analyst"].tools


def test_the_analyst_could_finish_the_task_without_anyone(team) -> None:
    """Delegation has to be a choice, or it is a fact about this file.

    The shipped Analyst reads and delegates and holds no tool that edits a file
    or runs a command. On a prebuilt team that starves it into messaging: every
    handoff it makes is one it had no alternative to, so "the model delegated"
    stops being evidence that the model collaborates. The same starvation makes
    the Analyst weaker than the single agent this arm is compared against, so a
    difference between the arms could be read off the tool bundles alone.

    Here the Analyst holds the working tools, and messaging is one option among
    others. Every one of these tools is load-bearing for that: reading is not
    doing, so ``file_read``/``grep`` alone would leave the starvation in place.
    """
    analyst = set(team.roles["analyst"].tools)
    for doing_tool in ("apply_patch", "bash", "run_tests"):
        assert doing_tool in analyst, (
            f"the Analyst cannot {doing_tool} and so cannot finish alone; "
            "delegation would be forced rather than chosen"
        )


def test_no_capability_is_reachable_only_through_a_teammate(team) -> None:
    """The Analyst's options are a superset of what delegating would buy it.

    If the Coder or the Tester held a working tool the Analyst lacks, sending
    them work would be the only way to use that tool, and the arm would measure
    a tool gap rather than a decision.
    """
    working_tools = {"apply_patch", "bash", "run_tests"}
    analyst = set(team.roles["analyst"].tools)
    for role in ("coder", "tester"):
        exclusive = (set(team.roles[role].tools) & working_tools) - analyst
        assert not exclusive, f"only the {role} can {sorted(exclusive)}"


#: The single-agent arm's tool list, duplicated here because it lives in the
#: other repository — ``WORKING_TOOL_NAMES`` in OpenCollab-Eval's
#: ``generation/gen_prediction_constants.py``, which is what that arm passes to
#: ``builtin_tools``. Both sides pin it, so a change on either fails a test.
SINGLE_AGENT_WORKING_TOOLS = frozenset(
    {"apply_patch", "bash", "file_read", "file_write", "grep", "run_tests", "submit"}
)

#: Everything else a role on this team may hold. The collaboration channel is
#: what this arm is supposed to differ by; ``git_diff`` is how a role with no
#: edit tool reads what someone else changed.
COLLABORATION_TOOLS = frozenset({"git_diff", "message_agent", "team_status"})


def test_the_analyst_holds_the_single_agent_s_tools_and_the_channel(team) -> None:
    """Exactly, not merely a capable superset.

    This file's own comment already claimed the Analyst "carries the single
    agent's working tools", and it did not: the single agent has ``file_write``
    and no ``apply_patch``, the Analyst had ``apply_patch`` and no
    ``file_write``, and the Analyst additionally held ``run_tests``,
    ``ask_user`` and ``use_skill``. Four capability differences on the axis this
    arm is not supposed to differ on, described in prose as none.

    A superset is not good enough either. If the Analyst can do something the
    single agent cannot, a difference in outcome can be read off the bundles
    rather than off how the work was organized -- which is the reading this
    whole configuration exists to rule out.
    """
    analyst = set(team.roles["analyst"].tools)

    assert analyst - COLLABORATION_TOOLS == set(SINGLE_AGENT_WORKING_TOOLS)
    assert analyst & COLLABORATION_TOOLS == {"message_agent", "team_status"}


def test_no_role_holds_a_tool_that_cannot_succeed_here(team) -> None:
    """``ask_user`` and ``use_skill`` are not merely unused, they cannot work.

    Nobody is at an unattended benchmark run to answer a question, and no skill
    package is shipped into the task container. A role holding either can only
    spend a turn discovering that, and only one arm held them.
    """
    for role in ROLES:
        tools = set(team.roles[role].tools)
        assert "ask_user" not in tools, role
        assert "use_skill" not in tools, role


def test_every_role_is_told_which_tree_is_read_as_the_answer() -> None:
    """An empty patch has to be attributable to the model, not to not knowing.

    Only the repository the Analyst works in is collected when the run ends; the
    Coder's and the Tester's worktrees are not. An agent that leaves its work in
    one of those submits nothing, and without this fact stated the run cannot
    tell "the model chose not to bring the work back" from "the model was never
    told where back is".
    """
    prompts = REPO_ROOT / "configs" / "handoff-experiment"
    for role in ROLES:
        body = " ".join((prompts / f"{role}.md").read_text(encoding="utf-8").split())
        assert "read as the answer" in body, role


def test_every_role_is_told_that_finishing_is_how_you_wait() -> None:
    """Waiting has a free form, and a model that misses it pays in budget.

    ``message_agent`` returns immediately, so a role that has handed work over
    and has nothing left to do has to choose what to do with its turn. Two of
    the three available moves are wrong: polling ``team_status`` until the
    answer lands bills a whole conversation to the provider per poll, and
    treating the exchange as over abandons a teammate's work. The third is
    free -- a delivered message reopens a finished turn -- but nothing about
    the tool's behaviour reveals it, so each card says it.
    """
    prompts = REPO_ROOT / "configs" / "handoff-experiment"
    for role in ROLES:
        body = " ".join((prompts / f"{role}.md").read_text(encoding="utf-8").split())
        assert "finishing is how you wait" in body, role


# --- The Analyst-card variable ------------------------------------------------
#
# How hard the Analyst's card leans on handing work over is a treatment with
# three declared levels, not a wording detail. The first measured batches ran on
# a card that told the Analyst it could carry the request end to end without
# involving anyone, and delegation never happened in any of them; a delegation
# rate read off that card is a fact about the card. So the levels are named,
# they differ in one block and nothing else, and that is pinned here.

PROMPTS = REPO_ROOT / "configs" / "handoff-experiment"
JUDGE_HEADING = "## What is yours to judge"
#: level -> (analyst card, team file). ``primary`` keeps the unsuffixed names
#: because it is the level every reported quantity is measured on.
LEVELS = {
    "primary": ("analyst.md", "team.handoff.experiment.yaml"),
    "weak": ("analyst.weak.md", "team.handoff.weak.yaml"),
    "strong": ("analyst.strong.md", "team.handoff.strong.yaml"),
}


def _analyst_card(level: str) -> str:
    return (PROMPTS / LEVELS[level][0]).read_text(encoding="utf-8")


def _split_on_judge_block(card: str) -> tuple[str, str]:
    """Everything outside the judge section, and the judge section itself."""
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
    shared = {level: _split_on_judge_block(_analyst_card(level))[0] for level in LEVELS}
    assert len(set(shared.values())) == 1, {k: len(v) for k, v in shared.items()}


def test_the_three_levels_are_actually_three_different_levels() -> None:
    """Three files that happen to say the same thing are one level, not three."""
    blocks = {level: _split_on_judge_block(_analyst_card(level))[1] for level in LEVELS}
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
    block = " ".join(_split_on_judge_block(_analyst_card("primary"))[1].split()).lower()
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
    block = " ".join(_split_on_judge_block(_analyst_card("strong"))[1].split()).lower()
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
