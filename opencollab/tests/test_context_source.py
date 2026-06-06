"""Unit tests for the layered context domain (ContextSource / ContextPlan)."""

from __future__ import annotations

from opencollab.domain.context import (
    ContextLayer,
    ContextPlan,
    ContextPosition,
    ContextSource,
    LoadTiming,
)


def _src(name, layer, timing, position, content="", loader_key=None):
    return ContextSource(
        name=name,
        layer=layer,
        timing=timing,
        position=position,
        content=content,
        loader_key=loader_key,
    )


def test_system_prompt_joins_only_startup_system_sources_in_order():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, LoadTiming.STARTUP, ContextPosition.SYSTEM, "I am lead."),
            _src("team", ContextLayer.TEAM, LoadTiming.STARTUP, ContextPosition.SYSTEM, "## Your team"),
            _src("task", ContextLayer.TASK, LoadTiming.STARTUP, ContextPosition.USER_CONTEXT, "do the thing"),
        )
    )
    assert plan.system_prompt() == "I am lead.\n\n## Your team"


def test_startup_user_messages_take_only_user_context_startup_sources():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, LoadTiming.STARTUP, ContextPosition.SYSTEM, "I am lead."),
            _src("task", ContextLayer.TASK, LoadTiming.STARTUP, ContextPosition.USER_CONTEXT, "do the thing"),
        )
    )
    assert plan.startup_user_messages() == [{"role": "user", "content": "do the thing"}]


def test_messages_is_system_then_user_context():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, LoadTiming.STARTUP, ContextPosition.SYSTEM, "I am lead."),
            _src("task", ContextLayer.TASK, LoadTiming.STARTUP, ContextPosition.USER_CONTEXT, "do the thing"),
        )
    )
    assert plan.messages() == [
        {"role": "system", "content": "I am lead."},
        {"role": "user", "content": "do the thing"},
    ]


def test_deferred_and_empty_sources_do_not_enter_messages():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, LoadTiming.STARTUP, ContextPosition.SYSTEM, "I am lead."),
            # registered but on-demand: contributes no content this period
            _src(
                "memory", ContextLayer.MEMORY, LoadTiming.ON_DEMAND,
                ContextPosition.USER_CONTEXT, loader_key="memory",
            ),
            # startup but empty content: skipped
            _src("team", ContextLayer.TEAM, LoadTiming.STARTUP, ContextPosition.SYSTEM, ""),
        )
    )
    assert plan.startup_user_messages() == []
    assert plan.system_prompt() == "I am lead."
    assert {s.name for s in plan.deferred_sources()} == {"memory"}


def test_assembly_is_generic_over_position_not_layer():
    # A brand-new, never-before-seen layer assembles correctly with no code
    # change: a USER_CONTEXT/STARTUP source becomes a user message purely by its
    # position, proving messages() does not special-case known layers.
    novel = ContextSource(
        name="novel",
        layer=ContextLayer.PROJECT,  # stand-in for "some new layer"
        timing=LoadTiming.STARTUP,
        position=ContextPosition.USER_CONTEXT,
        content="project conventions here",
    )
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, LoadTiming.STARTUP, ContextPosition.SYSTEM, "sys"),
            novel,
        )
    )
    assert plan.messages() == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "project conventions here"},
    ]
