"""A team file names the context policy every seat runs under.

Until now the shaper pipeline a seat ran was fixed by the code: every team
session took the default lazy-degradation pipeline (or its profile's), and
only a caller holding ``build_session(shaper=)`` could change it. A team file
could not say which context policy its run used, so a run's context handling
was recoverable only from the version of the code it ran on.

What is pinned here: an omitted ``context`` is exactly today's pipeline; a
named policy reaches every seat, entry agent and teammates alike; an unknown
name or key is refused at load; and the session record names the policy.
"""

from __future__ import annotations

import pytest

from opencollab.application.event_bus import EventBus
from opencollab.application.shaping import PerToolResultBudgetShaper
from opencollab.bootstrap.container import _build_default_shaper, build_session_runtime
from opencollab.bootstrap.context_builder import SpawnConfig
from opencollab.bootstrap.context_policy import ContextPolicy, resolve_context_policy
from opencollab.bootstrap.team_config import RoleConfig, TeamConfig, load_team_config
from opencollab.domain.agent import Agent
from opencollab.domain.team import Topology
from tests.support.session_run_loop_test_support import FakeTracer

SCHED = object()


class _Llm:
    def __init__(self, window=1_000_000):
        self._window = window

    def context_window(self):
        return self._window

    async def complete(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("no model call is made while building a session")


def _write(tmp_path, body: str):
    path = tmp_path / "team.yaml"
    path.write_text("entry: solo\nroles:\n  solo:\n    prompt: hi\n" + body, encoding="utf-8")
    return path


def _layers(shaper) -> list[type]:
    return [type(layer) for layer in shaper._shapers]


# --- Declaration and validation ------------------------------------------------


def test_an_omitted_context_is_the_default_policy(tmp_path) -> None:
    team = load_team_config(path=str(_write(tmp_path, "")))
    assert team.context == ContextPolicy()
    assert team.context.name == "default"


def test_a_policy_may_be_named_or_given_parameters(tmp_path) -> None:
    named = load_team_config(path=str(_write(tmp_path, "context: no_history_compaction\n")))
    assert named.context == ContextPolicy(name="no_history_compaction")

    detailed = load_team_config(
        path=str(
            _write(
                tmp_path,
                "context:\n  policy: no_history_compaction\n  tool_result_budget: 8000\n",
            )
        )
    )
    assert detailed.context == ContextPolicy(name="no_history_compaction", tool_result_budget=8_000)


@pytest.mark.parametrize(
    "body",
    [
        "context: summarize_everything\n",
        "context:\n  policy: default\n  keep_recent: 3\n",
        "context:\n  tool_result_budget: 0\n",
        "context:\n  tool_result_budget: true\n",
        "context: 3\n",
    ],
    ids=["unknown-name", "unknown-key", "zero-budget", "boolean-budget", "not-a-name"],
)
def test_an_unknown_policy_is_refused_at_load(tmp_path, body) -> None:
    with pytest.raises(ValueError):
        load_team_config(path=str(_write(tmp_path, body)))


def test_the_policy_names_are_normalized() -> None:
    assert resolve_context_policy(" No-History-Compaction ").name == "no_history_compaction"
    assert resolve_context_policy(None) == ContextPolicy()


# --- What a policy builds -------------------------------------------------------


def _shaper(policy: ContextPolicy | None):
    runtime = build_session_runtime(
        agent=Agent(name="coder", system_prompt="sys", model="fake-model"),
        llm=_Llm(),
        context_policy=policy,
    )
    return runtime.runner.shaper


def test_the_default_policy_builds_todays_pipeline() -> None:
    """The regression that matters: runs already made used exactly this."""
    today = _layers(_build_default_shaper(_Llm(), lambda _: "summary"))
    assert _layers(_shaper(None)) == today
    assert _layers(_shaper(ContextPolicy())) == today


def test_no_history_compaction_keeps_only_the_per_result_budget() -> None:
    shaper = _shaper(ContextPolicy(name="no_history_compaction", tool_result_budget=8_000))
    assert _layers(shaper) == [PerToolResultBudgetShaper]
    assert shaper._shapers[0].max_chars == 8_000


def test_the_session_record_names_the_policy() -> None:
    tracer = FakeTracer()
    build_session_runtime(
        agent=Agent(name="coder", system_prompt="sys", model="fake-model"),
        llm=_Llm(),
        tracer=tracer,
        context_policy=ContextPolicy(name="no_history_compaction"),
    )
    record = next(s["payload"] for s in tracer.steps if s["step_type"] == "session.history_compaction")
    assert record["context_policy"] == "no_history_compaction"
    # No history layer runs, so no threshold is in force.
    assert record["history_trigger_tokens"] is None
    assert record["history_thresholds_from"] == "disabled_by_context_policy"


# --- Every seat ------------------------------------------------------------------


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


def test_every_seat_carries_the_teams_policy(monkeypatch, tmp_path) -> None:
    from opencollab.application.scheduler import LaunchSpec
    from opencollab.bootstrap import session_factory

    policy = ContextPolicy(name="no_history_compaction")
    team = TeamConfig(
        roles={
            "adopter": RoleConfig(prompt="Adopter card.", model=None, tools=["message_agent"]),
            "coder": RoleConfig(prompt="Coder card.", model=None, tools=["message_agent"]),
        },
        entry="adopter",
        topology=Topology(edges={"adopter": frozenset({"coder"}), "coder": frozenset({"adopter"})}),
        context=policy,
    )
    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen[kwargs["agent"].name] = kwargs.get("context_policy")
        return object()

    monkeypatch.setattr(session_factory, "build_session", capture)
    factory = session_factory.DefaultSessionFactory(_cfg(), team_cfg=team, lead_workspace=str(tmp_path))
    factory.create_lead_session(
        scheduler=SCHED, launch=LaunchSpec(session_file=None, auto_save_path=None), budget=1_000,
    )
    assert seen["adopter"] == policy
