"""What `configs/team.collab.yaml` promises, checked against what it configures.

This file is the reusable team: one self-contained YAML a caller names on a
command line, with every role's card inline. Two failure modes are worth a gate.

The first is a card that says something untrue about the run. A role told it
holds a tool it does not hold, or told a teammate holds one it does not, spends
turns reaching for something that is not there -- and the discrepancy is
invisible in the prose, because prose is not checked against the tool list
anywhere else. So every tool a card names in backticks is checked against the
tools that role actually receives, and the two cross-role claims the Analyst
makes about its teammates' bundles are checked against those bundles.

The second is drift in the sentence the file exists for. Delegation here comes
from a command that blocks the alternative; deleting only the prohibition drops
handoff from 3/3 runs to 1/3. That sentence, the imperative it qualifies, and
the absence of `spawn_agent` are pinned, so a later edit that softens the card
fails a test rather than quietly turning the team back into a solo Analyst.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from opencollab.bootstrap.team_config import load_team_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "configs" / "team.collab.yaml"
ROLES = ("analyst", "coder", "tester")
#: Backticked names in the cards that are prose, not tools.
NOT_TOOLS = frozenset(
    {
        "git",
        "git commit",
        "git checkout <sha>",
        "git log --all",
        "pytest",
        "git worktree list",
        "spawn_agent",
        "prebuild_team=True",
        "team.collab.yaml",
        "opencollab",
    }
)
TOOL_TOKEN = re.compile(r"`([a-z_]+)`")


@pytest.fixture(scope="module")
def team():
    return load_team_config(path=str(CONFIG))


def test_the_file_loads_as_a_three_role_team_entered_at_the_analyst(team) -> None:
    assert sorted(team.roles) == sorted(ROLES)
    assert team.entry == "analyst"


def test_every_ordered_pair_is_an_edge(team) -> None:
    """A prebuilt teammate has no join path, so a reply needs a declared edge."""
    edges = {role: sorted(team.topology.edges.get(role, ())) for role in ROLES}
    assert edges == {
        "analyst": ["coder", "tester"],
        "coder": ["analyst", "tester"],
        "tester": ["analyst", "coder"],
    }


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_spawn(team, role: str) -> None:
    """All three cards state that spawning is refused. Holding the tool would
    make that statement false rather than making spawning work: a prebuilt team
    refuses the call."""
    assert "spawn_agent" not in team.roles[role].tools


@pytest.mark.parametrize("role", ROLES)
def test_every_role_can_be_reached_and_can_reach_back(team, role: str) -> None:
    """A role with an outgoing edge and no `message_agent` is seated mute."""
    assert "message_agent" in team.roles[role].tools
    assert "team_status" in team.roles[role].tools


def _named_tools(text: str) -> set[str]:
    return {t for t in TOOL_TOKEN.findall(text) if t not in NOT_TOOLS}


def _own_capability_paragraph(prompt: str) -> str:
    """The card's first-person answer to "what can I do" -- the paragraph right
    after `## What you can do`. Later paragraphs describe teammates, so a tool
    named there is a claim about someone else's bundle, not about this role's."""
    body = prompt.partition("## What you can do")[2]
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    assert paragraphs, "card has no capability paragraph"
    return paragraphs[0]


@pytest.mark.parametrize("role", ROLES)
def test_a_card_claims_no_tool_its_role_does_not_hold(team, role: str) -> None:
    """The defect this catches: a card telling a role it can do something it
    was never given a tool for. It reads as instruction and cannot be acted on."""
    held = set(team.roles[role].tools)
    named = _named_tools(_own_capability_paragraph(team.roles[role].prompt))
    assert named, f"{role} card names no tool at all"
    assert named <= held, f"{role} card claims tools it does not hold: {sorted(named - held)}"


@pytest.mark.parametrize("role", ROLES)
def test_a_card_names_no_tool_that_exists_nowhere_on_this_team(team, role: str) -> None:
    """A card may name a teammate's tool -- the Analyst says what the Tester
    holds. It may not name one no seat has: that is a capability nobody can use."""
    on_team = {tool for cfg in team.roles.values() for tool in cfg.tools}
    named = _named_tools(team.roles[role].prompt)
    assert named <= on_team, f"{role} card names absent tools: {sorted(named - on_team)}"


def test_the_analyst_could_do_the_whole_task_alone(team) -> None:
    """Handing work over has to stay a thing the Analyst was told to do, not a
    thing it had no alternative to. A seat without edit tools delegates because
    it cannot do anything else, and the handoff then measures the config."""
    assert {"apply_patch", "file_write", "bash", "run_tests"} <= set(team.roles["analyst"].tools)


def test_the_analysts_claims_about_its_teammates_bundles_are_true(team) -> None:
    """Both cross-role sentences in the Analyst card, against the real bundles."""
    analyst = set(team.roles["analyst"].tools)
    assert set(team.roles["coder"].tools) == analyst  # "holds the same tools you do"
    # "the same minus the two that edit files, plus `git_diff`"
    assert set(team.roles["tester"].tools) == (analyst - {"apply_patch", "file_write"}) | {
        "git_diff"
    }


def test_the_tester_holds_nothing_that_edits_a_file(team) -> None:
    tools = set(team.roles["tester"].tools)
    assert not tools & {"apply_patch", "file_write"}


def test_the_analyst_is_commanded_and_the_alternative_is_blocked(team) -> None:
    """The load-bearing pair. Measured: with both, 3/3 runs hand work over;
    delete the prohibition alone and it is 1/3."""
    card = " ".join(team.roles["analyst"].prompt.split())
    assert "send the Coder a message with `message_agent` describing the change" in card
    assert "Do not apply the change yourself." in card
    assert "Verification is the Tester's" in card


def test_the_analyst_is_told_to_check_the_roster_before_working(team) -> None:
    """Without a prebuilt roster the teammates do not exist and no role can
    spawn them, so the run would otherwise be a silent solo run reported as a
    team's. The check turns that into something the output says."""
    card = " ".join(team.roles["analyst"].prompt.split())
    assert "Call `team_status` before anything else." in card
    assert "without a prebuilt roster" in card


def test_the_analyst_is_told_that_a_commit_reaches_the_answer_only_via_checkout(
    team,
) -> None:
    """Delivery is agent 0's tree. A run where the Coder commits and the Analyst
    never checks the sha out delivers nothing, and nothing else reports that."""
    card = " ".join(team.roles["analyst"].prompt.split())
    assert "git checkout <sha>" in card
    assert "delivers nothing" in card


# --- the runner script -------------------------------------------------------
#
# The team file states "the Coder and the Tester are already running" as a fact
# about the run, and no role holds `spawn_agent`. That fact is produced by one
# argument at the call site, `prebuild_team=True`, which the CLI has no flag
# for. Get it wrong and the run is not an error: the Analyst is seated alone and
# does the whole task, and the output looks like a team's. So the call the
# script makes is pinned here rather than left to the reader of a docstring.


def _runner_module():
    import importlib.util

    path = REPO_ROOT / "scripts" / "run_collab_team.py"
    spec = importlib.util.spec_from_file_location("run_collab_team", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _StubResult:
    output = "done"
    status = "completed"
    reason = None
    tokens = 1
    metrics: dict = {}
    artifacts = None
    ok = True


def _record_calls(module, monkeypatch) -> list[dict]:
    calls: list[dict] = []

    class _StubClient:
        def __init__(self, workspace, **kwargs):
            calls.append({"workspace": workspace, **kwargs})

        async def team(self, prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return _StubResult()

    monkeypatch.setattr(module, "OpenCollab", _StubClient)
    return calls


def test_the_runner_seats_the_roster_before_the_first_model_call(monkeypatch, tmp_path):
    module = _runner_module()
    calls = _record_calls(module, monkeypatch)
    assert module.main(["--workspace", str(tmp_path), "--prompt", "fix it"]) == 0
    team_call = calls[-1]
    assert team_call["prebuild_team"] is True
    assert team_call["use_worktrees"] is True
    assert team_call["serialize_turns"] is True
    assert team_call["prompt"] == "fix it"


def test_the_runner_defaults_to_the_reusable_team_file(monkeypatch, tmp_path):
    module = _runner_module()
    calls = _record_calls(module, monkeypatch)
    module.main(["--workspace", str(tmp_path), "--prompt", "fix it"])
    assert Path(calls[-1]["config"]) == CONFIG


def test_the_runner_reads_a_task_from_a_file(monkeypatch, tmp_path):
    module = _runner_module()
    calls = _record_calls(module, monkeypatch)
    issue = tmp_path / "issue.md"
    issue.write_text("the bug\n", encoding="utf-8")
    module.main(["--workspace", str(tmp_path), "--prompt-file", str(issue)])
    assert calls[-1]["prompt"] == "the bug\n"


def test_the_runner_leaves_the_shell_sandboxed_unless_asked(monkeypatch, tmp_path):
    """`bash` refuses to run with no OS sandbox, and the sha handoff is `git`.
    The flag has to exist for a host run and must not be the default."""
    module = _runner_module()
    calls = _record_calls(module, monkeypatch)
    module.main(["--workspace", str(tmp_path), "--prompt", "fix it"])
    assert calls[-1]["allow_unisolated_shell"] is None
    calls.clear()
    module.main(["--workspace", str(tmp_path), "--prompt", "fix it", "--allow-unisolated-shell"])
    assert calls[-1]["allow_unisolated_shell"] is True


@pytest.mark.parametrize("role", ("coder", "tester"))
def test_the_roles_that_run_tests_are_told_to_leave_no_ignored_files(team, role: str):
    """A teammate's worktree that holds an ignored file cannot have its changes
    read, and cleanup then raises *after* the answer is given: the team delivers
    a correct patch and the run still reports failed. `pytest` alone causes it."""
    card = " ".join(team.roles[role].prompt.split())
    assert "Leave nothing behind in your worktree that git would ignore." in card
    assert "-p no:cacheprovider" in card


def test_the_runner_keeps_test_caches_out_of_the_worktrees(monkeypatch, tmp_path):
    """The same failure, closed on the route this repository controls: the
    agents' shell inherits this process's environment."""
    module = _runner_module()
    _record_calls(module, monkeypatch)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    module.main(["--workspace", str(tmp_path), "--prompt", "fix it"])
    assert os.environ["PYTEST_ADDOPTS"] == "-p no:cacheprovider"
    assert os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_the_runner_does_not_override_a_caller_who_set_them(monkeypatch, tmp_path):
    module = _runner_module()
    _record_calls(module, monkeypatch)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
    module.main(["--workspace", str(tmp_path), "--prompt", "fix it"])
    assert os.environ["PYTEST_ADDOPTS"] == "-x"


def test_the_runner_reports_whether_work_was_handed_over(tmp_path):
    """A solo run and a delegating run are indistinguishable in a team's
    metrics, which carry only agent 0's steps and a seat count. The line the
    runner prints has to come from the transcripts or it says nothing."""
    module = _runner_module()
    (tmp_path / "agent_0_analyst-aaa.json").write_text(
        json.dumps(
            {
                "messages": [
                    {"tool_calls": [{"function": {"name": "message_agent"}}]},
                    {"tool_calls": [{"function": {"name": "apply_patch"}}]},
                    {"tool_calls": [{"function": {"name": "message_agent"}}]},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "agent_1_coder-bbb.json").write_text(
        json.dumps({"messages": [{"tool_calls": [{"function": {"name": "apply_patch"}}]}]}),
        encoding="utf-8",
    )

    class _Result:
        metrics = {"sessions": 3, "steps": 12}
        artifacts = tmp_path

    line = module._handoff_summary(_Result())
    assert "seats=3" in line
    assert "analyst=2" in line
    assert "coder=0" in line


def test_the_runner_says_so_when_it_cannot_count_handoffs(tmp_path):
    """Silence and zero are different answers; reporting zero without evidence
    is the failure this avoids."""
    module = _runner_module()

    class _Result:
        metrics = {"sessions": 3, "steps": 12}
        artifacts = None

    assert "handoffs=unknown" in module._handoff_summary(_Result())


def test_the_file_is_self_contained(tmp_path):
    """The point of inlining every card: copied on its own to an unrelated
    directory it still loads. A `prompt_file` would resolve against the team
    file's directory and this would fail."""
    copied = tmp_path / "my-team.yaml"
    copied.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    elsewhere = load_team_config(path=str(copied))
    assert sorted(elsewhere.roles) == sorted(ROLES)
    assert all(elsewhere.roles[r].prompt.strip() for r in ROLES)


def test_the_analyst_is_told_a_teammates_commit_is_not_in_its_own_log(team):
    """Measured failure this closes: the Coder committed, the Tester checked the
    sha out and the suite passed, and the Analyst ran `git log` -- which does not
    reach a commit held only by another worktree's detached HEAD -- concluded
    nothing had been delivered, and spent its remaining budget on `sleep` waiting
    for a commit that already existed."""
    card = " ".join(team.roles["analyst"].prompt.split())
    assert "The sha you were sent is enough on its own: check it out." in card
    assert "only `git log --all` reaches it" in card
