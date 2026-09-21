"""A team seat may run under an agent profile, and both halves of it arrive.

A profile is four things at once: a base system prompt, the tool output caps
its agent was evaluated with, a history shaper and a safety wrapper. Until
now a team file could take none of them — profiles reached only the SDK's
single agent and the workflow runtime — so a team whose seats were meant to be
"that agent, organized" could copy its words and nothing else.

What is pinned here is that a seat declaring ``profile: single2`` gets all four,
that the profile leads and the role card follows rather than replacing it, and
that a role declaring nothing is exactly what it was before.
"""

from __future__ import annotations

import pytest

from opencollab.adapters.tools.single2 import SINGLE2_BASH_OUTPUT_CHARS
from opencollab.application.event_bus import EventBus
from opencollab.bootstrap.agent_profiles import resolve_agent_profile
from opencollab.bootstrap.context_builder import ContextBuilder, SpawnConfig
from opencollab.bootstrap.single2_prompt import SINGLE2_SYSTEM_PROMPT
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig, load_team_config
from opencollab.domain.team import Topology
from opencollab.teams import declared_role_profiles

#: Single2's six tools in the order it was evaluated with, plus the three a
#: team seat needs to take part at all. The order is the file's, and it is not
#: alphabetical on purpose — a tool list reaches the model in this order.
SEAT_TOOLS = [
    "bash", "file_read", "file_write", "apply_patch", "git_diff", "grep",
    "message_agent", "team_status", "submit",
]


#: The tools only store the scheduler they are bound to; nothing here calls it.
SCHED = object()


def _cfg():
    return SpawnConfig(
        model="default-model",
        provider="openai",
        api_key="k",
        base_url=None,
        llm_timeout=600.0,
        tracer=None,
        event_bus=EventBus(None),
        permission_policy=None,
    )


def _team(profile: str | None, *, tool_limits: dict | None = None) -> TeamConfig:
    return TeamConfig(
        roles={
            "adopter": RoleConfig(
                prompt="Adopter card.", model=None, tools=list(SEAT_TOOLS),
                profile=profile,
            ),
            "coder_a": RoleConfig(
                prompt="Coder A card.", model=None, tools=list(SEAT_TOOLS),
                profile=profile,
            ),
        },
        entry="adopter",
        topology=Topology(edges={"adopter": frozenset({"coder_a"}),
                                "coder_a": frozenset({"adopter"})}),
        tool_limits=tool_limits or {},
    )


def _builder(profile: str | None, **kwargs) -> ContextBuilder:
    return ContextBuilder(_team(profile, **kwargs), _cfg())


# --- The prompt ----------------------------------------------------------------


def test_the_profile_leads_and_the_card_follows() -> None:
    """The seat is that profile's agent first and this team's role second."""
    prompt = _builder("single2").build_plan("adopter").system_prompt()
    assert prompt.startswith(SINGLE2_SYSTEM_PROMPT)
    card_at = prompt.index("Adopter card.")
    assert card_at > len(SINGLE2_SYSTEM_PROMPT)
    assert prompt.index("## Your team") > card_at


def test_the_profile_prompt_is_carried_byte_for_byte() -> None:
    """Not paraphrased, not re-wrapped: the same text the SDK path seats.

    The point of seating a profile is that the agent is the evaluated one. A
    copy that drifted by a word would be a different agent wearing its name,
    and nothing in a run would say so.
    """
    sources = _builder("single2").build_plan("adopter").sources
    profile_source = next(s for s in sources if s.name == "profile")
    assert profile_source.content == SINGLE2_SYSTEM_PROMPT


def test_a_role_without_a_profile_is_unchanged() -> None:
    """The regression that matters: every existing team file still reads the same."""
    plan = _builder(None).build_plan("adopter")
    assert [s.name for s in plan.sources] == ["identity", "team"]
    assert plan.system_prompt().startswith("Adopter card.")


# --- The tools -----------------------------------------------------------------


def test_the_seat_gets_the_profiles_output_caps() -> None:
    """Single2's 10,000-character `bash` result, not OpenCollab's default.

    A seat that took the profile's words while keeping OpenCollab's caps would
    read more of every command's output than the agent being mirrored did, and
    the difference would land in the context budget rather than in an error.
    """
    agent = _builder("single2").build_agent("adopter", scheduler=SCHED)
    bash = next(tool for tool in agent.tools if tool.name == "bash")
    assert bash.max_output_chars == SINGLE2_BASH_OUTPUT_CHARS

    plain = _builder(None).build_agent("adopter", scheduler=SCHED)
    plain_bash = next(tool for tool in plain.tools if tool.name == "bash")
    assert plain_bash.max_output_chars != SINGLE2_BASH_OUTPUT_CHARS


def test_a_team_file_may_still_state_its_own_cap() -> None:
    """An explicit limit beats the profile default, as it does on the SDK path."""
    builder = _builder("single2", tool_limits={"bash": {"max_output_chars": 4_242}})
    bash = next(t for t in builder.build_agent("adopter", scheduler=SCHED).tools if t.name == "bash")
    assert bash.max_output_chars == 4_242


def test_the_declared_tool_order_survives() -> None:
    """The order a seat's tools reach the model is the file's, profile or not."""
    agent = _builder("single2").build_agent("adopter", scheduler=SCHED)
    assert [tool.name for tool in agent.tools] == SEAT_TOOLS


# --- The other half: shaper and safety -----------------------------------------


def test_both_seats_carry_the_profile_into_their_session(monkeypatch, tmp_path) -> None:
    """The shaper and the safety wrapper come from the profile too.

    ``ContextBuilder`` folds in the prompt and the caps; the session runtime
    applies the other two. They read one declaration, so a seat cannot take a
    profile's words while running OpenCollab's own history handling.
    """
    from opencollab.application.scheduler import LaunchSpec
    from opencollab.bootstrap import session_factory

    def _launch() -> LaunchSpec:
        return LaunchSpec(session_file=None, auto_save_path=None)

    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen[kwargs["agent"].name] = kwargs.get("agent_profile")
        return object()

    monkeypatch.setattr(session_factory, "build_session", capture)
    factory = session_factory.DefaultSessionFactory(
        _cfg(), team_cfg=_team("single2"), lead_workspace=str(tmp_path),
    )
    factory.create_lead_session(scheduler=SCHED, launch=_launch(), budget=1_000)
    assert seen["adopter"] is not None
    assert seen["adopter"].name == "single2"


# --- Declaration and validation ------------------------------------------------


def test_an_unknown_profile_is_refused_at_load(tmp_path) -> None:
    """Fail at startup, not by seating an agent nobody asked for."""
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: solo\nroles:\n  solo:\n    prompt: hi\n    profile: single3\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="single2"):
        load_team_config(path=str(path))


def test_default_means_opencollabs_own_agent(tmp_path) -> None:
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: solo\nroles:\n  solo:\n    prompt: hi\n    profile: default\n",
        encoding="utf-8",
    )
    assert load_team_config(path=str(path)).roles["solo"].profile is None
    assert resolve_agent_profile(None) is None


def test_a_team_file_reports_the_profile_each_seat_declares(tmp_path) -> None:
    """A run recording the card digest alone cannot name its condition.

    The card is what a treatment varies; the profile is the base that card is
    appended to. Two runs of the same card under different profiles are two
    different agents doing the same job.
    """
    path = tmp_path / "team.yaml"
    path.write_text(
        "entry: adopter\n"
        "roles:\n"
        "  adopter:\n    prompt: a\n    profile: single2\n"
        "  coder_a:\n    prompt: b\n",
        encoding="utf-8",
    )
    assert declared_role_profiles(str(path)) == {
        "adopter": "single2", "coder_a": None,
    }
