"""Unit tests for the skill domain value object + the SKILL context layer."""

from __future__ import annotations

import dataclasses

import pytest

from opencollab.domain.context import LAYER_PRIORITY, ContextLayer
from opencollab.domain.skill import SkillManifest


def test_skill_manifest_holds_name_and_description():
    m = SkillManifest(name="debug-flaky-tests", description="Find and fix flaky tests.")
    assert m.name == "debug-flaky-tests"
    assert m.description == "Find and fix flaky tests."


def test_skill_manifest_is_frozen():
    m = SkillManifest(name="x", description="y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.name = "z"  # type: ignore[misc]


def test_skill_manifest_is_hashable_and_value_equal():
    a = SkillManifest(name="x", description="y")
    b = SkillManifest(name="x", description="y")
    assert a == b
    assert hash(a) == hash(b)  # frozen dataclasses are hashable


def test_context_layer_skill_exists_with_priority_85():
    assert ContextLayer.SKILL.value == "skill"
    assert LAYER_PRIORITY[ContextLayer.SKILL] == 85


def test_skill_priority_sits_between_team_and_task():
    # Semantic ordering sanity: infrastructure catalog ranks just under TEAM.
    assert (
        LAYER_PRIORITY[ContextLayer.TASK]
        < LAYER_PRIORITY[ContextLayer.SKILL]
        < LAYER_PRIORITY[ContextLayer.TEAM]
    )
