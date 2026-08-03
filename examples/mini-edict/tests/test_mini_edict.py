from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from opencollab.bootstrap.team_config import load_team_config
from opencollab.bootstrap.workflow_runtime import load_workflow_specs

WORKFLOW = (
    Path(__file__).parents[1]
    / "workflows/three_departments_six_ministries.py"
)
TEAM = WORKFLOW.parents[1] / "team.yaml"
README = WORKFLOW.parents[1] / "README.md"
README_ZH_CN = WORKFLOW.parents[1] / "README.zh-CN.md"


class RecordingContext:
    def __init__(self, reviews: list[dict[str, Any]] | None = None, fail_label: str | None = None) -> None:
        self.labels: list[str] = []
        self.phases: list[str] = []
        self.parallel_widths: list[int] = []
        self.logs: list[str] = []
        self.reviews = iter(reviews or [
            {"verdict": "approve", "findings": []},
            {"verdict": "approve", "findings": []},
        ])
        self.fail_label = fail_label

    async def agent(self, prompt: str, *, label: str, schema: dict[str, Any] | None = None, **_: Any) -> Any:
        self.labels.append(label)
        if label == self.fail_label:
            return None
        if label == "Menxia Chancellery":
            return next(self.reviews)
        if label == "Shangshu Department" and schema:
            return {
                "summary": "Two ministries are relevant",
                "assignments": [
                    {"ministry": "War", "task": "Build it", "acceptance": "Tests pass"},
                    {"ministry": "Justice", "task": "Verify it", "acceptance": "Evidence is complete"},
                ],
            }
        return f"{label} output"

    async def parallel(self, thunks: list[Any]) -> list[Any]:
        self.parallel_widths.append(len(thunks))
        return [await thunk() for thunk in thunks]

    async def phase(self, title: str) -> None:
        self.phases.append(title)

    async def log(self, message: str) -> None:
        self.logs.append(message)

    def tokens_spent(self) -> int:
        return 123


def test_demo_revises_then_routes_only_relevant_ministries() -> None:
    ctx = RecordingContext([
        {"verdict": "revise", "findings": ["Add a rollback plan"]},
        {"verdict": "approve", "findings": []},
        {"verdict": "approve", "findings": []},
    ])
    result = asyncio.run(load_workflow_specs(str(WORKFLOW))[0].fn(ctx, {"task": "Plan a research release"}))

    assert result["status"] == "completed"
    assert len(result["reviews"]) == 2
    assert list(result["ministries"]) == ["War", "Justice"]
    assert result["tokens_spent"] == 123
    assert ctx.parallel_widths == [2]
    assert ctx.logs == ["Menxia vetoed proposal round 1"]
    assert ctx.labels == [
        "Zhongshu Secretariat", "Menxia Chancellery", "Zhongshu Secretariat", "Menxia Chancellery",
        "Shangshu Department", "Ministry of War", "Ministry of Justice", "Shangshu Department",
        "Menxia Chancellery",
    ]
    assert ctx.phases[-1] == "Menxia audits the memorial"


def test_demo_rejects_empty_tasks_and_honors_repeated_veto() -> None:
    spec = load_workflow_specs(str(WORKFLOW))[0]
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(spec.fn(RecordingContext(), {"task": " "}))

    ctx = RecordingContext([{"verdict": "revise", "findings": ["Still unsafe"]}] * 3)
    result = asyncio.run(spec.fn(ctx, {"task": "Plan a research release"}))
    assert result["status"] == "blocked"
    assert result["stage"] == "review"
    assert len(result["reviews"]) == 3
    assert "Shangshu Department" not in ctx.labels


def test_demo_blocks_a_memorial_that_fails_the_final_audit() -> None:
    ctx = RecordingContext([
        {"verdict": "approve", "findings": []},
        {"verdict": "revise", "findings": ["Missing execution evidence"]},
    ])
    result = asyncio.run(load_workflow_specs(str(WORKFLOW))[0].fn(ctx, {"task": "Plan a research release"}))
    assert result["status"] == "blocked"
    assert result["stage"] == "audit"
    assert result["audit"]["findings"] == ["Missing execution evidence"]


def test_interactive_court_enters_through_zhongshu() -> None:
    team = load_team_config(path=TEAM)
    ministries = {"personnel", "revenue", "rites", "war", "justice", "works"}
    assert team.entry == "zhongshu"
    assert set(team.roles) == {"zhongshu", "menxia", "shangshu", *ministries}
    assert team.topology.edges["zhongshu"] == frozenset({"menxia", "shangshu"})
    assert team.topology.edges["shangshu"] == frozenset(ministries)
    assert not team.topology.allows("zhongshu", "war")
    assert not team.topology.allows("menxia", "shangshu")


def test_example_keeps_its_bilingual_guides_together() -> None:
    english = README.read_text(encoding="utf-8")
    chinese = README_ZH_CN.read_text(encoding="utf-8")

    assert "[Chinese](README.zh-CN.md)" in english
    assert "[English](README.md)" in chinese
    assert "examples/mini-edict/team.yaml" in english
    assert "examples/mini-edict/team.yaml" in chinese
