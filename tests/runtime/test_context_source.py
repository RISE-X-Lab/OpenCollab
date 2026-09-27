"""Unit tests for the layered context domain (ContextSource / ContextPlan)."""

from __future__ import annotations

from opencollab.domain.context import (
    LAYER_PRIORITY,
    ContextLayer,
    ContextPlan,
    ContextPosition,
    ContextSource,
)


def _src(name, layer, position, content="", priority=None):
    return ContextSource(
        name=name,
        layer=layer,
        position=position,
        content=content,
        priority=priority,
    )


def test_system_prompt_joins_only_system_sources_in_order():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, ContextPosition.SYSTEM, "I am lead."),
            _src("team", ContextLayer.TEAM, ContextPosition.SYSTEM, "## Your team"),
            _src("task", ContextLayer.TASK, ContextPosition.USER_CONTEXT, "do the thing"),
        )
    )
    assert plan.system_prompt() == "I am lead.\n\n## Your team"
    assert plan.startup_system_messages() == [
        {
            "role": "system",
            "content": "I am lead.",
            "_ctx": {"name": "identity", "layer": "identity", "priority": 100},
        },
        {
            "role": "system",
            "content": "## Your team",
            "_ctx": {"name": "team", "layer": "team", "priority": 90},
        },
    ]


def test_startup_user_messages_take_only_user_context_sources():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, ContextPosition.SYSTEM, "I am lead."),
            _src("task", ContextLayer.TASK, ContextPosition.USER_CONTEXT, "do the thing"),
        )
    )
    assert plan.startup_user_messages() == [
        {
            "role": "user",
            "content": "do the thing",
            "_ctx": {
                "name": "task",
                "layer": "task",
                "priority": LAYER_PRIORITY[ContextLayer.TASK],
            },
        }
    ]


def test_empty_content_sources_are_skipped_from_both_channels():
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, ContextPosition.SYSTEM, "I am lead."),
            # empty content: skipped everywhere — no registered-but-empty leak.
            _src("team", ContextLayer.TEAM, ContextPosition.SYSTEM, ""),
            _src("blank-task", ContextLayer.TASK, ContextPosition.USER_CONTEXT, ""),
        )
    )
    assert plan.system_prompt() == "I am lead."
    assert plan.startup_user_messages() == []


def test_assembly_is_generic_over_position_not_layer():
    # A source assembles purely by its POSITION: a USER_CONTEXT source becomes a
    # user message regardless of layer, proving assembly never special-cases a
    # known layer.
    plan = ContextPlan(
        sources=(
            _src("identity", ContextLayer.IDENTITY, ContextPosition.SYSTEM, "sys"),
            _src("proj", ContextLayer.PROJECT, ContextPosition.USER_CONTEXT, "project conventions here"),
        )
    )
    assert plan.system_prompt() == "sys"
    assert plan.startup_user_messages() == [
        {
            "role": "user",
            "content": "project conventions here",
            "_ctx": {
                "name": "proj",
                "layer": "project",
                "priority": LAYER_PRIORITY[ContextLayer.PROJECT],
            },
        }
    ]


def test_effective_priority_falls_back_to_layer_default_then_override():
    base = _src("proj", ContextLayer.PROJECT, ContextPosition.USER_CONTEXT, "p")
    assert base.effective_priority == LAYER_PRIORITY[ContextLayer.PROJECT]
    pinned = _src(
        "vip-project", ContextLayer.PROJECT, ContextPosition.USER_CONTEXT, "p",
        priority=95,  # explicit override beats the layer default
    )
    assert pinned.effective_priority == 95


def test_startup_user_messages_stamp_layer_and_priority():
    plan = ContextPlan(
        sources=(
            _src("project", ContextLayer.PROJECT, ContextPosition.USER_CONTEXT, "p"),
            _src("task", ContextLayer.TASK, ContextPosition.USER_CONTEXT, "t"),
        )
    )
    tags = [m["_ctx"] for m in plan.startup_user_messages()]
    assert tags == [
        {
            "name": "project",
            "layer": "project",
            "priority": LAYER_PRIORITY[ContextLayer.PROJECT],
        },
        {
            "name": "task",
            "layer": "task",
            "priority": LAYER_PRIORITY[ContextLayer.TASK],
        },
    ]
